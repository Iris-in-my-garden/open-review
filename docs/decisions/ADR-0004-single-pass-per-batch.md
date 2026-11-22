# ADR-0004：每个 batch 一次结构化调用

- 状态：Accepted
- 日期：2026-10
- 影响版本：`SingleShotReviewer`
- 相关代码：`src/openreview/review/single_shot.py`、`review/prompt_builder.py`

## 上下文

早期实现按 aspect 并行审查（clean_code / bugs / performance / security），每个维度一次调用。问题是：

- 调用量约为单遍的四倍；
- 多份结果需要额外聚合与去重；
- 在没有证据表明“四倍调用带来成比例收益”前，成本和延迟不可接受；
- 评测维度变多，难以判断收益来自哪里。

## 决策

每个 batch 用**一次**结构化调用覆盖五类风险（bugs / security / performance / clean_code / test_gap），在输出中用 `category` 区分。

“single-shot”指**每个 batch 一次调用**，不是整个 PR 只调用一次；大 PR 仍由 `plan_review_batches` 拆批。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 每 aspect 一次调用 | 约四倍调用量，聚合成本高，收益未证明 |
| 每文件一次调用 | 调用量随文件数爆炸，跨文件证据被割裂 |
| 整个 PR 一次调用 | 超上下文窗，长 PR 必然截断 |

## 后果

正面：

- 成本与延迟显著下降；
- 输出结构统一，Validator/Verifier 只需处理一种 schema；
- 评测可以固定“每 batch 1 次调用”，成本可预测。

负面：

- 类别注意力竞争，不能假设效果与多轮审查相同；
- 预算不足时无法“只跑部分维度”（`supports_aspect_narrowing=False`），只能整批跳过；
- 单次输出质量更依赖 prompt 与模型。

## 证据

- `SingleShotReviewer.calls_per_review = 1`；
- `supports_aspect_narrowing = False`，预算不足时整批跳过；
- 消融阶梯固定为 `diff / ast / graph / verifier`，不引入 aspect 维度。

## 复审条件

当离线评测显示“按 aspect 拆分”在特定类别（如 security）带来稳定、可量化的提升，且成本可接受时，再引入受控的多调用模式，并把它作为独立消融档位。
