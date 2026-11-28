# 07 · 可观测性与排障

## 1. RunMetrics

每次 run 生成一份 `RunMetrics`（`src/openreview/domain/metrics.py`，schema `5`），写入：

- `reports/...`（随 JSON 输出）
- `--dump-intermediates` 目录下的 `run_<run_id>_metrics.json`

| 分组 | 关键字段 | 用途 |
|---|---|---|
| 身份 | `schema_version`、`run_id`、`versions` | 复现与版本对齐 |
| 阶段 | `stages_seconds`、`total_seconds` | 定位慢在哪 |
| Diff | `files`、`diff.files/additions/deletions/languages` | 规模与语言 |
| 检索 | `retrieval.*` | 覆盖、去重、优先级、graph、`context_gap` |
| 批次 | `batches`、`llm_calls`、`llm_calls_detail` | 成本与拆分 |
| 证据增强 | `triage`、`impact`、`static_analysis` | 哪些增强生效 |
| 校验 | `validation`、`verifier`、`verification` | 漏斗与丢弃原因 |
| 缓存 | `cache` | review/LLM cache 命中 |
| 预算 | `budget` | 用量、预留、是否超限 |
| 结果 | `outcome`、`failed_batches`、`budget_exhausted`、`degraded_reasons`、`errors` | 终态与降级原因 |

### 1.1 关键派生指标

| 指标 | 计算 | 读法 |
|---|---|---|
| `retrieval.changed_file_coverage` | 被上下文覆盖的变更文件 / 变更文件 | 检索是否覆盖改动 |
| `retrieval.symbol_coverage` | 检索到的变更符号 / 变更符号 | 符号级召回 |
| `retrieval.duplicate_contexts` | 总上下文 - 唯一上下文 | 去重效果 |
| `retrieval.context_gap.prompt_contexts_omitted` | 被 prompt 预算丢弃的上下文 | “检索到但没喂进去” |
| `validation.validator_kept` | 过滤后保留数 | 噪声水平 |
| `verification.final_count` | 最终 finding 数 | 漏斗终点 |
| `budget.input_tokens_over_budget` | 实际输入是否超限 | 估算误差 |

## 2. Trace

每个节点产生 `TraceEvent(node, elapsed_ms, status, detail)`：

- `status="ok"`：正常完成；
- `status="skipped"`：条件跳过；
- `status="error"`：异常，`detail` 为 `Type: message`。

`state.stages` 与 trace 互补：trace 看节点顺序与跳过原因，stages 看聚合耗时。

## 3. Debug sink

`--dump-intermediates <dir>` 会写入：

| 文件 | 内容 |
|---|---|
| `state.json` | 单次 run 的 state 摘要（diff、batch、contexts、errors、budget） |
| `metrics.json` | 完整 RunMetrics |
| `report.md` | 报告副本 |

用于离线分析、评测复核、面试演示。不要把包含私有代码的 debug 产物提交到公开仓库。

## 4. 排障 Runbook

| 症状 | 先看 | 可能原因 | 处理 |
|---|---|---|---|
| exit 1，无报告 | `outcome`、`errors`、trace 的 `error` 节点 | URL/GitHub/worktree/parse 失败 | 按错误节点修复输入或凭证 |
| exit 2 | `outcome.status=partial`、`failed_batches`、`budget_exhausted` | 单 batch 失败或预算不足 | 提高预算/缩小 PR/看 batch 错误 |
| 报告说“结果不完整” | `degraded_reasons` | 同上 | 同上 |
| 没有 finding | `validation`、`verifier` | 置信度阈值过高、证据不足、Verifier 拒绝 | 看 `validation.dropped_*` 与 `verifier.rejected` |
| 行号大量缺失 | `validation.missing_line` | 模型未给行号或行号不在 diff 可见行 | 检查 prompt/模型；不要放宽校验 |
| 上下文很少 | `retrieval.context_gap`、`retrieval.stats` | 索引失败/预算 cap/大文件挤占 | 看 `index_error`、`contexts_omitted_by_cap` |
| 索引很慢 | `stages_seconds.prepare_repo`、`index_build_seconds` | 首次全量/大仓库 | 预热索引；检查 `OPENREVIEW_INDEX_DIR`；确认 seed 已生成且差集未超阈值 |
| 成本异常 | `budget`、`llm_calls_detail` | 重试/verifier/大 batch | 降低 `max_batch_tokens` 或预算 |
| 评论重复 | `cache`、GitHub review marker | upsert 未生效/权限不足 | 检查 token 权限与 `upsert_review` |
| 结果不可复现 | `versions`、`cache`、LLM cache 模式 | 模型/版本/温度/seed 变化 | 固定 model/temperature/seed，用 replay |

## 5. 回放与复现

```bash
# 录制一次真实响应
uv run openreview review <pr_url> --llm-cache-dir .cache/llm --llm-cache-mode record

# 离线回放（不调用模型）
uv run openreview review <pr_url> --llm-cache-dir .cache/llm --llm-cache-mode replay
```

回放用于：

- 改 Validator/Verifier/聚合策略后比较结果；
- 复现面试/评审中的具体 case；
- 在无 API Key 环境下跑通链路。

## 6. 缓存操作

| 操作 | 命令/配置 | 注意 |
|---|---|---|
| 关闭 review cache | 默认关闭 | 避免旧结果掩盖改动 |
| 指定 cache 目录 | `--cache-dir` | 多 run 共享时注意锁 |
| 清空索引 | 删除 `OPENREVIEW_INDEX_DIR` | 下次全量重建 |
| 关闭 seed 增量 | `OPENREVIEW_INDEX_SEED=0` | 每个新 HEAD 都全量构建 |
| 调整 seed 差集阈值 | `OPENREVIEW_INDEX_SEED_MAX_RATIO` | 默认 `0.35`；调小更保守 |
| 强制重建索引 | 修改 `INDEX_SCHEMA_VERSION` 或删除 DB | 会变慢 |
| 清理仓库缓存 | 删除平台缓存目录 | 下次重新 clone |

## 7. 版本对齐

排障前先确认 metrics 里的 `versions` 与当前代码一致：

```json
{
  "prompt": "review-v8",
  "validator": "validator-v2",
  "retriever": "hybrid-v5",
  "review_cache_schema": 4,
  "metrics_schema": 5
}
```

版本不一致时，先排除“旧缓存/旧报告”再定位代码问题。
