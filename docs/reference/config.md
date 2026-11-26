# 配置参考

`ReviewConfig`（`src/openreview/review/config.py`）是不可变配置，只包含“会影响输出”的项；客户端与工作流由 `ReviewPipeline` 装配。

## 1. 默认值

### 模型与连接

Review 与 Verifier 可以使用不同模型：默认 review 用 `deepseek-v4-pro` + thinking，
verifier 用 `deepseek-flash` + 非 thinking。非 DeepSeek 模型不会收到 `thinking`
扩展字段；verifier 默认沿用主模型。

| 字段 | 默认 | CLI | 说明 |
|---|---|---|---|
| `llm_model` | `deepseek-v4-pro` / `OPENAI_MODEL` | `--model` | Review 主模型 |
| `verifier_llm_model` | DeepSeek 下 `deepseek-flash` | `--verifier-model` | Verifier 模型 |
| `llm_base_url` | DeepSeek 官方 / `OPENAI_BASE_URL` | `--base-url` | OpenAI 兼容服务 |
| `llm_thinking` | `True` | `--thinking/--no-thinking` | Review thinking（仅 DeepSeek） |
| `verifier_llm_thinking` | `False` | `--verifier-thinking` | Verifier thinking |
| `llm_reasoning_effort` | `high` | `--reasoning-effort` | `none/low/high/max` |
| `llm_timeout` | 60.0 | — | 单次请求超时 |
| `llm_max_retries` | 0 | `--llm-max-retries` | 每次 attempt 独立计账 |
| `temperature` | 0.0 | — | 固定采样，保证可复现 |
| `seed` | `None` | — | 可选随机种子（DeepSeek 不发送） |
| `max_completion_tokens` | 16,384 | `--max-completion-tokens` | 单次输出上限（thinking 含 reasoning token） |

DeepSeek 只支持 `response_format={"type": "json_object"}`，因此
`structured_output=auto` 会对 DeepSeek 自动切换到 JSON 模式，并在 system prompt
末尾追加 JSON Schema 指令；其他 OpenAI 兼容端点继续使用 JSON Schema。
可用 `OPENREVIEW_STRUCTURED_OUTPUT=json_schema|json_object` 覆盖。

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
verifier_model
thinking
verifier_thinking
reasoning_effort
max_completion_tokens
mock
```

规则：**任何会改变输出的配置都必须进 cache key**；`debug_dir` 不影响输出，因此不进入。

## 4. 环境变量

| 变量 | 用途 |
|---|---|
| `OPENAI_API_KEY` | LLM 凭证（兼容 OpenAI 协议的端点） |
| `DEEPSEEK_API_KEY` | `OPENAI_API_KEY` 未设置时的 DeepSeek 凭证 |
| `OPENAI_MODEL` | Review 主模型（默认 `deepseek-v4-pro`） |
| `OPENREVIEW_VERIFIER_MODEL` | Verifier 模型（默认 DeepSeek 下 `deepseek-flash`） |
| `OPENAI_BASE_URL` | OpenAI 兼容服务地址（DeepSeek 模型默认官方地址） |
| `OPENREVIEW_STRUCTURED_OUTPUT` | `auto` / `json_schema` / `json_object` |
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
