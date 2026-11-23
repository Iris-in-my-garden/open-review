# OpenReview 离线评测（AACR-Bench · Python/Go）

这套工具用于回答一个具体问题：**在真实 PR 上，OpenReview 的 review 结果是否变好了？**

它不经过 GitHub API、不发评论、也不依赖 PR 是否还开着。输入只有三样：

- 一个仓库（本地缓存或任意 `clone_url`）
- `base_commit` / `head_commit`
- 人工标注的 reference comments

评测分两层：

1. **无需 LLM 调用的确定性指标**（每次 CI 都能跑）：行号合法性、证据可定位性、triage 跳过、维度归因、Validator/Verifier 漏斗、token 用量。
2. **可选语义指标**（调用 LLM judge，消耗 token）：复用 AACR-Bench 官方的文件级语义 P/R / balanced score。

---

## 数据集与切分

使用 [AACR-Bench](https://github.com/alibaba/aacr-bench) 的 Python/Go 子集：

- 19 个 Python PR + 21 个 Go PR = **40 个 PR**
- 覆盖 **10 个仓库**
- 固定的语言分层、仓库互斥切分：**7 dev / 23 test / 10 holdout PR**，分别覆盖
  **2 / 6 / 2 个仓库**；每种语言都出现在每个 split。

原始数据 SHA256 固定为：

```text
02c8561f1c3994a26f427d479b2a3f6e6c8e6858161fbc903b45248fa2cb2097
```

`manifests/aacr_pygo_v3.json` 是提交到仓库的固定索引，包含每个实例的
`instance_id / split / base_commit / head_commit / clone_url`。同一仓库的所有 PR
只会落入一个 split。每种语言各选一个 dev 仓库，再从剩余仓库中选一个 holdout
仓库；所选仓库的 PR 数尽量接近该语言样本总数的 20%，其余仓库全部用于 test。
候选并列时使用 SHA256(seed + split + language + repo) 稳定打破平局，不依赖
Python 版本的随机数实现：

- **dev**：Python、Go 各一个仓库
- **holdout**：与 dev 不重叠，Python、Go 各一个未见仓库
- **test**：剩余仓库；三个 split 都覆盖 Python 与 Go

这使 holdout 可以用于评估未见仓库泛化；dev/test/holdout 之间仍可能有仓库数量与
PR 数量不平衡，结论需同时查看按语言和仓库分层的结果。每个 split 的仓库数很少，
如果某语言少于两个仓库簇，报告会省略该层及总体的仓库 bootstrap 区间，而不是给出
无意义的零宽区间。

---

## 快速开始

所有命令都在项目根目录执行。

### 0. 准备原始数据

如果本地已有 AACR checkout：

```bash
uv run python -m evaluation.cli prepare \
  --source /path/to/aacr-bench/dataset/positive_samples.json
```

没有本地数据时可以让程序下载并校验 SHA256：

```bash
uv run python -m evaluation.cli prepare --download
```

默认写入 `evaluation/manifests/aacr_pygo_v3.json`。

如果用的是本地 fixture 或内网镜像（SHA256 与官方不同），加
`--no-verify-source` 跳过校验。它只影响校验，不会把数据标记成官方版本；
manifest 的 `source.sha256` 会记录实际文件哈希。

### 1. Mock review（不调用真实 LLM）

```bash
uv run python -m evaluation.cli review \
  --split dev \
  --run-id dev-mock \
  --mock-llm
```

首次运行会把 10 个仓库 clone 到 `evaluation/.cache/repos/`。`run_local` 会为每个
PR 创建临时 head/base worktree，review 结束后清理；仓库缓存本身不会被 checkout。

### 2. 确定性指标

```bash
uv run python -m evaluation.cli metrics \
  --split dev \
  --run-id dev-mock
```

输出：

- `evaluation/results/aacr-pygo-v3/dev-mock/<instance_id>.json`
- `evaluation/results/aacr-pygo-v3/dev-mock/summary_openreview.json`
- `evaluation/metrics/aacr-pygo-v3/dev-mock/deterministic_dev-mock.json`
- `evaluation/metrics/aacr-pygo-v3/dev-mock/retrieval_dev-mock.json`

`deterministic_*.json` 覆盖行号合法性、证据可定位性、维度归因与 token 用量；
`retrieval_*.json` 是 Phase 1/2 的检索质量指标，完全不调用 LLM：

### 3. CI 门禁

`gate` 只读取 `deterministic_*.json`，不调用模型，适合作为 CI 的必需检查：

```bash
uv run python -m evaluation.cli gate \
  --metrics evaluation/metrics/aacr-pygo-v3/dev-mock/deterministic_dev-mock.json
```

默认硬阈值：结果不能有 error/missing，未知文件率、非法行号率、未 grounding
的 evidence 率必须为 0，evidence 可核验率和归因覆盖率有下限。阈值可通过 `--min-*` /
`--max-*` 覆盖，但 `results_error` / `results_missing` 不允许放宽。

- `file_recall` / `index_file_recall`：reference comment 指向的文件是否被上下文覆盖
- `symbol_coverage`：diff 里的 changed symbols 有多少被检索回来
- `citation_rate`：finding 是否真的引用了注入的 `context_refs`
- `duplicate_context_rate` / `avg_contexts`：上下文是否重复或膨胀
- `graph_contexts_avg` / `graph_context_kinds`：每个样本由 import/call graph 实际注入的上下文数及类型
- `patch_validation`：`suggested_patch` 有多少能通过 `git apply --check`

做消融时使用 `--ablation` 固定四档，每次使用不同 `--run-id`，再比较
`retrieval_*.json`：

- `--ablation diff`：纯 Diff，不注入检索上下文
- `--ablation ast`：Diff + 单文件 AST 折叠上下文
- `--ablation graph`：AST + 代码图谱（Qualifier + Import）
- `--ablation verifier`：graph 基础 + 后置防幻觉校验

四档也可以一键串起来（review → metrics → gate → 可选 semantic → report）：

```bash
# 可选：预先下载 split 涉及的仓库和所有 base/head commits
uv run python -m evaluation.cli prepare-repos --split dev

# 实际检索上下文并估算 prompt；必要时 clone/fetch，但不调用 LLM
./evaluation/run_ablation.sh --dry-run --split dev --limit 1

# 真实运行四档
./evaluation/run_ablation.sh \
  --split dev \
  --model gpt-4o-mini \
  --preflight-tokens 200000 \
  --run-budget-tokens 20000 \
  --report-out reports/eval_dev.md

# 加跑 AACR 官方语义评测
./evaluation/run_ablation.sh --split dev --semantic \
  --aacr-eval-dir /tmp/aacr-bench/evaluation
```

脚本会为每档生成 `dev-diff / dev-ast / dev-graph / dev-verifier` 四个 run-id；
gate 未通过时会继续跑完其余档位，最后以非零退出码提示。
非 dry-run 会在 review 前按唯一仓库预热仓库与所需 commits；若目标 commit 已缓存，
后续评测不再重复 fetch。启用 `--semantic` 时，脚本先验证已有逐样本结果的状态、
manifest、ablation、模型和代码工件版本；完整匹配的档位直接复用，不匹配/缺失的样本
才补跑。旧结果没有这些元数据时不会被自动复用，以免混淆模型或配置。

### 4. 真实模型（按需，消耗 token）

```bash
export OPENAI_API_KEY=...

# 实际检索上下文并估算 prompt；可能 clone/fetch 仓库，但不调用 LLM
uv run python -m evaluation.cli review \
  --split dev \
  --limit 1 \
  --dry-run \
  --dry-run-output reports/context-dry-run.json

# 再跑一条 smoke，确认 API、token 预算和结果落盘
uv run python -m evaluation.cli review \
  --split dev \
  --limit 1 \
  --run-id dev-gpt4o-mini-smoke \
  --model gpt-4o-mini \
  --preflight-tokens 20000 \
  --run-budget-tokens 20000

# 完整 dev
uv run python -m evaluation.cli review \
  --split dev \
  --run-id dev-gpt4o-mini \
  --model gpt-4o-mini \
  --preflight-tokens 200000 \
  --run-budget-tokens 20000
```

两个 token 参数语义不同：

- `--dry-run` 会确保仓库和 base/head commit 可用（必要时 clone/fetch），创建临时
  worktree 并执行真实检索、上下文规划和 prompt 构建；不会调用 LLM，也不会写评测结果。
  JSON 报告逐段包含 context 文本、类型、优先级、片段/注入段 token 估算，以及每个
  batch 的 system/user/final prompt token 估算。文本会按实际 prompt 路径脱敏。
  `estimate_tokens` 是启发式估算，不是 provider tokenizer；真实 token 用量只有模型响应后可知。
- `--preflight-tokens` 是 **split 级预检**：用 manifest 的变更行数/评论数，
  并按 ablation 档位粗略加上检索上下文 token，超过上限就在任何 LLM 调用前退出。
  它是 lower bound：不模拟 batching、重试和真实 `max_context_tokens` 截断。
- `--run-budget-tokens` 是 **单个 PR run 的运行时硬上限**：每个 PR 独立计账，
  估算输入超过该值就停止该 run 的后续调用，并把它标记为 `partial`。

没有设置 `--preflight-tokens` 时不要直接跑 `--split all`。

### 5. 可选：AACR 官方语义评测

```bash
# 1) clone AACR（只需一次）
git clone --depth 1 https://github.com/alibaba/aacr-bench.git /tmp/aacr-bench

# 2) 配置 judge（不配 API key 时 AACR 会退化为本地 mock judge）
export JUDGE_BASE_URL=...
export JUDGE_API_KEY=...
export JUDGE_MODEL=...

# 3) 调用官方 evaluate.py
uv run python -m evaluation.cli semantic \
  --split dev \
  --run-id dev-gpt4o-mini \
  --aacr-eval-dir /tmp/aacr-bench/evaluation
```

这里没有复制 AACR 的 judge 代码：`evaluation/semantic.py` 把它的 `evaluation/`
目录加入 `sys.path`，直接调用 `evaluate.evaluate(...)`。我们写出的结果 JSON 使用
AACR OCR 兼容字段（`review.comments[].content`、`review.summary.input_tokens`），
`reviewer="openreview"` 会走 AACR 的 OCR builder 分支。

如果 AACR 目录不存在，跳过这一步即可；确定性指标仍然可用。

### 6. 汇总多次 run

`report` 子命令会把 `deterministic_*.json`、`retrieval_*.json` 和可选的
`metrics_openreview_*.json` 合成 Markdown 表格，方便贴到 README / PR / 面试材料：

```bash
uv run python -m evaluation.cli report \
  --run diff=evaluation/metrics/aacr-pygo-v3/dev-diff \
  --run ast=evaluation/metrics/aacr-pygo-v3/dev-ast \
  --run graph=evaluation/metrics/aacr-pygo-v3/dev-graph \
  --run verifier=evaluation/metrics/aacr-pygo-v3/dev-verifier \
  --out reports/eval_dev.md
```

- 报告展示 P/R、检索覆盖、citation、graph context、调用次数和延迟；有 AACR
  逐样本 judge 记录时还会展示语言/仓库分层语义指标
- 单个 run 的 95% 区间按语言分层、以仓库为簇有放回抽样；多 run 共同 PR 的配置
  差异另用语言分层的配对 PR bootstrap 区间。任一语言少于两个仓库簇/配对 PR 时
  不报告区间。随机 seed 固定为 42，结果可复现。
- 语义文件默认在 metrics 目录里自动查找最新的 `metrics_openreview_*.json`
- 也可以显式指定：`--semantic single=/path/to/metrics_openreview_xxx.json`

---

## 结果 JSON 约定

每个实例写一个 `<instance_id>.json`：

```json
{
  "instance_id": "owner__repo@abc1234",
  "repo": "owner/repo",
  "base_commit": "...",
  "head_commit": "...",
  "reviewer": "openreview",
  "status": "ok",
  "duration_seconds": 12.3,
  "review": {
    "comments": [
      {
        "path": "src/app.py",
        "start_line": 12,
        "end_line": 12,
        "content": "reasoning\n\nSuggestion: ...",
        "severity": "high",
        "category": "bugs",
        "confidence": 0.9,
        "source_aspects": ["bugs", "security"],
        "evidence": "return a + b",
        "suggested_patch": null
      }
    ],
    "summary": {"input_tokens": 1000, "output_tokens": 200, "llm_calls": 4},
    "metrics": {"...": "完整 pipeline metrics"}
  }
}
```

额外字段（`severity/category/confidence/source_aspects/evidence/suggested_patch`）
AACR 会忽略，但本地指标和人工分析会用到。

失败实例也会落盘，`status="error"` 且带 `error`，这样指标里能精确区分“没跑”
和“跑了但失败”，而不是静默丢样本。

---

## 确定性指标定义

这些指标不调用任何模型，适合 CI 和回归对比。实现见 `evaluation/deterministic.py`。

### validity

- `invalid_line_rate`：最终 finding 的行号不在该文件 diff 新侧可见行内 / 总 findings
- `missing_line_rate`：finding 没有行号 / 总 findings
- `unknown_file_rate`：finding 指向的文件不在本次 diff / 总 findings

validator 已经把非法行号置空、把幻觉文件丢掉，所以这三个值正常情况下应接近 0。
如果升高，通常说明 verifier 或聚合逻辑把脏数据带回来了。

### evidence

- 新口径在 `evidence.schema_version = 2` 标识
- `coverage`：严格按 finding 的文件、LEFT/RIGHT 侧和闭区间行号取对应 commit 的源码，
  evidence 经空白归一化后能在该行范围内精确定位 / 已检查 evidence 数
- `evaluable_rate`：能做上述检查的 evidence 数 / 有 evidence 的 finding 数
- `ungrounded`：行号和源码均可检查，但引用文本不匹配的 evidence 数
- `unevaluable` / `unevaluable_reasons`：无法核验的 evidence 数及原因（diff 不可用、
  未知文件、行号缺失/非法/不在 diff、源码不可用）；不再把这些样本混入 grounded/
  ungrounded 的分母
- `missing_rate`：没有 evidence / 总 findings

这项指标有意比 pipeline Validator 更严格：不使用附近代码窗口、整份 patch 或
token overlap 作为证据；大小写敏感，仅折叠空白。它只验证“引用代码是否逐字出现于
所标行”，不判断引用是否足以支持 finding 的语义结论。CI gate 对新格式指标同时
要求 `evaluable_rate` 至少 0.8，避免只在少数可核验项上获得高 coverage。

### triage

- `skip_rate`：`metrics.triage.skipped_llm == true` 的实例 / 成功实例数
- `reasons`：跳过原因分布

### attribution

- `coverage`：带 `source_aspects` 的 findings / 总 findings
- `aspect_counts`：每个来源贡献了多少 findings

单遍模式下没有独立维度，`source_aspects` 使用 finding 自己的 `category`
归因。

### retrieval

- `contexts`：所有成功实例累计注入的 context 数量
- `mean_changed_file_coverage`：每个实例“被 context 覆盖的变更文件数 / 变更文件总数”
  的平均值

它回答的是“检索器有没有把改动文件带进上下文”，不是“带进来的上下文有没有用”。
后者需要人工抽查或语义评测。

### validator

直接累加 pipeline `metrics.validation` 的计数器：

`candidates / kept / dropped_unknown_file / invalid_line / missing_line /
missing_evidence / ungrounded_evidence / grounded_evidence / low_confidence /
deduplicated`

多批次 review 会逐批累加，不会只保留最后一批。

### verification

`metrics.verification` 是每次 review 的结构化拦截漏斗，并带有独立的
`schema_version`。其 `stages` 记录 `raw_count`、`validator_kept`、
`verifier_candidates` 和 `final_count`；`drops` 记录 Validator 与 Verifier
各阶段互斥的淘汰原因；`events` 记录无效 patch 被剥离、证据未匹配、行号修正等
可能不淘汰 finding 的事件。Verifier 未选中的 finding 仍会保留，因此
`verifier_candidates` 是 `validator_kept` 的子集，不是阶段差额。
`conservation` 报告 Validator 与 Verifier 阶段数量关系是否守恒；无法采集完整
Validator 计数时 `available=false`，不计算阶段守恒。
`by_diff_scope` 汇总未知文件、diff 外行号和默认过滤的历史 finding；
`deduplicated` 包含 Validator 内及跨 batch 去重。当前 evidence 不匹配会降低置信度，
并非独立淘汰规则，因此相关淘汰计入 `by_low_confidence`，而
`by_evidence_mismatch` 暂为 0；`ungrounded_evidence` 保留为诊断事件。

确定性评测按样本保留该对象，并在整体层面对计数先求和、再计算留存率和拒绝率。
只有 `available=true` 且嵌套 schema 版本受支持的样本才参与整体汇总；旧结果若没有
此字段、或自定义 Reviewer 未提供 Validator 计数，不会被假定为零值。不同的嵌套
schema 版本会分开标记，避免混合统计口径。根级 `metrics.schema_version` 用于标识
整份 review metrics 的 schema。

### cost（token / 调用量，不含价格）

- `llm_calls`、`input_tokens`、`output_tokens`
- `duration_seconds`
- 平均值按成功实例数计算
- `cost` 只是历史字段名，里面不包含货币金额

---

## 消融开关

`review` 子命令只暴露一个固定消融开关：

```bash
# 四档消融，相邻档位只差一个变量
--ablation diff      # 纯 Diff
--ablation ast       # + AST Folding（单文件局部上下文）
--ablation graph     # + 代码图谱（Qualifier + Import）
--ablation verifier  # + Verifier（后置防幻觉校验）

# 模型档位
--model gpt-4o-mini
```

建议按 `diff → ast → graph → verifier` 顺序逐档对照，每档使用同一份
LLM record/replay 缓存。不要一次性展开太多维度，否则 40 个 PR 的样本量撑不住结论。

### 录制 / 回放，避免重复付费

```bash
# 第一次：真实调用并写入 LLM 缓存
uv run python -m evaluation.cli review \
  --split dev --run-id dev-record \
  --model gpt-4o-mini \
  --llm-cache-dir evaluation/.cache/llm

# 后续：只回放已录制的响应；缺任何一条都会让该实例失败
uv run python -m evaluation.cli review \
  --split dev --run-id dev-replay \
  --model gpt-4o-mini \
  --llm-cache-dir evaluation/.cache/llm \
  --llm-cache-mode replay

# 复用完整 review 结果（连后处理都不重跑）
uv run python -m evaluation.cli review \
  --split dev --run-id dev-cached --review-cache
```

`--llm-cache-mode auto` 是默认值：有缓存就读，没有就调用并写入。缓存 key 包含
模型名、schema、system/user prompt；修改 prompt 或模型后不会误命中旧结果。

---

## 复现与离线镜像

- manifest 固定了 seed、数据 SHA256、每个实例的 base/head commit。
- 如果公司内网或 CI 不能访问 GitHub，可在原始数据/自定义 manifest 中给实例加
  `clone_url`（本地路径或镜像 URL），`ensure_repo` 会优先使用它。
- 仓库缓存默认在 `evaluation/.cache/repos/`，不会提交；删除后下次会重新 clone。
- `--keep-worktrees` 会保留 head/base worktree，便于排查失败样本，但用完请手动清理。

---

## 目录结构

```text
evaluation/
  aacr.py            # 原始数据校验、Python/Go 过滤、确定性 manifest
  repos.py           # clone / fetch / 按 commit 读文件
  results.py         # run_local + AACR OCR 兼容结果 JSON
  deterministic.py   # 确定性指标
  retrieval.py       # 检索 / graph 指标（零 LLM 调用）
  semantic.py        # 可选：AACR 官方 LLM judge 适配
  report.py          # 多次 run -> Markdown Results 表
  cli.py             # prepare / review / metrics / semantic / report
  manifests/
    aacr_pygo_v3.json
```

---

## 已知边界

- Mock LLM 只验证链路，不代表 review 质量；不要用 mock 指标做模型对比。
- 确定性指标是工程护栏，不替代 AACR 语义 P/R；两者应一起看。
- 当前 `tree_sitter_retriever.py` / `impact.py` 内部仍会调用 `git grep`，所以不要
  宣称“完全不用 grep”；这里的“无独立 grep retriever”指的是没有单独的 grep
  检索器 fallback。
- call graph 是 best-effort：Python 用 `ast` 抽取，Go 调用边暂不默认启用；它只补充
  candidate context，不保证完整类型解析。
- `graph_contexts_avg` 统计的是最终注入的 caller/callee/import/dependency 上下文，
  不是候选边数量；候选命中数仍保留在 `stats` 里。
- 评测是串行的。同一 repo 反复建 worktree 时并发收益有限，但更容易把临时目录
  和磁盘打爆；需要提速时优先按 repo 分组并行。
