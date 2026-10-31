# OpenReview 离线评测（AACR-Bench · Python/Go）

这套工具用于回答一个具体问题：**在真实 PR 上，OpenReview 的 review 结果是否变好了？**

它不经过 GitHub API、不发评论、也不依赖 PR 是否还开着。输入只有三样：

- 一个仓库（本地缓存或任意 `clone_url`）
- `base_commit` / `head_commit`
- 人工标注的 reference comments

评测分两层：

1. **零成本确定性指标**（每次 CI 都能跑）：行号合法性、证据可定位性、triage 跳过、维度归因、validator 统计、成本。
2. **可选语义指标**（调用 LLM judge，花钱）：复用 AACR-Bench 官方的文件级语义 P/R / balanced score。

---

## 数据集与切分

使用 [AACR-Bench](https://github.com/alibaba/aacr-bench) 的 Python/Go 子集：

- 21 个 Python PR + 22 个 Go PR = **43 个 PR**
- 覆盖 **10 个仓库**
- 固定切分：**10 dev / 25 test / 8 holdout**

原始数据 SHA256 固定为：

```text
02c8561f1c3994a26f427d479b2a3f6e6c8e6858161fbc903b45248fa2cb2097
```

`manifests/aacr_pygo_v1.json` 是提交到仓库的固定索引，包含每个实例的
`instance_id / split / base_commit / head_commit / clone_url`。切分算法使用
`SHA256(seed + instance_id)` 排序，而不是 `random.shuffle`，因此不依赖 Python
版本的随机数实现：

- **dev**：每个 repo 取 1 条，保证 5 Python + 5 Go 仓库都有覆盖
- **holdout**：每种语言最多 4 个仍有剩余样本的 repo，各取 1 条
- **test**：其余全部

注意：这是 **PR 级 holdout**，不是 repo 级 holdout。同一个仓库的代码风格会同时
出现在 dev/test 中，结论更偏向“同仓库新 PR 的泛化”，不要解读成“未见仓库泛化”。

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

默认写入 `evaluation/manifests/aacr_pygo_v1.json`。

如果用的是本地 fixture 或内网镜像（SHA256 与官方不同），加
`--no-verify-source` 跳过校验。它只影响校验，不会把数据标记成官方版本；
manifest 的 `source.sha256` 会记录实际文件哈希。

### 1. Mock review（零成本链路验证）

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

- `evaluation/results/aacr-pygo-v1/dev-mock/<instance_id>.json`
- `evaluation/results/aacr-pygo-v1/dev-mock/summary_openreview.json`
- `evaluation/metrics/aacr-pygo-v1/dev-mock/deterministic_dev-mock.json`
- `evaluation/metrics/aacr-pygo-v1/dev-mock/retrieval_dev-mock.json`

`deterministic_*.json` 覆盖行号合法性、证据可定位性、维度归因与成本；
`retrieval_*.json` 是 Phase 1 的检索质量指标，完全不调用 LLM：

- `file_recall` / `index_file_recall`：reference comment 指向的文件是否被上下文覆盖
- `symbol_coverage`：diff 里的 changed symbols 有多少被检索回来
- `citation_rate`：finding 是否真的引用了注入的 `context_refs`
- `duplicate_context_rate` / `avg_contexts`：上下文是否重复或膨胀
- `patch_validation`：`suggested_patch` 有多少能通过 `git apply --check`

做检索消融时加 `--no-hybrid-retrieval`，关闭 FTS5 符号索引，只保留 tree-sitter
AST 检索；两次 run 使用不同 `--run-id`，再比较 `retrieval_*.json`。

### 3. 真实模型（按需，产生费用）

```bash
export OPENAI_API_KEY=...
uv run python -m evaluation.cli review \
  --split dev \
  --run-id dev-gpt4o-mini \
  --model gpt-4o-mini
```

建议先只跑 `--split dev --limit 1` 验证 API 与预算，再跑完整 dev。按当前子集规模，
`gpt-4o-mini` 的 dev 成本大致在 **几美元**；完整 43 个 PR 约 **10–20 美元**，
实际取决于 diff 大小和重试次数。没有预算时不要直接跑 `--split all`。

### 4. 可选：AACR 官方语义评测

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

- `coverage`：有 evidence 且能在 head 源码或 patch 中定位 / 已检查 evidence 数
- `missing_rate`：没有 evidence / 总 findings

evidence 定位规则与 validator 类似：短文本用归一化子串匹配，长文本用 token
overlap ≥ 0.8。它不是语义判断，只回答“引用的代码是否真的存在”。

### triage

- `skip_rate`：`metrics.triage.skipped_llm == true` 的实例 / 成功实例数
- `reasons`：跳过原因分布

### attribution

- `coverage`：带 `source_aspects` 的 findings / 总 findings
- `multi_aspect_rate`：被多个维度同时命中的 findings / 总 findings
- `aspect_counts`：每个来源贡献了多少 findings

单遍模式下没有独立维度，`source_aspects` 使用 finding 自己的 `category`
归因；多维度模式下则是实际运行维度。跨模式下对比时请先看清 `reviewer_mode`。

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

### cost

- `llm_calls`、`input_tokens`、`output_tokens`
- `duration_seconds`
- 平均值按成功实例数计算

---

## 消融开关

`review` 子命令直接暴露了当前需要验证的开关：

```bash
# reviewer 策略：默认单遍；multi_aspect 仅用于高召回/消融
--reviewer-mode single
--reviewer-mode multi_aspect

# verifier on/off
--no-verifier

# 跨文件 impact / tool-loop on/off
--no-impact

# 规则 triage on/off
--no-triage

# 模型档位
--model gpt-4o-mini
```

建议保持三个主消融：**reviewer 策略、verifier、impact/tool-loop**，模型档位和
triage 作为补充。不要一次性展开太多维度，否则 43 个 PR 的样本量撑不住结论。

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
  deterministic.py   # 零成本指标
  semantic.py        # 可选：AACR 官方 LLM judge 适配
  cli.py             # prepare / review / metrics / semantic
  manifests/
    aacr_pygo_v1.json
```

---

## 已知边界

- Mock LLM 只验证链路，不代表 review 质量；不要用 mock 指标做模型对比。
- 确定性指标是工程护栏，不替代 AACR 语义 P/R；两者应一起看。
- 当前 `tree_sitter_retriever.py` / `impact.py` 内部仍会调用 `git grep`，所以不要
  宣称“完全不用 grep”；这里的“无独立 grep retriever”指的是没有单独的 grep
  检索器 fallback。
- 评测是串行的。同一 repo 反复建 worktree 时并发收益有限，但更容易把临时目录
  和磁盘打爆；需要提速时优先按 repo 分组并行。
