# ADR-0007：规则 triage，不用模型

- 状态：Accepted
- 日期：2026-11
- 影响版本：`review/triage.py`
- 相关代码：`src/openreview/review/triage.py`、`services/review_executor.py`
- 补充：[ADR-0004](ADR-0004-single-pass-per-batch.md) 的单遍审查

## 上下文

triage 要回答的是“这个 PR 值不值得调用 LLM、应该重点看什么”。如果 triage 自己
也调用 LLM，就会出现两个问题：

- 判断“要不要调用 LLM”本身产生一次 LLM 调用，调用量不成立；
- 引入不可解释、不可复现的判断，评测时无法区分 triage 与 review 的收益。

## 决策

triage 只用确定性规则：

- `docs-only`：全部是文档/配置，直接跳过 LLM，返回 `skipped`；
- `test-only`：只改测试，收窄审查重点，不跳过；
- 其他：运行完整审查。

triage 在所有消融档位固定开启，不作为独立消融维度。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| LLM triage | 多一次调用，且不可解释 |
| 不做 triage | docs-only PR 仍消耗 review 预算 |
| 把 triage 作为消融维度 | 与检索/verifier 的归因混淆 |
| 按文件数/行数硬跳过 | 大 PR 可能正是最有价值的审查对象 |

## 后果

正面：

- 零 LLM 成本、确定性、易测试；
- docs-only PR 的跳过语义可进入 metrics/outcome；
- 消融只比较检索/verifier，归因更干净。

负面：

- 规则可能误跳“看起来像文档、实际含代码片段”的 PR；
- test-only 的“重点收窄”仍是启发式；
- 当前评测集 triage skip rate 为 0，规则价值在真实流量中更明显。

## 证据

- `triage()` 的规则分支与 `build_skipped_result`；
- `TriageDecision.aspects` 进入 prompt focus；
- `tests/test_assessment.py` / workflow 测试覆盖跳过与收窄。

## 复审条件

当真实流量中出现大量“规则无法判断、LLM 判断明显更好”的 PR，且离线评测能
量化 triage 的增益时，可以把 LLM triage 作为**独立消融档位**引入，而不是默认开启。
