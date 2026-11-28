# 评测结果快照（2026-11-28）

> 这是当前项目的**结果单一事实源**。架构文档只描述机制，README 只保留摘要和链接。
> 快照对应 main 上的三点 diff / evidence anchoring 修复之后的结果；holdout 仍未运行。

## 0. 状态

| 项 | 状态 |
|---|---|
| full dev production（20 PR） | ✅ 已完成 |
| mini dev ablation（3 PR × 4 tier） | ✅ 已完成 |
| 6-PR prompt 实验（v9 worktree） | ✅ 已完成，仅作附录 |
| full dev ablation（20 PR × 4 tier） | ❌ 未运行 |
| holdout production | ❌ 未运行，配置冻结后只跑一次 |
| 独立 judge 复核 | ❌ 未运行；当前 semantic judge 与 reviewer 同模型 |

固定配置（full dev production）：

```text
manifest:      aacr_pygo_v3.json
split:         dev
prompt:        review-v8
validator:     validator-v2（evidence anchoring 修复已合入代码）
retriever:     hybrid-v5
index schema:  6
metrics:       5
verification:  1
review:        deepseek-v4-pro + thinking + reasoning_effort=high
verifier:      deepseek-flash + no thinking
judge:         deepseek-v4-pro, line_k=1
```

---

## 1. 先修正评测有效性

### 1.1 三点 diff

AACR 记录里的 `base_commit` 经常是采集时 base 分支的 tip，而不是 PR 分支的分叉点。
40 个实例中有 **32 个** base 不是 head 的祖先。两点 diff `base..head` 会把 base
分支后来合入的无关改动算进 PR。

现在统一使用 GitHub PR 语义：

```text
git diff base...head
= git diff $(git merge-base base head) head
```

`langflow@b1cbdba` 是典型例子：

```text
两点 diff:  814 files, +57,870 / -53,896
三点 diff:    1 file,  +18 / -8
```

校验方式不是 `change_line_count`，而是 reference comment 的 `path` / `line`：

```text
reference paths in diff: 234 / 234
line ranges valid:       232 / 234
```

`change_line_count` 是 GitHub PR **最终 head** 的 additions+deletions；
manifest 的 `head_commit` 是 reference comment 提交时的 commit
（GitHub API 的 `original_commit_id`），两者不相等是正常的。
详见 `reports/diff_line_count_check.md`。

### 1.2 Evidence 锚定

模型经常把多行 evidence 挂在单行上，或在 evidence 里写 `...`。
Validator 现在会把 evidence 对齐到真实源码的连续行区间，并写回
`start_line/end_line/evidence`。

```text
dev-production-v2 原始 evidence coverage: 70.2%
锚定修复后的副本:                         100.0%
Gate:                                    PASS
```

修复只改变锚点和 evidence 文本，不改变 finding 内容、severity、category。
原始结果保留，修复副本位于：

```text
evaluation/results/aacr-pygo-v3/dev-production-v2-evidence/
evaluation/metrics/aacr-pygo-v3/dev-production-v2-evidence/
```

---

## 2. Full dev production（20 PR）

| 指标 | 值 |
|---|---:|
| N | 20 / 20 ok |
| Findings | 57 |
| Precision | 43.9% |
| Recall | 17.9% |
| Balanced / F1 | 25.4% |
| Invalid line | 0.0% |
| Evidence（原始 / 锚定后） | 70.2% / 100.0% |
| File recall | 88.2% |
| Symbol coverage | 31.9% |
| Citation rate | 59.6% |
| Graph contexts / PR | 18.15 |
| Calls / PR | 2.95 |
| Input / output tokens | 611k / 301k |
| 总耗时 | 2,335s（39 min） |

语言分层：

| 语言 | N | Precision | Recall | Balanced |
|---|---:|---:|---:|---:|
| Go | 10 | 34.4% | 12.8% | 18.6% |
| Python | 10 | 56.0% | 27.5% | 36.8% |

解读：

- Python 明显好于 Go；Go 是主要召回短板。
- 57 条 finding 是“高置信、少而精”的结果，不是覆盖式审查。
- 43.9% 的 precision 是**下限**：reference comment 稀疏，很多未命中 finding
  在人工抽查中是合理问题，只是 reference 没标。
- semantic judge 与 reviewer 同模型，存在 self-preference bias；最终对外结论应
  用独立 judge 或人工抽检复核。

---

## 3. Mini dev ablation（3 PR × 4 tier）

选样：

```text
Go:     mudler/LocalAI@e466450
        wavetermdev/waveterm@61b0fb4
Python: comfyanonymous/ComfyUI@4dcd698
```

合计 51 / 145 条 reference（35%），只用于方向验证，不做语言结论。

| Tier | Findings | Precision | Recall | Balanced | File recall | Symbol cov | Graph ctx | Calls/PR | Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| diff | 14 | 78.6% | 23.5% | 36.2% | 0.0% | 0.0% | 0 | 3.33 | 145.6k |
| ast | 13 | 76.9% | 21.6% | 33.7% | 89.3% | 29.9% | 0 | 3.33 | 178.8k |
| graph | 19 | 63.2% | 25.5% | 36.3% | 89.3% | 32.6% | 28.33 | 3.33 | 185.0k |
| verifier | 17 | 64.7% | 21.6% | 32.4% | 89.3% | 32.6% | 28.33 | 4.67 | 236.2k |

逐 PR balanced：

| PR | diff | ast | graph | verifier |
|---|---:|---:|---:|---:|
| LocalAI | 11.8% | 12.5% | **30.0%** | 22.2% |
| waveterm | **51.2%** | 39.2% | 43.5% | 40.0% |
| ComfyUI | 36.4% | **41.7%** | 33.3% | 32.0% |

结论（方向性，不作强断言）：

- **AST 解决文件覆盖**：file recall 0% → 89.3%，但 semantic balanced 33.7%。
- **Graph 主要是召回换精度**：R +2.0pp，P -15.4pp，balanced 基本持平；
  收益集中在 LocalAI（R 6.7% → 20.0%），waveterm/ComfyUI 没有提升。
- **Graph 的 citation rate 反而下降**（AST 38.5% → graph 21.1%）：上下文变多，
  但没有被模型有效使用，甚至引入噪声。
- **Verifier 未证明价值**：检查 5 个候选，0 rejection，token +28%；
  graph → verifier 的 balanced 从 36.3% 降到 32.4%。这不是 verifier 丢 finding，
  而是两次独立 review run 的波动，且 verifier 本身没有拒绝任何候选。

样本只有 3 个 PR，一条 finding 就能让 P 变化 5–7pp；报告中的配对 CI 为 `-`，
不能据此做显著性结论。

---

## 4. 6-PR prompt 实验（附录，v9 worktree）

同一组 6 个 PR，对比旧 prompt 与 recall 变体：

| Config | Findings | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| baseline（review-v8） | 26 | 50.0% | 16.3% | 24.6% |
| candidate（recall 变体） | 25 | **68.0%** | **20.9%** | **32.0%** |

文件级命中从 7 个提升到 11 个，且没有丢失原命中文件。  
但这只是**一次 6-PR 实验**，finding 级重合度低（Jaccard 0.36），不能替代
full-dev 结论。该变体没有解决 nil/panic、langflow、低覆盖问题。

---

## 5. Holdout

**未运行。**

holdout 只在 full-dev ablation 完成、配置冻结后跑一次。  
Mini 3-PR 结果不是 full-dev 结果，不应拿来跑 holdout 或据此调参。

---

## 6. 不要对外声称

```text
× graph 提升了 review 质量
× verifier 降低了误报
× 44% precision 代表真实 precision
× 18% recall 代表模型找不到问题
× mini ablation 支持语言差异结论
```

可以声称：

```text
- 修正了 32/40 个实例的 diff 语义错误
- evidence 锚定把可核验率从 70% 提到 100%
- full dev production 是 20/20 ok，P 43.9 / R 17.9 / Balanced 25.4
- AST 把 file recall 从 0 提到 89%
- graph 增加 context 和 symbol coverage，但在 3-PR mini 上 balanced 持平
- verifier 在当前配置下 0 rejection，成本 +28%，价值待验证
```

---

## 7. 产物

```text
reports/eval_dev_production_v2.md       # full dev production
reports/eval_mini_ablation.md           # 3-PR 四档 ablation
reports/diff_line_count_check.md        # diff 语义校验
evaluation/README.md                    # 复现步骤
```
