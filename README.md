# OpenReview

**面向 GitHub PR 的 AI Code Review：检索代码证据、控制模型预算，并明确呈现不完整结果。**

把整个 diff 直接交给 LLM，常见问题是：缺少跨文件上下文、大 PR 超出上下文窗、finding 行号或证据不可靠、重试和复核推高成本，以及失败被误看作成功。OpenReview 用一条**有界、可验证、可观测**的流水线处理这些工程问题；它不是自主规划的 Agent。

## 全局架构

```mermaid
flowchart TD
    A[GitHub PR / 本地 base..head] --> B[拉取信息与解析 Diff]
    B --> C[规则 Triage]
    C -->|需要审查| D[Impact / 可选静态分析]
    D --> E[AST + 符号索引 + Import/Call Graph]
    E --> F[上下文去重、排序与预算]
    F --> G[按 Batch 结构化 LLM 审查]
    G --> H[Finding 校验]
    H --> I[可选有界 Verifier]
    I --> J[Markdown / JSON 报告]
    J --> K[可选 GitHub Review 评论]
    C -->|文档/配置变更| J
```

工作流节点显式编排；模型调用、预算和错误都按单次 run 跟踪。设计权衡、异常路径和局限见[架构说明](docs/ARCHITECTURE.md)。

## 评测状态

AACR-Bench Python/Go 子集：40 个 PR / 10 个仓库，按仓库互斥切分 dev / holdout 各 20 个 PR。当前已完成：

- **full dev production**（20 PR）：P 43.9% / R 17.9% / Balanced 25.4%，20/20 ok
- **3-PR mini ablation**（2 Go + 1 Python）：AST 把 file recall 从 0 提到 89.3%；graph 提高召回但降低精度，balanced 基本持平；verifier 在当前配置下 0 rejection
- **6-PR prompt 实验**（附录）：P 50.0% → 68.0%，R 16.3% → 20.9%，仅作方向验证
- **尚未完成**：full-dev 四档 ablation、holdout production、独立 judge 复核

真实数字、语言分层、失败分析和“不可对外声称”的边界见[评测结果快照](docs/evaluation-results.md)。

## Quickstart

需要 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

用 mock 响应跑通端到端流程（仍需联网读取公开 PR 并准备仓库）：

```bash
uv run openreview review https://github.com/owner/repo/pull/123 --mock_llm
```

`--dump-intermediates` 的产物结构：

```text
reports/debug/run-001/
  state.json                  # PR、parsed_diff、batches、contexts、stats、trace、outcome
  metrics.json
  prompts/01_single_shot.system.txt
  prompts/01_single_shot.user.txt
  llm/01_single_shot.parsed.json
  validation/01.json          # 每条 finding 的 confidence 变化、丢弃原因、patch 校验
  verifier/01.json            # 每条 finding 的 verdict / reason / confidence
```

该目录只用于本地排查，默认不创建；`--dump-intermediates` 不参与 review cache key，
不会影响缓存命中。

### 本地仓库模式（评测 / 离线回放）

不经过 GitHub API，直接对任意本地 git 仓库的 `base..head` 做 review。适合
AACR-Bench 这类给出 `repo + base_commit + head_commit` 的评测集：

```python
from pathlib import Path
from openreview.services.review_service import ReviewPipeline

with ReviewPipeline(mock_llm=True) as pipeline:
    report = pipeline.run_local(
        repo_path=Path("/path/to/repo"),
        base_sha="<base_commit>",
        head_sha="<head_commit>",
        output_dir=Path("reports/local"),
        owner="encode",
        repo="httpx",
        pr_number=1055,
    )
    result = pipeline.last_result  # ReviewResult，含 source_aspects / metrics
```

`run_local` 会在系统临时目录创建 detached head/base worktree，结束后自动清理
（`keep_worktrees=True` 可保留用于排查）。本地模式不会执行 GitHub 写操作，
`post_comments` 不生效。

`pipeline.last_outcome` 会给出本次运行的结构化状态：`completed` / `skipped` /
`partial` / `failed`。部分 batch 失败或预算耗尽时仍会生成报告，但状态为
`partial`，CLI 以非零退出码返回，评测结果也不会把它当成完整成功。

报告默认写入 `reports/<owner>_<repo>_pr_<number>_review.md`，可用 `--output` 指定目录。

`--post-comment` 只发表 `COMMENT` 类型的 Review，不会替用户 `APPROVE` 或 `REQUEST_CHANGES`，避免在 CI 中越权；需要审批动作时请由人工完成。发布前会按 marker 查找并删除上一轮由 OpenReview 创建的 review，因此同一 PR 重跑是幂等的，不会重复堆积评论。

## 项目结构

```
src/openreview/
├── cli.py                     # Typer CLI
├── github/                    # GitHub REST API 与 PR URL 解析
├── repo/
│   ├── manager.py             # 仓库 clone / fetch / head+base worktree
│   ├── local.py               # 本地 repo + base/head worktree + git diff（评测/离线）
│   └── diff_parser.py         # Hunk、变更行、可见行、符号提取
├── retriever/
│   ├── base.py                # Retriever 协议
│   ├── pathing.py             # AST / index / impact 共用的目录剪枝规则
│   ├── grep.py                # 符号引用检索：rg 优先，git grep 分批 + 有界 Python 兜底
│   ├── tree_sitter_retriever.py  # AST 边界 + 定义/引用 + 测试文件 + base 检索
│   ├── hybrid.py                # AST + 关系索引 + graph 融合与去重
│   ├── index.py                 # SQLite 符号与 import/call 关系索引
│   ├── graph.py                 # import/call graph 扩展（caller/callee/依赖）
│   └── query.py                 # 从 Diff 构造检索查询
├── review/
│   ├── config.py              # ReviewConfig：配置校验 + cache key 指纹
│   ├── prompt_builder.py      # 带行号 Patch、不可信数据包裹、token 预算
│   ├── token_budget.py        # token 估算 / 截断 / 大 PR 分批
│   ├── workflow.py            # 有界工作流、ReviewBudget、ReviewOutcome、trace
│   ├── triage.py              # 规则 triage（docs-only / test-only）
│   ├── impact.py              # 跨文件符号引用分析
│   ├── static_analysis.py     # ruff/mypy 证据源（默认关闭）
│   ├── single_shot.py         # 单遍审查（每个 batch 1 次调用）
│   ├── ablation.py            # 固定四档消融定义
│   ├── finalize.py            # 审查结果收敛（validator / 测试缺口 / injection）
│   ├── output_parser.py       # 结构化 Schema、严重级别校验与聚合
│   ├── finding_validator.py   # 行号 / evidence 校验与结论重算
│   ├── evidence_anchor.py     # 多行 evidence / 省略号 / 缩进锚定修复
│   ├── verifier.py            # critical/high 语义复核
│   ├── cache.py               # review 结果缓存
│   ├── llm_cache.py           # LLM 响应 record/replay
│   ├── assessment.py          # 风险等级 / 决策 / 跨维度去重 / 批次合并
│   ├── test_gap.py            # 基于关联测试文件的测试缺口分析
│   ├── comment_builder.py     # GitHub Review body / 行内评论构造、severity 门禁
│   ├── redaction.py           # 高置信度密钥 / 内网地址脱敏
│   ├── report_writer.py       # Markdown / JSON / Run Metrics 渲染
│   └── llm_client.py          # 真实 / Mock LLM 客户端（含 token usage）
└── services/
    ├── review_service.py      # ReviewPipeline：组件构造 / 生命周期 facade
    └── review_executor.py     # 有界工作流节点实现与 metrics
```

## 设计取舍

**为什么检索采用 AST + 图扩展，而不是 FTS5？**
tree-sitter AST 是结构化主路径，能定位变更行所属的函数/类；SQLite 只保存符号定义与 import/call 关系，符号查找走精确名字和 LIKE 兜底。FTS5/BM25 在这里收益有限：代码审查更依赖精确符号和结构关系，而全文检索容易把弱相关定义带进 prompt。import/call graph 补上“谁调用了变更符号 / 变更函数调用了谁”，把影响面从文件级提升到函数级。上下文按相关性排序并去重，任何增强失败时仍可退回 AST 结果。call graph 是 best-effort：Python 用 `ast` 抽取 caller/callee，Go 通过 receiver/import/同名方法做带置信度过滤的 caller/callee 扩展；不宣称完整类型解析或接口动态分发。当前索引和 AST 解析都聚焦 Python / Go；其他语言只使用 diff，不注入结构上下文。

**为什么引用检索不直接用 `git grep`？**
`git grep -e sym1 -e sym2 ...` 会把多个模式合成一个交替正则，耗时随模式数和匹配行数一起放大。实测同一个 676 文件 / 10MB 的 Go 仓库：1 个模式 0.03s，10 个 2.8s，20 个 5.6s，而 20 个模式用 ripgrep 只要 0.02s。默认 50 个检索目标会让每次 batch 都撞上 15s 超时并被静默降级。现在 `retriever/grep.py` 优先用 `rg -F -w`（固定字符串、整词、源码 glob），没有 ripgrep 时把 `git grep` 拆成 4 个模式一批并共享总时间预算，最后才退到有界的 Python 扫描；命中数和 AST 解析量都有硬上限，引擎超时只降级、不挂住 review。

**为什么 triage 只用规则、不用模型？**
triage 的目标是减少无效调用，不是做判断。用一次 LLM 调用决定“要不要调用 LLM”在调用量上不成立；docs-only / test-only 这类信号用文件后缀和 diff 元数据就能确定性判断，也更容易测试。

**为什么只保留单遍审查？**
四维度并行共享同一份检索上下文、同一个模型和同一套输出结构，差异主要来自 persona prompt；在没有证据表明它能带来成比例收益之前，四倍调用不划算。单遍审查把四类风险放进同一份 checklist，大 PR 仍按 batch 拆分，评测维度也从“reviewer 模式”收敛为检索与 verifier 四档消融。

**validator 已经校验了行号和 evidence，为什么还要 verifier？**
validator 做的是确定性校验：文件是否存在、行号是否在 diff 内、evidence 能否定位。它拦不住“证据真实但结论过度解读”的问题。verifier 只处理 `critical` / `high`，最多回炉一次，并且所有调用都受 `ReviewBudget` 约束，避免二次校验变成调用量黑洞。

**静态分析为什么默认关闭？**
静态分析会在本地对 PR 代码执行 ruff/mypy，等于执行不可信输入。默认关闭，CLI 不暴露该开关；需要时通过 `ReviewPipeline` 参数开启，且只跑变更文件、只保留落在 diff 可见行上的信号。接入 CI 时应放在沙箱或受控 runner 中。它只作为证据源，最终结论仍由 LLM + validator 收敛。

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
会。所以有三层控制：
1. **内部 prompt 预算策略**（不暴露 CLI）：总 prompt 没超过 hard cap 时不拆分，保留完整上下文；超过后才按 soft target 把内容尽量装满每个 bin，避免无谓地拆成很多小调用。
2. **文件过大时按 hunk / enclosing symbol 切片**：优先把同一函数的 hunk 放在一起；只有单个 hunk 仍超预算时才截断。
3. `PromptBuilder` 仍保留 `max_patch_tokens` / `max_context_tokens` 作为最后的安全上限，并优先保留源码、其次测试、最后配置/文档。

默认策略是 `soft=10k / hard=14k`：先扣掉约 2k 的固定 prompt 开销和 2.5k 的 context 预留，patch 的 soft/hard 预算约为 5.5k / 9.5k。`--max-batch-tokens` 现在只是外部硬上限（可以让 batch 更小），不再是主要的“质量旋钮”。

**仓库缓存如何避免重复 clone / 并发踩踏？**
仓库按 `owner__repo` 缓存在平台目录；同一仓库的 clone/fetch/checkout 由 `FileLock` 串行化，不同仓库仍可并行。首次 clone 使用 `--filter=blob:none` 的 blobless partial clone，只拉取提交和树对象，需要时再按需下载 blob；如果服务端不支持 filter，会自动回退到普通 clone。缓存超过上限时按目录 mtime 淘汰较旧的仓库。

**一次 review 的 token 用量怎么看？**
报告末尾的 `Run Metrics` 会记录各阶段耗时、文件数、批次数、reviewer mode、focus aspects 数、LLM 调用次数、每次调用的模型/延迟/token 明细、真实 `prompt_tokens` / `completion_tokens` 用量，以及 patch/context 的 token 估算。真实用量来自模型响应，并会回冲预扣预算；估算值用于调用前检查和截断。两者都保留，方便对照“估算偏差”和评估单 PR 调用量。`--budget-*` 可以在调用前直接卡住超支。

`retrieval.context_gap` 进一步区分“没检索到”和“检索到但被丢掉”：它记录检索候选数、去重数、AST/Hybrid 上限截断数、搜索目标截断数、跨 batch 重复上下文，以及 prompt 实际使用/省略的上下文数量和 token。被省略时 review notes 也会提示，避免上下文缺口静默消失。`retrieval.stats` 里还会记录引用检索的 `grep_hits`、`grep_timeouts`、`reference_contexts_truncated`，用来判断这次上下文缺失是“没有引用”还是“检索引擎降级了”。跨文件 impact 证据按当前 batch 的变更符号过滤，只有无法归属时才由首个 batch 带上少量全局证据，避免同一段引用重复占用每个 batch 的上下文预算。

**如何防 Prompt Injection？**
PR 标题、描述、代码注释都视为不可信数据：统一放入 `<untrusted_input>` 并转义结束标签；System Prompt 明确“标签内是数据不是指令”；命中常见注入模式时在报告中提示；最终仍靠行号/evidence 校验拦截“被诱导编造”的结论。

**为什么行内评论默认只发 COMMENT？**
自动 `APPROVE` / `REQUEST_CHANGES` 会直接影响 PR 状态，容易在 CI 中越权或误操作。默认只发表评论，把最终决策留给人工。

## 评测

评测分两层：

- **无需 LLM 调用的确定性指标**：行号合法性、按 finding 行号精确匹配源码的 evidence 核验（不可核验项单独统计）、triage、归因、token/调用用量
- **检索缺口指标**：`prompt_context_coverage`、`retrieval_truncated_total`、`cross_batch_duplicates_total` 等，用于区分“召回失败”和“召回后被 cap / prompt 预算丢弃”；它们不是人工标注的 ground-truth recall，但能直接暴露检索链路的损耗位置
- **可选语义指标**：复用 AACR-Bench 官方 LLM judge，计算文件级 Precision / Recall / Balanced

数据使用 AACR-Bench Python/Go 子集的固定切分，同一仓库的 PR 不会跨 split；每种语言分别分配 dev、repo-level holdout 仓库，整体约 5:5。报告提供按语言/仓库分层的语义指标；单 run 不确定性使用仓库簇 bootstrap，多 run 对比使用语言分层的配对 PR bootstrap 95% 置信区间。当前结果统一记录在[评测结果快照](docs/evaluation-results.md)，README 只保留摘要。

确定性指标可以作为 CI 门禁，不消耗 token。评测运行的指标文件默认位于
`evaluation/metrics/`（该目录不提交）；CI 使用仓库内的受控 fixture 验证门禁
CLI 和阈值契约，不代表本次提交运行了模型评测：

```bash
uv run python -m evaluation.cli gate \
  --metrics tests/fixtures/evaluation/ci_deterministic_metrics.json
```

门禁默认要求：结果无 error/missing、未知文件率 0、非法行号率 0、未 grounding 的 evidence 率 0，evidence 可核验率至少 0.8、evidence/归因覆盖率不低于阈值；阈值可用 `--min-*` / `--max-*` 调整。
运行真实或 mock 评测后，可将实际生成的 `deterministic_<run>.json` 传给同一命令检查该次结果。

### 结果与复现

真实结果、语言分层、mini ablation 和失败分析见[评测结果快照](docs/evaluation-results.md)。
复现命令、manifest、分片并发和 semantic judge 配置见[评测手册](evaluation/README.md)。

full dev production（20 PR）：

| 指标 | 值 |
|---|---:|
| N | 20 / 20 ok |
| Findings | 57 |
| Precision | 43.9% |
| Recall | 17.9% |
| Balanced | 25.4% |
| Evidence（锚定后） | 100% |
| Calls / PR | 2.95 |
| Tokens | 912k |
| Latency | 116.8s/PR |

语言分层：

```text
Go:     P 34.4% / R 12.8% / Balanced 18.6%
Python: P 56.0% / R 27.5% / Balanced 36.8%
```

3-PR mini ablation（2 Go + 1 Python，方向性）：

| Tier | Precision | Recall | Balanced | File recall | Tokens |
|---|---:|---:|---:|---:|---:|
| diff | 78.6% | 23.5% | 36.2% | 0.0% | 145.6k |
| ast | 76.9% | 21.6% | 33.7% | 89.3% | 178.8k |
| graph | 63.2% | 25.5% | 36.3% | 89.3% | 185.0k |
| verifier | 64.7% | 21.6% | 32.4% | 89.3% | 236.2k |

解读时至少同时看四件事：**质量（P/R）是否提升、检索覆盖是否提升、token 是否成比例、证据是否仍然可定位**。
3 个 PR 的 P/R 只能看方向，不能做语言或显著性结论；full-dev ablation 和 holdout 仍未运行。

## 测试与工程化

```bash
uv run pytest -q
uv run ruff check src tests evaluation
uv run mypy src evaluation
uv run pre-commit run --all-files
```

CI 配置在 `.github/workflows/ci.yml`：安装依赖后依次执行 ruff、mypy、pytest，最后用跟踪的受控 fixture 运行确定性评测门禁（`evaluation.cli gate`）。这保证 gate 命令和阈值契约有效；它不代表 CI 运行了 AACR 评测或调用了模型。实际结果仍可用同一 gate 命令检查。`ruff` / `mypy` / `pre-commit` 通过 `dependency-groups.dev` 管理。

## 已知限制 / Roadmap

已完成（P0/P1/P2）：
- [x] 行号定位、`suggested_patch` 与 GitHub Review API 行内评论
- [x] 跨维度去重（按文件 + 行号 + reasoning）
- [x] 基于 evidence 的置信度校准
- [x] 大 PR token 预算、按文件分批、Prompt 层截断
- [x] 测试文件关联与测试缺口分析
- [x] base/head 双 worktree，支持查看被删除代码的上下文
- [x] 单遍审查的超时、重试与失败上报
- [x] Prompt Injection 防护与红队测试
- [x] RepoManager：FileLock、blobless clone、平台缓存目录、LRU 式缓存淘汰
- [x] GitHub API 429/5xx/限流重试，CLI `finally` 关闭 HTTP 连接
- [x] 默认结构化检索：tree-sitter AST 主路径 + SQLite 符号/关系索引 + import/call graph；支持 AST / +graph 消融
- [x] 默认采用单遍 Review（每个 batch 1 次调用）；检索与 verifier 收敛为固定四档消融
- [x] 有界工作流（`workflow.py`）：条件节点、`ReviewBudget`、trace
- [x] 规则 triage：docs-only 跳过、test-only 收窄审查重点
- [x] 跨文件影响分析（`impact.py`）
- [x] 可选静态分析证据源（`static_analysis.py`，默认关闭）
- [x] 语义 verifier：critical/high 二次复核（`verifier.py`）
- [x] review 结果缓存与 LLM record/replay（`cache.py` / `llm_cache.py`）
- [x] 真实 token usage 与硬预算（调用数 / token / 耗时）
- [x] `logging`、阶段耗时、token 用量与报告 `Run Metrics`
- [x] 严重级别 `Literal` 化，统一兼容大小写与中文别名
- [x] ruff + mypy + pre-commit + GitHub Actions

后续：
- [x] AACR-Bench 固定切分、确定性指标与官方语义评测适配
- [x] 三点 diff 修复（32/40 个实例的 base 不是分支点）
- [x] evidence 锚定修复（70.2% → 100%，Gate PASS）
- [x] full dev production（20 PR，P 43.9 / R 17.9 / Balanced 25.4）
- [x] 3-PR mini 四档 ablation（方向性结论）
- [x] 6-PR prompt 实验（附录）
- [ ] full-dev 四档 ablation（20 PR × 4 tier）
- [ ] holdout production（冻结后只跑一次）
- [ ] 独立 judge / 人工 audit 校准 precision
- [ ] 增量 review（只重跑变更文件，复用上次结果）
- [ ] 多语言扩展（Java / TypeScript AST）
- [x] 行内评论幂等（按 marker 替换上一轮 review，同一 PR 重跑不重复发评论）
- [x] 确定性评测门禁（`evaluation.cli gate`，不调用模型，可直接放入 CI）

已知限制：
- 只支持 Python / Go；其他语言没有 AST 上下文，效果未做承诺
- call graph 是 best-effort：Python 用 `ast` 抽取，Go 通过 receiver/import/同名方法过滤 caller/callee；不做完整类型解析，也不解析接口动态分发
- patch/context 的 token 数仍是保守估算（ASCII 约 3 字符/token，CJK 按更高权重计），用于预算与截断；真实用量单独记录
- 行内评论只能落在 diff 可见行；不在 diff 行内或 severity 为 medium/low 的问题会写入 Review body，而不是行内评论
- 单文件极大时会先按 hunk / symbol 切片；只有单个 hunk 仍超预算时才截断，可能漏掉该 hunk 后半部分的问题
- 单遍审查的 findings 受单次输出 token 限制；大 PR 会按 batch 拆分，但单个 batch 仍可能截断
- 静态分析默认关闭、CLI 不暴露，且只覆盖 Python；通过 API 开启时会在本地执行 ruff/mypy
- `--mock_llm` 仅验证流程与报告格式，不代表审查质量；仓库中的示例报告也是 mock 演示输出，不应作为真实漏洞发现或评测结果
- full dev production 的 recall 只有 17.9%；50% 的 reference 落在零 finding 文件，生成覆盖仍是主要瓶颈
- semantic judge 与 reviewer 同模型，precision 可能有 self-preference bias；reference comment 稀疏，43.9% 应视为下限
- mini ablation 里 graph 是“召回换精度”，verifier 0 rejection、成本 +28%；二者都还没有证明稳定质量收益
- full-dev ablation 和 holdout 尚未运行；不要把 mini 的方向性结论当成最终结论

- [评测结果快照](docs/evaluation-results.md)：full dev production、mini ablation、prompt 实验、holdout 状态
- [reports 索引](reports/INDEX.md)：当前报告、归档、非结论文件
- [架构地图](docs/ARCHITECTURE.md)：系统边界、生命周期、关键决策索引
- [架构决策 ADR](docs/decisions/)：有界工作流、结构化检索、预算下沉、单遍审查、partial outcome
- [节点与预算契约](docs/architecture/03-workflow.md)：13 个节点的输入/输出/失败语义
- [评测方法](docs/architecture/08-evaluation.md)：指标定义、数据切分、四档消融、当前状态
- [评测复现步骤](evaluation/README.md)
