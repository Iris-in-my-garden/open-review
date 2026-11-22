# ADR-0005：`partial` 是一等状态

- 状态：Accepted
- 日期：2026-11
- 影响版本：`ReviewStatus` / `ReviewOutcome`
- 相关代码：`src/openreview/review/workflow.py`、`services/outcome_resolver.py`

## 上下文

早期只要有一个 batch 成功就会生成报告。调用方无法区分：

- 完整成功；
- 部分 batch 失败；
- 预算耗尽导致后续批次未执行；
- triage 主动跳过。

这会让评测把降级结果当成完整成功，也会让 CI 误判。

## 决策

引入四态 outcome：

| 状态 | 条件 | exit code |
|---|---|---:|
| `completed` | 无失败 batch、无预算耗尽 | 0 |
| `skipped` | triage 判定无需 LLM | 0 |
| `partial` | 有失败 batch 或预算耗尽 | 2 |
| `failed` | 致命错误/无报告 | 1 |

并配套：

- `degraded_reasons` 记录每个降级原因；
- `failed_batches` / `budget_exhausted` 进入 metrics；
- 只缓存 `ok`（completed/skipped）的结果，避免 partial 被下次 run 命中；
- 工作流整体失败时尽力写 metrics/debug，再抛原错误。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 只有 success/failure | 无法表达“部分结果可用” |
| 用异常表达 partial | CLI/评测难以稳定处理 |
| 把 partial 写入缓存 | 下次 run 会把不完整结果伪装成完整成功 |

## 后果

正面：

- CLI、评测、CI 都能区分完整与降级；
- 报告会明确写“结果不完整”；
- 缓存语义安全。

负面：

- 调用方需要处理 exit code 2；
- 状态判断分散在 executor/resolver，需要测试覆盖；
- partial 结果不能复用，重复 run 成本更高。

## 证据

- `ReviewOutcome.exit_code` 映射 0/1/2；
- `finalize` 只在 `outcome.ok` 时写 review cache；
- `execute` 在异常路径尽力持久化 metrics 与 debug state。

## 复审条件

当引入“可恢复的断点续跑”时，可以把 partial 细分为“可续跑”和“不可续跑”，但仍需保持对调用方的显式语义。
