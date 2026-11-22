# 术语表

| 术语 | 含义 |
|---|---|
| **PR** | Pull Request；也支持本地 `base..head` 区间 |
| **Diff / Patch** | 变更内容；OpenReview 解析为文件、hunk、可见行、符号 |
| **Hunk** | diff 中以 `@@` 开头的一段连续变更 |
| **Side** | `RIGHT` 表示新文件侧行号，`LEFT` 表示删除行的旧文件侧行号 |
| **Scope** | `introduced`（本次引入）或 `pre_existing`（历史问题）；默认只保留前者 |
| **Finding / Issue** | 模型报告的一条潜在问题 |
| **Evidence** | 支持 finding 的代码片段，必须能在 patch/context/源码中定位 |
| **Grounding** | 证据是否真的存在于给定代码中；Validator 的核心检查 |
| **Context** | 注入 prompt 的代码证据（变更所在函数、定义、引用、字段、测试等） |
| **Priority** | 上下文优先级 P1–P6：static / enclosing / definition / reference / field / test |
| **Batch** | 按 token 预算切分的一组变更文件；每个 batch 一次 LLM 调用 |
| **Ablation** | 固定四档消融：`diff → ast → graph → verifier` |
| **Triage** | 规则决定是否跳过 LLM、收窄审查重点 |
| **Validator** | 确定性校验：文件、行号、side、scope、证据、重复、patch 可应用性 |
| **Verifier** | 语义复核：先反证再判定，输出 verified/rejected/needs_more_context |
| **Outcome** | 一次 run 的终态：completed / skipped / partial / failed |
| **Partial** | 有失败 batch 或预算耗尽，但仍有可用结果 |
| **Degraded reason** | 记录“结果为什么可能不完整”的稳定文本 |
| **Budget** | 调用数/输入 token/输出 token/时间的硬护栏 |
| **Reserve** | 调用前原子检查并扣减预算 |
| **Settle** | 用 provider usage 校正估算输入、结算输出预留 |
| **Refund** | 退还未实际发出的请求所占预算 |
| **Run ID** | 单次 run 的唯一标识；metrics、trace、调用日志按它归属 |
| **Trace** | 每个节点的耗时、状态（ok/skipped/error）与错误详情 |
| **Artifact version** | prompt/validator/retriever/cache/metrics 的版本，参与 cache key |
| **Cache key** | PR 身份 + 模型采样参数 + artifact 版本 + 输出相关配置 |
| **Worktree** | 每次 run 独立的 git 工作树，避免并发 checkout 互相踩踏 |
| **RepoIndex** | SQLite 符号/import/call 索引，按 blob SHA 增量更新 |
| **AST** | tree-sitter 解析出的语法树；用于定位函数/类与折叠 |
| **Graph** | import/call 关系图；用于找 caller/callee/dependency |
| **Base revision** | PR 基线版本的代码；只用于解释删除代码，不能作为新文件 finding 的证据 |
| **Gate** | 只读确定性 metrics 的 CI 门禁 |
