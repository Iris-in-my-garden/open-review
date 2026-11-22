# ADR-0003：预算下沉到 LLM 客户端

- 状态：Accepted
- 日期：2026-11
- 影响版本：`BudgetedLLMClient`
- 相关代码：`src/openreview/review/llm_client.py`、`review/workflow.py`

## 上下文

早期预算在 `ReviewExecutor._node_analyze` 里按“维度数”记账。问题是：

- provider 重试不扣调用数；
- verifier 的额外调用不扣；
- 超时/异常后的保守计费缺失；
- 并发 aspect 可能同时通过检查，把硬预算变成软预算；
- 自定义 reviewer 绕过客户端时账目不一致。

## 决策

把预算放进 `BudgetedLLMClient`，包住每一次 provider attempt：

- `reserve(calls=1, tokens=estimate, output=reservation)` 原子检查 + 扣减；
- 输出预留同时作为 `max_completion_tokens`；
- 成功/失败后用 provider usage `settle`；
- 未发出的请求 `refund`；
- 超时取 `min(配置超时, 剩余时间预算)`；
- SDK 隐式重试关闭，每次 attempt 独立计账；
- 每个 `ReviewSession` 绑定独立 budget，metrics 按 `run_id` 汇总。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 节点层按维度记账 | 重试/verifier/并发路径漏记 |
| 只在调用后统计 | 无法阻止超额调用，只能事后发现 |
| 共享全局计数器 | 并发 run 互相污染，无法归属 |
| 精确 tokenizer + 账单 | 模型/Provider 差异大，先做硬护栏 |

## 后果

正面：

- 主审查、verifier、重试共享同一本账；
- 并发下仍是硬上限；
- 预算耗尽可解释地转为 `partial`；
- metrics 可还原每次调用的 run_id/attempt/usage。

负面：

- 输入 token 是估算，当前请求可能超限（后续调用被拦截）；
- 输出上限依赖 Provider 遵守参数；
- 时间预算只覆盖 LLM 请求。

## 证据

- `ReviewBudget.reserve` 在 `RLock` 内完成检查 + 扣减；
- `BudgetedLLMClient` 在 `BudgetExceeded` 时 `refund`，其他异常按真实/预留 `settle`；
- `tests/test_budgeted_client.py`、`tests/test_workflow.py` 覆盖并发与边界。

## 复审条件

当接入精确 tokenizer、Provider 计费 API 或集中式配额服务时，可把“估算护栏”升级为“账单对齐”，但保留客户端层作为唯一入口。
