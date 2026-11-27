# OpenReview 评测结果

> 由 `python -m evaluation.cli report` 生成。Precision / Recall / Balanced 来自 AACR 官方语义 judge；未跑 semantic 时显示 `-`。

## 汇总

| Config | N | Findings | Precision | Recall | Balanced | Invalid line | Evidence eval | Evidence | Unevaluable | File recall | Symbol cov | Citation | Graph ctx | Attribution | Calls/PR | Tokens | Latency |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| production | 20/20 | 57 | 43.9% | 17.9% | 25.4% | 0.0% | 100.0% | 70.2% | 0 | 88.2% | 31.9% | 59.6% | 18.15 | 100.0% | 2.95 | 912.1k | 116.8s |

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

- **production**: `evaluation/metrics/aacr-pygo-v3/dev-production-v2`

## 语义指标分层与置信区间

分层指标基于 AACR 每个 PR 的 judge 记录计算。单 run 的 95% CI 使用语言分层的仓库簇 bootstrap；配置对比则对相同 PR 做配对 bootstrap。固定 seed=42；任一语言少于 2 个仓库簇/配对 PR 时不报告区间，以免给出虚假的零宽区间。

### production

| Group | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 10 | 34.4% | 12.8% | 18.6% |
| Python | 10 | 56.0% | 27.5% | 36.8% |
| browser-use/browser-use | 3 | 50.0% | 60.0% | 54.5% |
| comfyanonymous/ComfyUI | 3 | 66.7% | 25.0% | 36.4% |
| langflow-ai/langflow | 4 | 50.0% | 22.7% | 31.2% |
| mudler/LocalAI | 1 | 50.0% | 6.7% | 11.8% |
| ollama/ollama | 6 | 30.8% | 8.9% | 13.8% |
| wavetermdev/waveterm | 3 | 35.3% | 20.6% | 26.0% |

| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |
|---|---:|---:|---:|
| overall | 38.5–54.3% | 14.0–22.9% | 20.8–30.0% |
| Go | 30.8–50.0% | 6.7–20.6% | 11.8–26.0% |
| Python | 50.0–66.7% | 22.7–60.0% | 31.2–54.5% |
