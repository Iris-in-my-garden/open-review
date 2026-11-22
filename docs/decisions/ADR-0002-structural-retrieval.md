# ADR-0002：精确符号图优先，不启用向量检索

- 状态：Accepted
- 日期：2026-10
- 影响版本：retriever `hybrid-v4`、index schema `3`
- 相关代码：`src/openreview/retriever/index.py`、`graph.py`、`hybrid.py`

## 上下文

Code Review 需要回答的是结构化问题：

- 变更行属于哪个函数/类？
- 这个 callee 的定义在哪？
- 谁调用了被修改/删除的符号？
- 哪些字段/测试与变更相关？
- 删除的符号是否还有残留引用？

这些问题需要**精确、可解释、可定位到行**的证据。向量检索的“语义相似”无法保证这些，还会引入 embedding 成本、索引漂移和不可解释的召回。

## 决策

以 SQLite 符号索引 + tree-sitter AST + import/call graph 为主：

- `RepoIndex` 存 symbols/imports/calls/files；
- 按 git blob SHA 增量更新；
- `HybridRetriever` 并行跑 AST 与 graph；
- 索引/图不可用时才走 grep fallback；
- 不启用向量检索与 FTS5。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| 向量检索（chunk + embedding） | 无法保证符号/行定位；成本与索引漂移；不可解释 |
| FTS5 全文检索 | 已实现但未使用，最终移除（commit `72489a8`） |
| 纯 grep | 无结构关系，噪声大，无法区分定义/引用 |
| 完整类型/数据流分析 | 成本过高，语言覆盖难，超出 review 所需 |

## 后果

正面：

- 证据可解释：每条上下文有 file/lines/kind/priority/revision；
- 可增量：只重排 diff 涉及的文件；
- 可降级：索引/图失败时仍能保留 AST 或 grep；
- 无 embedding 成本。

负面：

- 只覆盖 Python/Go；
- call graph 是 candidate，不保证类型正确；
- 动态调用、反射、配置注入等关系可能漏检；
- 语义相似的历史 review 知识无法召回。

## 证据

- `RepoIndex.ensure` 按 `git diff --name-status` + blob SHA 增量；
- `HybridRetriever` 用 `ThreadPoolExecutor(2)` 并行 AST/Graph；
- 评测提供 `file_recall` / `symbol_coverage` / `graph_context_kinds` 指标。

## 复审条件

当多语言覆盖或“历史 review 知识召回”成为主要瓶颈，且离线评测证明语义检索带来稳定增益时，再引入向量检索（优先用于历史知识，而不是当前 diff 定位）。
