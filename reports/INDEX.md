# Reports 索引

`reports/` 只保留当前结论和面试材料，过期/中间产物放在 `reports/archive/`。

## 当前保留

| 文件 | 用途 |
|---|---|
| `eval_dev_production_v2.md` | full dev production（20 PR）；semantic 为 current，evidence 70.2% 是锚定修复前数字，修复后 100% 见 `docs/evaluation-results.md` |
| `eval_mini_ablation.md` | 3-PR 四档 mini ablation |
| `diff_line_count_check.md` | 三点 diff / reference path/line 校验 |
| `microsoft_typescript-go_pr_1354_review.md` | 单 PR 示例报告，用于 demo |

## 归档（不作为结论）

| 文件 | 原因 |
|---|---|
| `archive/eval_dev_production_invalid_two_dot.md` | 两点 diff 时期的旧结果，已被 v2 取代 |
| `archive/mini-ablation/*.md` | per-repo 中间报告，汇总见 `eval_mini_ablation.md` |

## 非结论文件

- `context-dry-run*.json`：上下文/prompt 预算的 dry-run 产物，不调用 LLM。
- `tune6/`：6-PR prompt 实验的 shard/summary/log，附录用。
- `debug/`：本地排障产物。

结果数字的单一事实源是 [`../docs/evaluation-results.md`](../docs/evaluation-results.md)。
