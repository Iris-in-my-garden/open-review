# OpenReview 评测结果

> 由 `python -m evaluation.cli report` 生成。Precision / Recall / Balanced 来自 AACR 官方语义 judge；未跑 semantic 时显示 `-`。

## 汇总

| Config | N | Findings | Precision | Recall | Balanced | Invalid line | Evidence eval | Evidence | Unevaluable | File recall | Symbol cov | Citation | Graph ctx | Attribution | Calls/PR | Tokens | Latency |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| production | 19/20 | 173 | - | - | - | 0.0% | 100.0% | 65.3% | 0 | 88.0% | 33.0% | 54.3% | 37.74 | 100.0% | 7.16 | 2.7M | 314.6s |

## 指标说明

- **N**: 成功结果数 / 计划样本数
- **Invalid line**: finding 行号不在 diff 可见行内的比例，越低越好
- **Evidence eval**: 有 evidence 的 finding 中，行号与对应 commit 源码可核验的比例
- **Evidence**: 核验成功的 finding 中，精确源码证据匹配比例
- **Unevaluable**: 因 diff、文件、行号或源码不可用而无法核验的 evidence 数
- **Unevaluable**: 因 diff、文件、行号或源码不可用而无法核验的 evidence 数
- **File recall**: reference comment 指向文件被检索上下文覆盖的比例
- **Symbol cov**: 变更符号被检索上下文覆盖的比例
- **Citation**: finding 显式引用 `[ctx-xxxxxxxx]` 的比例
- **Graph ctx**: 每个样本由 import/call graph 实际注入的上下文数
- **Attribution**: 带 `source_aspects` 的 finding 比例
- **Calls/PR**: 每个成功样本平均 LLM 调用数
- **Tokens**: 成功样本的 input + output token 总量

## 数据来源

- **production**: `evaluation/metrics/aacr-pygo-v3/dev-production`
