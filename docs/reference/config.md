# 配置参考

`ReviewConfig`（`src/openreview/review/config.py`）是不可变配置，只包含“会影响输出”的项；客户端与工作流由 `ReviewPipeline` 装配。

## 1. 默认值

### 模型与连接

| 字段 | 默认 | CLI | 说明 |
|---|---:|---|---|
| `llm_model` | `gpt-4o` / `OPENAI_MODEL` | `--model` | 模型名 |
| `llm_base_url` | `None` / `OPENAI_BASE_URL` | `--llm-base-url` | OpenAI 兼容服务 |
| `llm_timeout` | 60.0 | — | 单次请求超时 |
| `llm_max_retries` | 0 | `--llm-max-retries` | 每次 attempt 独立计账 |
| `temperature` | 0.0 | — | 固定采样，保证可复现 |
| `seed` | `None` | — | 可选随机种子 |
| `max_completion_tokens` | 2048 | `--max-completion-tokens` | 单次输出上限 |

### 批处理与上下文

| 字段 | 默认 | 说明 |
|---|---:|---|
| `max_batch_tokens` | 12,000 | 单批 patch 预算 |
| `max_patch_tokens` | 8,192 | 单文件 patch 软预算 |
| `hard_patch_tokens` | 48,000 | 单个超大 hunk 熔断 |
| `max_context_tokens` | 8,192 | P2–P6 共享上下文预算 |
| `soft_prompt_tokens` | 10,000 | 单批目标 |
| `hard_prompt_tokens` | 14,000 | 触发拆分的硬上限 |

### 检索与证据增强

| 字段 | 默认 | 说明 |
|---|---|---|
| `retrieval_mode` | `graph` | `none` / `ast` / `graph` |
| `triage_enabled` | `True` | 规则 triage |
| `impact_enabled` | `True` | 跨文件影响分析 |
| `static_analysis_enabled` | `False` | 默认关闭，避免执行本地工具 |
| `static_tools` | `("ruff",)` | 静态分析工具 |
| `verifier_enabled` | `True` | 二次复核 |
| `verify_severities` | `("critical","high")` | 复核范围 |
| `verify_confidence_threshold` | 0.75 | medium 低置信度也复核 |

### 预算

| 字段 | 默认 | CLI |
|---|---:|---|
| `budget_calls` | 0（不限） | `--budget-calls` |
| `budget_tokens` | 0（不限） | `--budget-tokens` |
| `budget_output_tokens` | 0（不限） | `--budget-output-tokens` |
| `budget_seconds` | 0（不限） | `--budget-seconds` |

### 缓存

| 字段 | 默认 | CLI |
|---|---|---|
| `review_cache_enabled` | `False` | `--cache-review` |
| `review_cache_dir` | `None` | `--cache-dir` |
| `llm_cache_dir` | `None` | `--llm-cache-dir` |
| `llm_cache_mode` | `auto` | `--llm-cache-mode` |
| `cache_schema_version` | 4 | — |

## 2. 消融档位

`--ablation` 会覆盖检索、verifier、triage、impact、static：

| 档位 | retrieval | verifier | triage | impact | static |
|---|---|---|---|---|---|
| `diff` | `none` | ❌ | ❌ | ❌ | ❌ |
| `ast` | `ast` | ❌ | ❌ | ❌ | ❌ |
| `graph` | `graph` | ❌ | ❌ | ❌ | ❌ |
| `verifier` | `graph` | ✅ | ❌ | ❌ | ❌ |

`None` 表示生产默认：graph + triage + impact + verifier，static 关闭。

## 3. Cache key 组成

```text
pr_url
base_sha
head_sha
model
temperature
seed
prompt_version
validator_version
retriever_version
review_cache_schema
schema
ablation
retrieval
triage
impact
static
static_tools
verifier
verify_severities
verify_confidence_threshold
max_batch_tokens
max_patch_tokens
hard_patch_tokens
max_context_tokens
soft_prompt_tokens
hard_prompt_tokens
llm_timeout
llm_max_retries
max_completion_tokens
mock
```

规则：**任何会改变输出的配置都必须进 cache key**；`debug_dir` 不影响输出，因此不进入。

## 4. 环境变量

| 变量 | 用途 |
|---|---|
| `OPENAI_API_KEY` | LLM 凭证（必需，mock 除外） |
| `OPENAI_MODEL` | 默认模型 |
| `OPENAI_BASE_URL` | OpenAI 兼容服务地址 |
| `GITHUB_TOKEN` | 私有仓库/更高 API 限额（可选） |
| `OPENREVIEW_INDEX_DIR` | 索引目录 |
| `OPENREVIEW_INDEX_SEED` | 是否用 baseline 快照做跨 HEAD 增量索引（默认开） |
| `OPENREVIEW_INDEX_SEED_MAX_RATIO` | seed 差集占比上限，超过则全量重建（默认 `0.35`） |
| `OPENREVIEW_CACHE_DIR` | 仓库缓存目录 |
| `OPENREVIEW_REVIEW_CACHE_DIR` | Review cache 目录 |

## 5. 配置变更检查清单

- [ ] 是否影响输出？若是，加入 `cache_key_parts()`。
- [ ] 是否影响预算语义？更新 `05-budget.md`。
- [ ] 是否影响检索/上下文？更新 `04-retrieval.md`。
- [ ] 是否影响 metrics？更新 `07-operations.md` 与 schema 版本。
- [ ] 是否影响消融？更新 `08-evaluation.md` 与 `ablation.py`。
