# OpenReview

OpenReview 是一个面向 GitHub Pull Request 的 AI 代码审查工具。它把代码检索、LLM 结构化输出、证据校验和成本护栏组合成一条**有界、可追踪、可降级**的审查流水线，而不是让模型自由规划的 Agent。

> 项目用于展示 AI 应用与后端工程设计。评测框架已搭建；实际质量对比结果尚未填写，见[评测结果](#评测结果)。

## 解决的问题

单纯把整个 diff 发给模型，容易遇到上下文缺失、长 PR 超限、行号幻觉、调用成本不可预期和失败状态不清晰等问题。OpenReview 将这些问题拆到显式模块中处理：

- **检索相关代码**：组合 tree-sitter AST、符号索引、import/call graph 和按需 grep fallback，围绕变更代码寻找定义、调用方、依赖、测试与 base 版本证据。
- **控制输入规模**：按 patch、上下文与最终 prompt 分层限额；大 PR 按文件、符号和 hunk 分批，避免单个文件独占上下文。
- **约束模型输出**：使用 Pydantic structured output；校验 finding 是否位于 diff、证据是否可定位，并可对高严重度 finding 做 verifier 二次检查。
- **预算与故障可见**：每次 review 有独立预算、结构化错误、阶段耗时与结果状态；单批失败可生成部分结果，流程失败也保留运行指标。
- **支持本地复现**：可从本地仓库的 base/head commit 执行 review，也可录制并回放 LLM 响应。

## 架构与数据流

```text
GitHub PR / 本地 base..head
            │
            ▼
       PR 元数据与 Diff
            │
            ▼
       规则 triage ───────── 文档/配置-only 可跳过 LLM
            │
            ▼
 Impact / 静态信号 / AST + 符号索引 + 代码图谱
            │
            ▼
     上下文优先级与 token 预算
            │
            ▼
   按 batch 执行结构化 single-pass LLM
            │
            ▼
 FindingValidator ── 可选 FindingVerifier
            │
            ▼
 Markdown / JSON 报告、可选 GitHub review 评论
```

`ReviewExecutor` 运行固定有界工作流；`ReviewState` 保存单次运行状态、预算、节点 trace 和错误。只有 triage、verifier 和失败降级等少数决策会改变路径，不存在模型自主规划或无限循环。

### 上下文扩展

默认 `HybridRetriever` 先增量刷新仓库索引，再并行运行 AST 与 graph 检索。上下文依优先级从变更行附近的静态证据、所属函数/类、相关定义、引用、字段到关联测试进行装配；同文件的相关代码会紧邻其 diff 注入。检索结果会去重、按文件轮询，并受 priority/kind cap 和上下文 token 预算约束。

graph/index 不可用时，主审查仍可继续使用 AST 结果；在满足条件时还会尝试 grep fallback。上下文不足或被预算省略会记录在报告与 metrics 中，而不是静默视为完整证据。

### LLM 与 finding 校验

默认每个 batch 使用一次 structured LLM 调用，覆盖 bug、安全、性能、可维护性和测试缺口等审查重点。模型结果先经 schema 解析，再校验文件、diff 行号、证据、置信度和去重；verifier 可对配置范围内的候选 finding 做有限次数的二次判定。

模型凭证与 GitHub 凭证分离。发送前会对高置信度凭证做脱敏；PR、diff 和检索到的代码均作为不可信数据传入 prompt，并由结果校验限制输出引用。

## 成本与预算语义

可通过 CLI 设置调用次数、输入 token、输出 token 和 LLM 请求时间预算。预算按每次 review 隔离，并由主审查、verifier 和重试共用。

| 预算项 | 调用前的控制 | 重要边界 |
|---|---|---|
| `--budget-calls` | 每次 provider 请求尝试前原子预留；显式重试单独计数 | 没有响应的网络失败也可能已被 provider 接收，因此按保守方式计账 |
| `--budget-tokens` | 用字符启发式估算 system/user prompt 与结构化 schema 的输入长度并在请求前检查 | tokenizer 与模型/兼容服务有关；真实输入用量只能在响应后校正，所以估算偏差可能导致本次请求后超过上限，后续请求会被拦截 |
| `--budget-output-tokens` | 请求前预留可用额度，并将单次 `max_completion_tokens` 限制在预留额度内 | provider 若不遵守兼容 API 的输出上限，仍需以返回 usage 监测超限 |
| `--budget-seconds` | 请求超时不超过配置的单次 timeout 与剩余 review 预算 | 限制的是 LLM 请求等待；不能强制取消任意本地代码或所有客户端后处理，因此整个 CLI 墙钟时间可能更长 |
| `--max-completion-tokens` | 限制单次模型最大输出 | 与 review 总输出预算同时生效；设置为 `0` 时取消此单次配置上限，但总输出预算仍可限制请求 |

`--budget-tokens` 是**估算输入 token 的调用前护栏**，不是基于精确模型 tokenizer 的绝对账单上限。报告会同时记录估算/真实用量、预留和超限状态及限制语义。当前不把 token 换算为货币成本；provider 价格、缓存计费和折扣需要额外的价格配置，不能从 token 数直接推断。

## 失败与并发

- **部分成功**：单个 batch 失败会记录错误并继续；剩余预算不足时停止后续调用。若已有有效结果，会生成明确标记为 `partial` 的报告，而非伪装成完整成功。
- **流程失败**：即使尚无 review 结果，也会尽力保存失败指标到 `<output>/run_<run_id>_metrics.json`；有 debug sink 时同时写入中间状态。错误记录和清理失败不会静默覆盖原始异常。
- **错误分类**：LLM provider 错误、输入/配置问题、预算不足、仓库与缓存问题按稳定类别进入 metrics。检索等增强环节在不影响主路径时按 best-effort 降级并记录原因。
- **调用重试**：关闭 SDK 内部的隐式重试，由预算层逐次重试可重试的 provider/network 错误；每次尝试都检查调用数、输出预留和剩余时间。
- **并发归属**：每次 run 使用独立预算客户端，provider 调用事件带 `run_id`，用该标识计算本次调用详情与 usage，避免共享累计计数器的差分串账。

运行级可观测字段包括阶段耗时、LLM 尝试/用量、预算、cache 命中、检索统计、validator/verifier 漏斗、trace、错误和最终 outcome。`--dump-intermediates` 会保存 prompt、LLM 解析结果和状态快照；这些调试文件可能包含代码及 PR 内容，应限制访问并按需清理。

## 快速开始

需要 Python 3.11+ 和 Git。

```bash
uv sync
```

### Mock 演示

不需要 API Key：

```bash
uv run openreview review \
  https://github.com/owner/repo/pull/123 \
  --mock_llm
```

Mock 只验证执行链路和报告格式，不代表审查质量。

### 真实模型审查

```bash
export OPENAI_API_KEY=...
export GITHUB_TOKEN=...  # 可选；私有仓库或需要较高 API 限额时使用

uv run openreview review \
  https://github.com/owner/repo/pull/123 \
  --model gpt-4o \
  --budget-calls 8 \
  --budget-tokens 20000 \
  --budget-output-tokens 8000 \
  --budget-seconds 120 \
  --max-completion-tokens 2048 \
  --json-out reports/result.json
```

可通过 `--base-url` 使用 OpenAI-compatible endpoint；`--llm-max-retries` 指定额外重试次数，默认不重试。报告默认写入 `reports/`。

### 本地 commit 区间与缓存

Python API 可使用 `ReviewPipeline.run_local(repo_path, base_sha, head_sha)`，不经过 GitHub API；仓库会通过临时 detached worktree 隔离 base/head。完整 review 结果可按 PR head 缓存，LLM 响应支持 record/replay：

```bash
uv run openreview review https://github.com/owner/repo/pull/123 \
  --cache-review \
  --llm-cache-dir .cache/llm \
  --llm-cache-mode record

# 只回放已录制的响应；不存在的 key 会显式失败
uv run openreview review https://github.com/owner/repo/pull/123 \
  --llm-cache-dir .cache/llm \
  --llm-cache-mode replay
```

### 调试与验证

```bash
uv run openreview review https://github.com/owner/repo/pull/123 \
  --dump-intermediates .cache/debug \
  --verbose

uv run ruff check
uv run mypy src/openreview
uv run pytest
```

## 配置参考

| 配置 | 用途 |
|---|---|
| `OPENAI_API_KEY` | 真实 LLM 调用凭证 |
| `OPENAI_MODEL` | 默认模型名；也可用 `--model` |
| `OPENAI_BASE_URL` | OpenAI-compatible API 地址；也可用 `--base-url` |
| `GITHUB_TOKEN` | 可选 GitHub API 凭证，与 LLM Key 独立 |

更多 CLI 参数可运行 `uv run openreview review --help` 查看。

## 评测结果

<!-- 效果评测完成后填写；在此之前不提供未实测的质量结论。 -->

| 配置/消融 | 样本切分 | 质量指标 | 检索指标 | 调用/Token | 延迟 |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## 评测方法

`evaluation/` 提供 AACR-Bench Python/Go 固定切分适配、确定性指标、检索分析、可选语义评测及多 run 报告生成。设计上区分三类问题：

- **确定性正确性**：行号、文件、证据、归因和 validator/verifier 漏斗是否满足约束。
- **检索质量**：变更符号、相关文件和引用上下文是否被找回；同时观察重复和截断。
- **语义效果**：在固定样本和统一 judge 下比较 finding 质量；对照不同检索/verifier 消融时同时报告成本与延迟。

**实际评测数值、对照结论和误差区间暂未填写**，待运行并复核后再加入上面的结果表。Mock 结果不作为模型质量证据。

## 当前限制与取舍

- AST/结构索引主要覆盖 Python 和 Go；其他语言以 diff 为主。call graph 是 best-effort，不是完整类型/数据流分析。
- token 估算是跨 provider 的保守启发式，并非精确 tokenizer 计量；真实 usage 依赖 provider 返回。
- 代码检索与上下文完整性会影响 finding 质量；预算省略的证据仍可能导致漏报。
- 当前报告 token 与调用数，不计算价格；没有提供集中式指标后端或告警系统。
- 可选静态分析会执行本地工具，审查不可信仓库时应在隔离环境运行。
- Prompt injection 缓解和证据校验用于降低风险，不构成模型安全或代码审查正确性的保证。

## 代码布局

```text
src/openreview/
  domain/       # 模型、错误分类、预算/metrics 类型
  github/       # GitHub REST API
  repo/         # diff、仓库缓存与 worktree 管理
  retriever/    # AST、索引、graph 与上下文预算规划
  review/       # prompt、LLM、validator/verifier、缓存、报告
  services/     # 工作流执行、批次/evidence 编排、metrics 收集
evaluation/     # 固定切分与评测适配
tests/          # 单元与集成测试
```
