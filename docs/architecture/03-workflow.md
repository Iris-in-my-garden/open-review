# 03 · 工作流节点契约

`BoundedWorkflow` 顺序执行节点，允许节点级条件跳过；`max_steps=32` 防止节点表失控。没有模型自主规划、没有无限循环。

## 1. 节点表

| # | 节点 | 启用条件 | 输入 | 输出 / 副作用 | 失败语义 |
|---:|---|---|---|---|---|
| 1 | `parse_url` | `not local_mode` | `state.pr_url` | `state.pr_ref` | 抛异常 → `failed` |
| 2 | `fetch_pr` | `not local_mode` | `pr_ref` | `pr_info`、`raw_files` | 抛异常 → `failed` |
| 3 | `load_local` | `local_mode` | `local_input` | 合成 `pr_info`、`pr_url` | 抛异常 → `failed` |
| 4 | `cache_lookup` | `review_cache.enabled` | cache key | 命中：`review_result`、`cache_hit=True` | 未命中继续 |
| 5 | `prepare_repo` | `not cache_hit and not local_mode` | `pr_ref`、`pr_info` | `pr_worktree`、`local_repo_path`、`base_path` | PR worktree 失败 → `failed`；base worktree `RepoCommandError` → 仅记录 `base_error` |
| 6 | `prepare_local` | `local_mode and not cache_hit` | `local_input` | `local_workspace`、`local_repo_path`、`base_path`、`raw_files` | 抛异常 → `failed` |
| 7 | `parse_diff` | `not cache_hit` | `raw_files` | `parsed_diff` | 抛异常 → `failed` |
| 8 | `triage` | `not cache_hit` | `parsed_diff`、`pr_info` | `selected_aspects`、`skipped_llm`、`triage_reason`；跳过时生成 `review_result` | 抛异常 → `failed`（规则本身是纯函数，正常不会失败） |
| 9 | `analyze` | `not cache_hit and not skipped_llm` | `parsed_diff`、repo、batch | `batches`、`batch_results`、`all_contexts`、`review_result` | 单 batch 失败 → 继续；预算耗尽 → break；无任何结果 → `failed` |
| 10 | `verify` | `verifier != None and not cache_hit and not skipped_llm and review_result != None` | `review_result`、`parsed_diff`、contexts、budget | 过滤/降权后的 `review_result`、`verifier_stats` | 内部按 chunk 容错；预算不足/调用失败保留原 finding |
| 11 | `write_report` | 总是 | `review_result`、`output_dir` | `report_path` | 抛异常 → `failed` |
| 12 | `post_comments` | `post_comments and not cache_hit and not local_mode` | `review_result`、`pr_ref`、`pr_info` | GitHub Review（upsert） | 抛异常 → `failed`（报告已落盘） |
| 13 | `finalize` | 总是 | 全部 state | `outcome`、`review_result.metrics`、review cache、debug | 抛异常 → `failed` |

## 2. `analyze` 内部顺序

对每个 batch 严格执行以下顺序，任何一步失败都不会影响已完成的 batch：

```text
1. retriever.retrieve(batch, base_path)
2. evidence_pipeline.extra_contexts(batch, impact, static)
3. plan_contexts(contexts, max_context_tokens)
4. 跨 batch 去重 → state.all_contexts
5. 预算预检：
   a. remaining_calls < calls_per_batch ?
      - 0 → budget_exhausted，break
      - 可收窄 → 按 security > bugs > performance > clean_code 收窄
      - 不可收窄 → budget_exhausted，break
   b. budget.allow(calls, estimated_tokens * calls_per_batch + 2000) ?
      - 否 → budget_exhausted，break
6. reviewer.review(...)
   - BudgetExceeded → budget_exhausted，break
   - 其他异常 → failed_batches++，记录错误，continue
7. 累加 validator stats
```

结束后：

- `batch_results` 为空 → 抛 `RuntimeError` → `failed`；
- 多 batch → `merge_review_results` + 跨 batch 去重；
- `failed_batches > 0` → 报告写“结果不完整”，outcome 由 resolver 判为 `partial`。

## 3. 预算与调用的关系

| 场景 | 行为 |
|---|---|
| single-pass reviewer | `calls_per_review = 1`，不能只跑部分维度；调用数不足则整批跳过 |
| 自定义 reviewer | 无 `calls_per_review` 时按 `len(aspects)` 估算 |
| reviewer 绕过 BudgetedLLMClient | executor 在调用后补 `consume(calls, tokens)` |
| LLM cache 命中 | 不增加 `used_llm_calls`，也不消耗预算 |

## 4. 失败矩阵（完整）

| 失败点 | 检测 | 降级动作 | outcome | 用户可见 |
|---|---|---|---|---|
| URL 非法 | `parse_pr_url` 抛错 | 终止 | `failed` | exit 1 |
| GitHub 元数据/文件失败 | `GitHubAPIError` | 重试后终止 | `failed` | exit 1 |
| PR worktree 失败 | `RepoCommandError` / git 失败 | 终止 | `failed` | exit 1 |
| base worktree 失败 | `RepoCommandError` | 记录 `base_error`，继续 | `completed` | review notes |
| 本地 worktree 失败 | 异常 | 终止 | `failed` | exit 1 |
| diff 解析失败 | 异常 | 终止 | `failed` | exit 1 |
| triage 规则异常 | 异常 | 终止（未捕获） | `failed` | exit 1 |
| 索引构建失败 | `index_error` | grep fallback | `completed` / `partial` | `retrieval.stats.index_error` |
| Graph 检索失败 | `graph_error` | 仅 AST；必要时补一次 grep | `completed` | `retrieval.graph_error` |
| AST 检索失败 | `ast_error` | 无上下文，继续 | `completed` | `retrieval.stats.ast_error` |
| 单 batch LLM 失败 | `ProviderError` 等 | 继续其他 batch | `partial` | `failed_batches` + notes |
| 预算耗尽（调用/token/时间） | `BudgetExceeded` / preflight 拒绝 | 停止后续 batch | `partial` | `budget_exhausted` |
| 所有 batch 失败 | `batch_results` 为空 | 终止 | `failed` | exit 1 + 错误 |
| Validator 丢弃 finding | 规则 | 过滤后重算风险 | `completed` | `validation` 统计 |
| Verifier 调用失败 | 异常 | 保留原 finding | `completed` | note + `verifier` 统计 |
| Verifier 预算不足 | `BudgetExceeded` | 剩余 finding 保留原判断 | `completed` | note |
| 报告写盘失败 | `OSError` | 终止 | `failed` | exit 1 |
| 评论发布失败 | `GitHubAPIError` | 终止 | `failed` | 报告已落盘，exit 1 |
| metrics 写盘失败 | `OSError` | best-effort | 原 outcome | 仅日志 |
| worktree 释放失败 | 异常 | best-effort | 原 outcome | 仅日志 |

## 5. Trace 与进度

每个节点产生一条 `TraceEvent`：

```python
TraceEvent(node, elapsed_ms, status="ok" | "skipped" | "error", detail="")
```

- 跳过节点也记录 `status="skipped"`，便于解释“为什么没执行”；
- 异常节点记录 `detail="Type: message"`；
- `state.stages` 记录阶段耗时（与 trace 互补，供 metrics 聚合）；
- CLI 进度按 `_PROGRESS_STEPS` 显示 `step/13`，必须与节点表保持同步。

## 6. 扩展节点时的检查清单

- [ ] 新节点是否有明确的 `enabled` 条件？
- [ ] 失败是终止、降级还是重试？是否写入 `degraded_reasons`？
- [ ] 是否影响 outcome？是否需要更新 `OutcomeResolver`？
- [ ] 是否消耗预算？是否走 `BudgetedLLMClient`？
- [ ] 是否写入共享资源（cache/index/worktree）？锁与隔离是否明确？
- [ ] 是否影响 cache key？是否更新 `CacheKeyBuilder` / `ReviewConfig.cache_key_parts()`？
- [ ] 是否更新 `_PROGRESS_STEPS`、本文档、metrics schema？
