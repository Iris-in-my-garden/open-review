# ADR-0006：检索执行：AST/Graph 优先，rg 兜底

- 状态：Accepted
- 日期：2026-11
- 影响版本：retriever `hybrid-v5`、index schema `6`
- 相关代码：`src/openreview/retriever/index.py`、`graph.py`、`hybrid.py`、`grep.py`、`repo/local.py`
- 补充：[ADR-0002](ADR-0002-structural-retrieval.md) 的“精确符号优先”高层决策

## 上下文

ADR-0002 决定了“用结构化检索，不用向量/FTS5”，但执行层还有几个工程问题：

- `git grep` 多模式合并成正则后，耗时随模式数和匹配行数放大；
- 只给文件级上下文，模型仍不知道变更行属于哪个函数；
- 同一仓库多 PR/多进程会争用 clone、index、worktree；
- graph 是 best-effort，不能因为图失败而阻断 review。

## 决策

- AST + symbol index + import/call graph 是主路径；不启用向量检索与 FTS5。
- 引用检索优先 `rg -F -w`；没有 ripgrep 时把 `git grep` 拆成 4 个模式一批，
  共享总时间预算；最后才用有界 Python 扫描。命中数/AST 解析量有硬上限，
  超时只降级，不挂住 review。
- `DiffParser` 从 hunk 计算新增行在 head 文件的行号，检索器用 tree-sitter
  反查所属函数/类，因此函数体变更也能拿到完整上下文。
- 仓库缓存按 `owner__repo` 隔离：clone/fetch/checkout 用 `FileLock` 串行化；
  首次使用 blobless partial clone；缓存按 mtime 淘汰；每个 run 使用独立
  detached worktree。
- Graph 扩展是 best-effort：Python 用 `ast` 抽 caller/callee；Go 通过
  receiver/import/同名方法做置信度过滤，不解析接口动态分发。

## 备选方案

| 方案 | 放弃原因 |
|---|---|
| FTS5 / 向量检索 | 无法保证符号/行定位；不可解释；成本与索引漂移 |
| 纯 `git grep` 多模式 | 模式数一多就超时，且无法区分定义/引用 |
| 只给 diff，不做函数体定位 | 无法回答“变更行在哪个函数里” |
| 每次 review 重新 clone | 大仓库准备时间不可接受 |
| 全仓库无锁并发 | worktree/index/clone 争用，失败不可复现 |

## 后果

正面：

- 上下文可解释：file/lines/kind/priority/revision；
- 索引增量、可降级、无 embedding 成本；
- 多进程可以按仓库分片，避免同 repo 争用。

负面：

- 只覆盖 Python/Go；
- Go graph 不做完整类型/接口分发；
- rg 缺失时 git grep 仍需降级；
- 索引/缓存首次构建仍有成本。

## 证据

- `file_recall` / `symbol_coverage` / `graph_context_kinds` 指标；
- 3-PR mini ablation：AST 把 file recall 从 0 提到 89.3%，graph 把 contexts
  从 98 提到 173；
- `tests/test_graph_retriever.py`、`tests/test_grep_search.py`、`tests/test_local_repo.py`。

## 复审条件

当多语言覆盖、历史 review 知识召回或接口动态分发成为主要瓶颈，且离线评测
证明新的检索方式带来稳定增益时，再引入新的检索源；新增源必须保持
file/line/kind 可解释，并进入独立消融档位。
