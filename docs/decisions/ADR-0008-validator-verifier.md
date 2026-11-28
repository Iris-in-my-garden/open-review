# ADR-0008：Validator 做确定性校验，Verifier 做有界语义复核

- 状态：Accepted
- 日期：2026-11
- 影响版本：validator `validator-v2` / `FindingVerifier`
- 相关代码：`src/openreview/review/finding_validator.py`、`evidence_anchor.py`、`verifier.py`
- 补充：[ADR-0005](ADR-0005-partial-outcome.md) 的失败可见性

## 上下文

LLM 自报的 finding 和 confidence 都不可直接信任：

- 行号可能不在 diff 内；
- evidence 可能被编造，或只是“看起来像源码”；
- 证据真实但结论过度解读；
- 多行 evidence 经常被挂在单行上，导致确定性指标误判 ungrounded。

需要一个“确定性校验 + 有界语义复核”的分层，而不是让模型自证。

## 决策

两层分工：

**Validator（确定性）**

- 文件必须在 diff 中；
- 行号必须在对应侧可见行内；
- evidence 必须能在 patch / context / 真实源码中定位；
- 多行 evidence / 省略号 / 缩进差异做锚定修复，写回
  `start_line/end_line/evidence`；
- 命中 evidence 加分，缺失/不匹配减分；低于阈值丢弃；
- `suggested_patch` 用 `git apply --check` 验证，失败只剥离 patch，不丢 finding；
- 过滤后重新计算风险、决策、证据列表。

**Verifier（语义、可选、有界）**

- 只复核 `critical` / `high`，以及低于阈值的 `medium`；
- 最多一次回炉，所有调用受 `ReviewBudget` 约束；
- 只做“支持/反驳/需要更多上下文”，不因原 severity/confidence 高而自动升级；
- 失败/超时保留原 finding，不阻断报告。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 只信模型 confidence | “自信但编造”无法过滤 |
| 只做 LLM validator | 不可解释、成本高、无法保证行号/evidence |
| Verifier 校验所有 finding | 调用量/成本过高，收益未证明 |
| Verifier 可以自动升级 severity | 引入不可控的二次判断 |
| 多轮 verifier | 成本黑洞，且没有证据支持 |

## 后果

正面：

- 行号/evidence 可机器核验，Gate 可进 CI；
- evidence 锚定把可核验率从 70.2% 提到 100%；
- verifier 不阻断主流程，预算可控。

负面：

- validator 抓不住“证据真实但结论错”的问题；
- mini ablation 中 verifier 检查 5 个候选、0 rejection、token +28%，
  当前配置未证明质量增量；
- 多行 evidence 的锚定是启发式，仍可能截断/错位。

## 证据

- `validator.last_stats` 的 grounded/missing/ungrounded、patch_valid、line_relocated；
- `tests/test_finding_validator.py`；
- full dev：Gate PASS，evidence coverage 100%；
- 3-PR mini：verifier candidates 5、rejected 0、needs_more_context 1。

## 复审条件

如果 full-dev  ablation 仍然显示 verifier rejection 接近 0，应优先改 verifier prompt
/ 候选策略，或把 verifier 降为可选档；只有在能证明“拒绝 FP 且不误杀 matched
finding”时，才值得保留默认开启。
