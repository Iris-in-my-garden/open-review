# OpenReview 评测结果

> 由 `python -m evaluation.cli report` 生成。Precision / Recall / Balanced 来自 AACR 官方语义 judge；未跑 semantic 时显示 `-`。

## 汇总

| Config | N | Findings | Precision | Recall | Balanced | Invalid line | Evidence eval | Evidence | Unevaluable | File recall | Symbol cov | Citation | Graph ctx | Attribution | Calls/PR | Tokens | Latency |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| diff | 3/3 | 14 | 78.6% | 23.5% | 36.2% | 0.0% | 100.0% | 100.0% | 0 | 0.0% | 0.0% | 0.0% | 0 | 100.0% | 3.33 | 145.6k | 118.3s |
| ast | 3/3 | 13 | 76.9% | 21.6% | 33.7% | 0.0% | 100.0% | 100.0% | 0 | 89.3% | 29.9% | 38.5% | 0 | 100.0% | 3.33 | 178.8k | 119.8s |
| graph | 3/3 | 19 | 63.2% | 25.5% | 36.3% | 0.0% | 100.0% | 100.0% | 0 | 89.3% | 32.6% | 21.1% | 28.33 | 100.0% | 3.33 | 185.0k | 109.2s |
| verifier | 3/3 | 17 | 64.7% | 21.6% | 32.4% | 0.0% | 100.0% | 100.0% | 0 | 89.3% | 32.6% | 35.3% | 28.33 | 100.0% | 4.67 | 236.2k | 126.0s |

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

- **diff**: `evaluation/metrics/aacr-pygo-v3/mini-diff`
- **ast**: `evaluation/metrics/aacr-pygo-v3/mini-ast`
- **graph**: `evaluation/metrics/aacr-pygo-v3/mini-graph`
- **verifier**: `evaluation/metrics/aacr-pygo-v3/mini-verifier`

## 语义指标分层与置信区间

分层指标基于 AACR 每个 PR 的 judge 记录计算。单 run 的 95% CI 使用语言分层的仓库簇 bootstrap；配置对比则对相同 PR 做配对 bootstrap。固定 seed=42；任一语言少于 2 个仓库簇/配对 PR 时不报告区间，以免给出虚假的零宽区间。

### diff

| Group | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 2 | 70.0% | 24.2% | 36.0% |
| Python | 1 | 100.0% | 22.2% | 36.4% |
| comfyanonymous/ComfyUI | 1 | 100.0% | 22.2% | 36.4% |
| mudler/LocalAI | 1 | 50.0% | 6.7% | 11.8% |
| wavetermdev/waveterm | 1 | 75.0% | 38.9% | 51.2% |

| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |
|---|---:|---:|---:|
| overall | - | - | - |
| Go | 50.0–75.0% | 6.7–38.9% | 11.8–51.2% |
| Python | - | - | - |

### ast

| Group | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 2 | 71.4% | 18.2% | 29.0% |
| Python | 1 | 83.3% | 27.8% | 41.7% |
| comfyanonymous/ComfyUI | 1 | 83.3% | 27.8% | 41.7% |
| mudler/LocalAI | 1 | 100.0% | 6.7% | 12.5% |
| wavetermdev/waveterm | 1 | 66.7% | 27.8% | 39.2% |

| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |
|---|---:|---:|---:|
| overall | - | - | - |
| Go | 66.7–100.0% | 6.7–27.8% | 12.5–39.2% |
| Python | - | - | - |

### graph

| Group | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 2 | 61.5% | 27.3% | 37.8% |
| Python | 1 | 66.7% | 22.2% | 33.3% |
| comfyanonymous/ComfyUI | 1 | 66.7% | 22.2% | 33.3% |
| mudler/LocalAI | 1 | 60.0% | 20.0% | 30.0% |
| wavetermdev/waveterm | 1 | 62.5% | 33.3% | 43.5% |

| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |
|---|---:|---:|---:|
| overall | - | - | - |
| Go | 60.0–62.5% | 20.0–33.3% | 30.0–43.5% |
| Python | - | - | - |

### verifier

| Group | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 2 | 70.0% | 21.2% | 32.6% |
| Python | 1 | 57.1% | 22.2% | 32.0% |
| comfyanonymous/ComfyUI | 1 | 57.1% | 22.2% | 32.0% |
| mudler/LocalAI | 1 | 66.7% | 13.3% | 22.2% |
| wavetermdev/waveterm | 1 | 71.4% | 27.8% | 40.0% |

| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |
|---|---:|---:|---:|
| overall | - | - | - |
| Go | 66.7–71.4% | 13.3–27.8% | 22.2–40.0% |
| Python | - | - | - |

## 配置间配对比较

对相同 PR 计算左侧配置减右侧配置的差值，并在语言层内对 PR 有放回抽样；每次抽样在两个配置间使用相同的 PR，给出配对 95% bootstrap CI。

| Comparison | Paired PRs | Metric | Δ (left − right) | 95% paired bootstrap CI |
|---|---:|---|---:|---:|
| diff vs ast | 3 | precision | +1.6% | - |
| diff vs ast | 3 | recall | +2.0% | - |
| diff vs ast | 3 | balanced | +2.5% | - |
| diff vs graph | 3 | precision | +15.4% | - |
| diff vs graph | 3 | recall | -2.0% | - |
| diff vs graph | 3 | balanced | -0.1% | - |
| diff vs verifier | 3 | precision | +13.9% | - |
| diff vs verifier | 3 | recall | +2.0% | - |
| diff vs verifier | 3 | balanced | +3.9% | - |
| ast vs graph | 3 | precision | +13.8% | - |
| ast vs graph | 3 | recall | -3.9% | - |
| ast vs graph | 3 | balanced | -2.6% | - |
| ast vs verifier | 3 | precision | +12.2% | - |
| ast vs verifier | 3 | recall | +0.0% | - |
| ast vs verifier | 3 | balanced | +1.3% | - |
| graph vs verifier | 3 | precision | -1.5% | - |
| graph vs verifier | 3 | recall | +3.9% | - |
| graph vs verifier | 3 | balanced | +4.0% | - |
