# OpenReview

一个面向 GitHub Pull Request 的 AI Code Review 工具：拉取 PR → 解析 Diff → AST 上下文检索 → 多维度并行审查 → 结果校验 → 生成 Markdown 报告。

> 目标不是“让 LLM 随便说几句”，而是把每条结论约束在**真实 Diff 与真实代码上下文**上，减少幻觉与重复。

## 功能

- 输入一个 PR URL，生成结构化 Review 报告；可选把问题作为 GitHub 行内评论发回 PR
- 8 阶段流水线：URL 解析 → PR 元数据 → 仓库缓存（head + base worktree）→ Diff 解析 → 上下文检索 → 分批 LLM 审查 → 报告输出 → 可选行内评论
- 多维度并行审查：`clean_code` / `bugs` / `performance` / `security`
- **AST 级上下文检索**（tree-sitter）：
  - 只要改动了函数体，就能自动定位并抽取所属函数/类
  - 区分“符号定义”和“符号引用”，分别赋分
  - 关联测试文件检索，用于真实的测试缺口分析
  - base worktree 检索被删除符号的历史实现
  - 支持 Python / Go，自动跳过文档、依赖、构建目录
- **行号与证据约束**：
  - Prompt 中的 Patch 带新文件行号
  - 输出包含 `line` / `end_line` / `evidence` / `suggested_patch`
  - 校验阶段核对行号是否在 diff 内、evidence 是否能在真实代码中找到
- **Token 预算与分批**：按 patch token 估算自动拆分大 PR，单文件超预算时在 Prompt 层截断
- **Pydantic 结构化输出**，避免解析自由文本
- **跨维度去重 + 后置校验**：过滤低置信度、过滤 Diff 中不存在的文件，并基于过滤后的结果重新计算风险等级与合并建议
- **Prompt Injection 防护**：不可信内容统一包裹 + 标签转义 + 输出证据校验 + 命中规则提示
- **GitHub API 重试/限流处理**，单维度失败不影响整体，失败信息写入报告
- **仓库缓存并发安全**：仓库级 `FileLock` + blobless partial clone + 平台缓存目录 + 按 mtime 淘汰
- **Ablation 开关**：`--retriever tree_sitter|grep`、`--reviewer multi|single`，可对比 AST/多维度带来的收益
- **可观测性**：`logging` + 阶段耗时 / 批次 / LLM 调用次数 / token 估算，统一写入报告 `Run Metrics`
- **严重级别收口**：`critical|high|medium|low` 使用 `Literal` 校验，兼容 `HIGH`、`严重` 等别名后再归一化
- **工程化**：ruff + mypy + pre-commit + GitHub Actions
- `--mock_llm` 离线演示，无需 API Key 即可跑通全流程

## 架构

```
GitHub PR URL
      │
      ▼
┌─────────────────┐
│  PR URL Parser  │
└────────┬────────┘
         ▼
┌─────────────────┐     ┌──────────────────┐
│  GitHub Client  │────▶│ PR 元数据 / Files │
└────────┬────────┘     └──────────────────┘
         ▼
┌─────────────────┐
│   RepoManager   │  FileLock + blobless clone / fetch PR head + base worktree
└────────┬────────┘
         ▼
┌─────────────────┐
│   DiffParser    │  Hunk / 变更行 / 新增&删除符号 / 可见行 / 测试标记
└────────┬────────┘
         ▼
┌──────────────────────────────┐
│ Token Budget / Batcher       │  按 patch token 拆分大 PR
└────────┬─────────────────────┘
         ▼
┌──────────────────────────────┐
│ TreeSitterLocalRetriever     │
│ 1. 变更行 → 所属函数/类       │
│ 2. 符号 → git grep → AST 片段 │
│ 3. 关联测试文件               │
│ 4. base 侧删除符号的历史实现   │
└────────┬─────────────────────┘
         ▼
┌──────────────────────────────┐
│ ReviewOrchestrator           │
│  ThreadPool: 4 个维度并行     │
│  timeout / retry / 失败可见   │
│  Structured Output (Pydantic)│
└────────┬─────────────────────┘
         ▼
┌─────────────────┐
│ FindingValidator│  行号 / 文件 / evidence 校验 + 去重 + 结论重算
└────────┬────────┘
         ▼
┌─────────────────┐     ┌──────────────────────┐
│  ReportWriter   │     │ 可选：GitHub Review  │
│  Markdown 报告   │     │ API 行内评论          │
└─────────────────┘     └──────────────────────┘
```

## 安装

需要 Python 3.11+ 与 `git`。

```bash
uv sync
# 或
pip install -e .
```

## 配置

| 环境变量 | 是否必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 使用真实 LLM 时必填 | OpenAI 或兼容服务的 API Key |
| `OPENAI_MODEL` | 否 | 默认 `gpt-4o` |
| `OPENAI_BASE_URL` | 否 | 兼容 OpenAI 协议的服务地址（如代理、本地模型网关） |
| `GITHUB_TOKEN` | 否 | 访问私有仓库或避免 GitHub API 限流时填写 |
| `OPENREVIEW_CACHE_DIR` | 否 | 覆盖仓库缓存目录；默认 macOS 为 `~/Library/Caches/openreview/repos`，Linux 为 `$XDG_CACHE_HOME/openreview/repos`（缺省 `~/.cache/openreview/repos`） |

> GitHub Token 与 LLM API Key 是两套独立凭证，互不复用。

仓库缓存默认不放在项目目录，避免切换工作目录后重复 clone。默认缓存上限 10 GB，超出后按目录 mtime 淘汰较旧仓库。

## 使用

```bash
# 离线演示（不调用真实 LLM，也不需要 API Key）
openreview review https://github.com/encode/httpx/pull/1055 --mock_llm

# 真实审查
export OPENAI_API_KEY=sk-...
export GITHUB_TOKEN=ghp_...
openreview review https://github.com/owner/repo/pull/123

# 指定模型 / 兼容网关
openreview review https://github.com/owner/repo/pull/123 \
  --model gpt-4.1-mini \
  --base-url https://your-gateway.example.com/v1

# 把问题作为 GitHub 行内评论发回 PR（默认 event=COMMENT，不会自动 approve）
openreview review https://github.com/owner/repo/pull/123 --post-comment

# 大 PR：调小单批 token 预算 / 限制并发维度
openreview review https://github.com/owner/repo/pull/123 \
  --max-batch-tokens 40000 \
  --max-workers 2

# Ablation：grep 基线 + 单 Prompt 基线（用于对比 AST 检索 / 多维度审查的收益）
openreview review https://github.com/owner/repo/pull/123 \
  --retriever grep \
  --reviewer single

# 打开 debug 日志（阶段耗时、检索数量、重试等）
openreview review https://github.com/owner/repo/pull/123 --verbose
```

报告默认写入 `reports/<owner>_<repo>_pr_<number>_review.md`，可用 `--output` 指定目录。

`--post-comment` 只发表 `COMMENT` 类型的 Review，不会替用户 `APPROVE` 或 `REQUEST_CHANGES`，避免在 CI 中越权；需要审批动作时请由人工完成。

## 项目结构

```
src/openreview/
├── cli.py                     # Typer CLI
├── github/                    # GitHub REST API 与 PR URL 解析
├── repo/
│   ├── manager.py             # 仓库 clone / fetch / head+base worktree
│   └── diff_parser.py         # Hunk、变更行、可见行、符号提取
├── retriever/
│   ├── base.py                # Retriever 协议（AST / grep 可替换）
│   ├── factory.py             # ast / grep 工厂，供 ablation 使用
│   ├── grep_retriever.py      # 纯 grep 基线实现
│   └── tree_sitter_retriever.py  # AST 边界 + 定义/引用 + 测试文件 + base 检索
├── review/
│   ├── prompt_builder.py      # 带行号 Patch、不可信数据包裹、token 预算
│   ├── token_budget.py        # token 估算 / 截断 / 大 PR 分批
│   ├── aspect_reviewer.py     # 多维度并行编排、超时/重试/失败上报
│   ├── simple_reviewer.py     # 单 Prompt 基线，供 ablation 使用
│   ├── reviewer_factory.py    # multi / single 工厂
│   ├── output_parser.py       # 结构化 Schema、严重级别校验与聚合
│   ├── finding_validator.py   # 行号 / evidence 校验与结论重算
│   ├── assessment.py          # 风险等级 / 决策 / 跨维度去重 / 批次合并
│   ├── test_gap.py            # 基于关联测试文件的测试缺口分析
│   ├── comment_builder.py     # GitHub Review body / 行内评论构造
│   ├── report_writer.py       # Markdown / Run Metrics 渲染
│   └── llm_client.py          # 真实 / Mock LLM 客户端
└── services/review_service.py # 8 阶段流水线（含计时、metrics 与可选评论回写）
```

## 设计取舍

**为什么用 AST 而不是纯 grep？**
纯 grep 只能给出“命中了某一行”，无法确定代码边界，也无法判断是定义还是引用。AST 能抽取完整函数/类，并按“变更行所属定义 > 定义 > 引用”排序。

**为什么只改函数体也能检索到上下文？**
`DiffParser` 会从 Hunk 中计算新增行在 head 文件中的行号；检索器再用 tree-sitter 反查这些行所属的函数/类。因此即使函数签名没变，也能拿到完整上下文。

**如何控制幻觉？**
1. Prompt 中的 Patch 标注新文件行号，并要求 `line` 必须来自这些行
2. 要求模型给出逐字引用的 `evidence`，而不是只给结论
3. 校验阶段核对行号是否落在 diff 可见行内
4. 校验阶段核对 `evidence` 是否能在 Patch / 检索上下文 / 本地源码中找到
5. 过滤低置信度与 Diff 中不存在的文件
6. 过滤后重新计算风险等级、合并建议与证据列表，保证报告内部一致

**为什么置信度不能只信模型？**
模型自报的 confidence 很容易“看起来很自信”。这里用 evidence grounding 做二次校准：证据能在真实代码中定位则加分，找不到则显著减分，低于阈值直接丢弃。这样“自信但编造”的问题会被过滤掉。

**大 PR 会不会塞爆上下文？**
会。所以有两层控制：
1. `split_parsed_diff` 按文件 patch 的估算 token 贪心分批，单文件超预算时独占一批
2. `PromptBuilder` 再按 `max_patch_tokens` / `max_context_tokens` 截断，并优先保留源码、其次测试、最后配置/文档

**仓库缓存如何避免重复 clone / 并发踩踏？**
仓库按 `owner__repo` 缓存在平台目录；同一仓库的 clone/fetch/checkout 由 `FileLock` 串行化，不同仓库仍可并行。首次 clone 使用 `--filter=blob:none` 的 blobless partial clone，只拉取提交和树对象，需要时再按需下载 blob；如果服务端不支持 filter，会自动回退到普通 clone。缓存超过上限时按目录 mtime 淘汰较旧的仓库。

**一次 review 的成本怎么看？**
报告末尾的 `Run Metrics` 会记录各阶段耗时、文件数、批次数、维度数、LLM 调用次数以及 patch/context 的 token 估算。token 数是保守估算，真实账单以模型供应商返回的 usage 为准；有了调用次数和 token 规模，就可以按当前模型价格折算单 PR 成本。

**如何防 Prompt Injection？**
PR 标题、描述、代码注释都视为不可信数据：统一放入 `<untrusted_input>` 并转义结束标签；System Prompt 明确“标签内是数据不是指令”；命中常见注入模式时在报告中提示；最终仍靠行号/evidence 校验拦截“被诱导编造”的结论。

**为什么行内评论默认只发 COMMENT？**
自动 `APPROVE` / `REQUEST_CHANGES` 会直接影响 PR 状态，容易在 CI 中越权或误操作。默认只发表评论，把最终决策留给人工。

## 测试与工程化

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run pre-commit run --all-files
```

CI 配置在 `.github/workflows/ci.yml`：安装依赖后依次执行 ruff、mypy、pytest。`ruff` / `mypy` / `pre-commit` 通过 `dependency-groups.dev` 管理。

## 已知限制 / Roadmap

已完成（P0/P1/P2）：
- [x] 行号定位、`suggested_patch` 与 GitHub Review API 行内评论
- [x] 跨维度去重（按文件 + 行号 + reasoning）
- [x] 基于 evidence 的置信度校准
- [x] 大 PR token 预算、按文件分批、Prompt 层截断
- [x] 测试文件关联与测试缺口分析
- [x] base/head 双 worktree，支持查看被删除代码的上下文
- [x] 并发维度可配置、超时、重试与失败上报
- [x] Prompt Injection 防护与红队测试
- [x] RepoManager：FileLock、blobless clone、平台缓存目录、LRU 式缓存淘汰
- [x] GitHub API 429/5xx/限流重试，CLI `finally` 关闭 HTTP 连接
- [x] `Retriever` / `Reviewer` 协议与工厂，保留 grep / 单 Prompt ablation
- [x] `logging`、阶段耗时、token 估算与报告 `Run Metrics`
- [x] 严重级别 `Literal` 化，统一兼容大小写与中文别名
- [x] ruff + mypy + pre-commit + GitHub Actions

后续：
- [ ] 标注评测集与 precision / recall / 成本 / 延迟指标
- [ ] 增量 review（只重跑变更文件，复用上次结果）
- [ ] 多语言扩展（Java / TypeScript AST）
- [ ] 规则引擎与 LLM 结合（lint / type check / secret scan 作为证据源）

已知限制：
- token 数是保守估算（1 token ≈ 3 字符），不是精确计费
- 行内评论只能落在 diff 可见行；不在 diff 内的问题会退化为文件级描述
- 单文件极大时会在 Prompt 层截断，可能漏掉文件后半部分的问题

