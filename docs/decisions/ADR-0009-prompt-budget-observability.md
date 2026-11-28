# ADR-0009：Prompt/context 预算、批处理与可观测性

- 状态：Accepted
- 日期：2026-11
- 影响版本：`PromptBuilder`、`BatchPlanner`、metrics schema `5`
- 相关代码：`src/openreview/review/prompt_builder.py`、`token_budget.py`、`services/batch_planner.py`、`domain/metrics.py`
- 补充：[ADR-0003](ADR-0003-budget-in-client.md) 的调用预算、[ADR-0004](ADR-0004-single-pass-per-batch.md) 的单遍调用

## 上下文

大 PR 会同时遇到三个问题：

- 整个 diff 塞不进上下文窗；
- 简单按文件截断会割裂同一函数的 hunk；
- 只看 token 总数无法区分“没检索到”和“检索到但被 prompt 丢掉”。

## 决策

- **Prompt 预算分层**：soft=10k / hard=14k；先扣固定 prompt 与 context 预留，
  patch 的 soft/hard 约为 5.5k/9.5k；`max_patch_tokens`/`max_context_tokens`
  只作为最后安全上限。
- **Batch 规划**：总 prompt 未超 hard cap 时不拆分；超过后按 soft target
  尽量装满每个 bin；同一函数的 hunk 优先放在一起；单 hunk 仍超预算才截断。
- **上下文优先级**：源码 > 测试 > 配置/文档；任何增强失败退回 AST/diff。
- **可观测性**：`Run Metrics` 记录阶段耗时、文件/ batch 数、LLM 调用明细、
  真实 usage、patch/context token 估算；`retrieval.context_gap` 记录候选数、
  去重数、AST/Hybrid 截断、跨 batch 重复、prompt 使用/省略 context 与 token。
- **预算口径**：`--preflight-tokens` 是 split 级 lower bound；
  `--run-budget-tokens` 是单 PR 运行时硬上限；两者不可混用。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 只按文件数拆 batch | 文件大小差异大，容易爆窗 |
| 固定每文件一个 batch | 调用量随文件数爆炸，跨文件证据割裂 |
| 只报 token 总数 | 无法定位“检索丢失”还是“prompt 截断” |
| 用精确 tokenizer 做预算 | Provider/模型差异大，先做保守硬护栏 |

## 后果

正面：

- 成本/延迟可预测，失败可归因；
- 大 PR 不会静默截断；
- 评测能区分检索失败与 prompt 丢弃。

负面：

- 输入 token 是估算，单次请求可能超过预算；
- soft/hard 预算是质量与成本的折中，不是最优解；
- context 被省略时仍可能丢召回；`context_gap` 只是可见性，不是修复。

## 证据

- `tests/test_prompt_builder.py`、`tests/test_token_budget.py`；
- full dev：57 findings，2.95 calls/PR，912k tokens，116.8s/PR；
- 3-PR mini：AST/Graph/Verifier 的 tokens 分别约 178.8k / 185.0k / 236.2k。

## 复审条件

当接入精确 tokenizer、Provider 账单或 context rerank 后，可把“估算 + 硬上限”
升级为“账单对齐 + 质量反馈”，但保留 `context_gap` 和 Run Metrics 作为可观测契约。
