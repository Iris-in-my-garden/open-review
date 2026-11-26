# 08 · 评测方法

评测回答一个问题：**在真实 PR 上，OpenReview 的审查结果是否变好了？**

评测不经过 GitHub API、不发评论、不依赖 PR 是否仍开放。输入只有：仓库、`base_commit` / `head_commit`、人工标注的 reference comments。

## 1. 数据集与切分

使用 [AACR-Bench](https://github.com/alibaba/aacr-bench) 的 Python/Go 子集：

| 项 | 值 |
|---|---:|
| 原始 PR 总数 | 43（21 Python + 22 Go） |
| 不可用 pinned head（已排除） | 3 |
| manifest PR 总数 | 40（19 Python + 21 Go） |
| 仓库数 | 10 |
| dev / holdout | 20 / 20 PR |
| 仓库覆盖 | 6 / 4 |
| 语言覆盖 | dev 10 Go + 10 Python；holdout 11 Go + 9 Python |
| 原始数据 SHA256 | `02c8561f...2cb2097` |

切分规则：

- **按仓库互斥**：同一仓库的所有 PR 只属于一个 split，避免同仓库泄漏。
- **两种语言都出现在两个 split**，并尽量让 PR 数接近 5:5。
- 每种语言按仓库整体做子集和，选择 PR 数最接近该语言一半的仓库子集作为 holdout；
  并列时优先仓库数更少（把更多仓库留给 dev），再按仓库名排序稳定打破平局。
- 不再保留独立的 `test` split：dev 用于调参和四档迭代，holdout 只在配置冻结后跑一次。
- 每个 split 仓库数很少（holdout 只有 4 个仓库簇）；某语言少于两个仓库簇时，
  报告省略该层 bootstrap 区间，而不是给零宽区间。
- **语言范围过滤**：评测按 manifest `language` 过滤 diff，只保留主语言源码
  （Go → `.go`；Python → `.py`/`.pyi`）。多语言仓库里的 C/C++/CUDA/TS
  vendored 代码不进入 Go/Python 评测队列；文件过滤器再单独跳过
  snapshot/testdata/generated/vendored 文件。
- **reference comments 同样过滤**：semantic judge 和 retrieval file recall 只使用
  主语言 reference comments，避免 C/C++/CUDA/notebook/doc 标注变成不可达的
  false negative。

## 2. 两层指标

### 2.1 确定性指标（不调用 LLM，可进 CI）

| 指标 | 定义 | 期望 |
|---|---|---|
| `unknown_file_rate` | finding 指向未知文件的比例 | 0 |
| `invalid_line_rate` | 行号不在 diff 可见行的比例 | 0 |
| `missing_line_rate` | 未给行号的比例 | 越低越好 |
| evidence coverage | evidence 能在 patch/context/源码中定位的比例 | 越高越好 |
| evidence missing rate | 缺 evidence 的比例 | 越低越好 |
| triage skip rate | 被规则跳过的比例 | 与 docs-only 比例一致 |
| attribution coverage | finding 能归因到审查维度的比例 | 越高越好 |
| validator/verifier funnel | candidates → kept → verified → final | 观察丢弃原因 |
| token usage | patch/context/输入/输出 | 成本对照 |

### 2.2 检索指标（不调用 LLM）

| 指标 | 定义 |
|---|---|
| `file_recall` | reference comment 指向的文件是否被上下文覆盖 |
| `index_file_recall` | 仅结构化索引产出的覆盖 |
| `symbol_coverage` | changed symbols 被检索回来的比例 |
| `citation_rate` | finding 是否真的引用了注入的 `context_refs` |
| `duplicate_context_rate` | 重复上下文比例 |
| `avg_contexts` | 平均注入上下文数 |
| `graph_contexts_avg` | 平均 graph 上下文数 |
| `graph_context_kinds` | caller/callee/import/dependency 分布 |
| `patch_validation` | `suggested_patch` 通过 `git apply --check` 的比例 |

### 2.3 语义指标（可选，调用 LLM judge）

复用 AACR-Bench 官方的**文件级语义 P/R / balanced score**。语义评测消耗 token，结果需与确定性指标一起报告。

## 3. 四档消融

只保留四个可解释配置，相邻两档只差一个变量：

| 档位 | 检索 | Verifier | triage/impact/static |
|---|---|---|---|
| `diff` | 无 | 关 | 全关 |
| `ast` | 单文件 AST | 关 | 全关 |
| `graph` | AST + 代码图谱 | 关 | 全关 |
| `verifier` | AST + 代码图谱 | 开 | 全关 |

```bash
# 一键跑四档：review -> metrics -> gate -> 可选 semantic -> report
# 默认 review=deepseek-v4-pro thinking，verifier=deepseek-flash
./evaluation/run_ablation.sh --split dev \
  --preflight-tokens 200000 --run-budget-tokens 20000

# 只估算，不 clone、不调用 LLM
./evaluation/run_ablation.sh --dry-run --split dev
```

也可以逐档手动跑：

```bash
uv run python -m evaluation.cli review --split dev --run-id dev-diff --ablation diff
uv run python -m evaluation.cli review --split dev --run-id dev-ast --ablation ast
uv run python -m evaluation.cli review --split dev --run-id dev-graph --ablation graph
uv run python -m evaluation.cli review --split dev --run-id dev-verifier --ablation verifier
```

两个 token 参数语义不同：

- `--preflight-tokens`：split 级预检，按 manifest 与 ablation 档位粗略估算整批 token（lower bound），超限则在调用 LLM 前退出。
- `--run-budget-tokens`：单个 PR run 的运行时输入 token 硬上限，超限后该 run 转为 `partial`。

每档必须同时报告：语义质量、检索覆盖、调用数、token、延迟、不确定性。

## 4. CI Gate

`gate` 只读 `deterministic_*.json`，不调用模型：

```bash
uv run python -m evaluation.cli gate \
  --metrics evaluation/metrics/aacr-pygo-v3/dev-mock/deterministic_dev-mock.json
```

默认硬阈值：

- `results_error = 0`、`results_missing = 0`（不可放宽）
- `unknown_file_rate = 0`
- `invalid_line_rate = 0`
- 未 grounding evidence 率 = 0
- evidence 可核验率、归因覆盖率有下限

阈值可通过 `--min-*` / `--max-*` 覆盖，但 error/missing 不允许放宽。

## 5. 当前状态（重要）

| 项 | 状态 |
|---|---|
| 评测框架 | ✅ 已就绪 |
| 固定样本 | ✅ 40 PR / 10 仓库（3 个不可用 head 已排除） |
| 仓库互斥切分 | ✅ dev 20 / holdout 20 |
| 确定性指标 | ✅ 已实现 |
| CI gate | ✅ 已实现 |
| 四档消融 | ✅ 已实现 |
| **真实模型质量/成本结果** | ❌ **尚未完成** |

现有 `evaluation/metrics/aacr-pygo-v1/*` 是 **mock 或单条 dev 样本**，只用于验证链路，不代表审查质量。README 与架构文档中的消融表留白，**空白不是 0，也不是结论**。

在补齐真实结果前，不应对外声称“检索提升 X%”或“Verifier 降低幻觉 Y%”。

## 6. 复现步骤

```bash
# 0. 准备 manifest（已有可跳过）
uv run python -m evaluation.cli prepare --download

# 1. Mock 链路（不调用真实 LLM）
uv run python -m evaluation.cli review --split dev --run-id dev-mock --mock-llm

# 2. 确定性 + 检索指标
uv run python -m evaluation.cli metrics --split dev --run-id dev-mock

# 3. CI gate
uv run python -m evaluation.cli gate \
  --metrics evaluation/metrics/aacr-pygo-v3/dev-mock/deterministic_dev-mock.json

# 4. 真实模型（按需）
export DEEPSEEK_API_KEY=...
uv run python -m evaluation.cli review --split dev --limit 1 \
  --run-id dev-smoke --preflight-tokens 20000 --run-budget-tokens 20000
```

默认 review 用 `deepseek-v4-pro` + thinking，verifier 用 `deepseek-flash`；
换模型用 `--model` / `--verifier-model` 覆盖。
完整四档对照直接跑 `./evaluation/run_ablation.sh --split dev`。

## 7. 统计注意事项

- 样本量小（40 PR，holdout 20 PR / 4 仓库），置信区间会很宽；必须按语言/仓库分层报告。
- 同一仓库的 PR 不独立；按仓库聚类做 bootstrap，而不是按 PR 独立采样。
- 语义 judge 自身有偏差；应固定模型、温度、prompt，并报告 judge 版本。
- 不要只报总分；同时报质量、覆盖、调用、token、延迟和失败率。
- 负结果同样有价值：如果 `graph` 相比 `ast` 没有稳定收益，应如实报告并调整设计。

## 8. 后续优先项

1. 在 dev/holdout 上跑完四档消融，发布分层结果与不确定性。
2. 校准 token 估算误差，补金额成本。
3. 建立用户反馈闭环（评论 accept/dismiss），跟踪 precision。
4. 扩展语言与符号关系，优先覆盖漏检案例。
