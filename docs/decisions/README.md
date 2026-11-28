# 架构决策记录（ADR）

| ADR | 决策 | 状态 |
|---|---|---|
| [ADR-0001](ADR-0001-bounded-workflow.md) | 固定有界工作流，不做自由 Agent | Accepted |
| [ADR-0002](ADR-0002-structural-retrieval.md) | 精确符号图优先，不启用向量检索 | Accepted |
| [ADR-0003](ADR-0003-budget-in-client.md) | 预算下沉到 LLM 客户端 | Accepted |
| [ADR-0004](ADR-0004-single-pass-per-batch.md) | 每个 batch 一次结构化调用 | Accepted |
| [ADR-0005](ADR-0005-partial-outcome.md) | `partial` 是一等状态 | Accepted |
| [ADR-0006](ADR-0006-retrieval-execution.md) | 检索执行：AST/Graph 优先，rg 兜底 | Accepted |
| [ADR-0007](ADR-0007-rule-based-triage.md) | 规则 triage，不用模型 | Accepted |
| [ADR-0008](ADR-0008-validator-verifier.md) | Validator 确定性校验 + Verifier 有界语义复核 | Accepted |
| [ADR-0009](ADR-0009-prompt-budget-observability.md) | Prompt/context 预算、批处理与可观测性 | Accepted |
| [ADR-0010](ADR-0010-safety-publication.md) | 安全边界与发布策略 | Accepted |

## 新增 ADR 的规则

1. 复制任一 ADR 作为模板。
2. 必须包含：状态、日期、影响版本、相关代码、上下文、决策、备选方案、后果、证据、复审条件。
3. 状态只能是 `Proposed` / `Accepted` / `Superseded` / `Deprecated`。
4. 被替代的 ADR 不删除，改为 `Superseded by ADR-XXXX`，保留历史。
5. 在 `docs/ARCHITECTURE.md` §7 的索引表中登记。
