"""OpenReview 的离线评测工具。

这里不依赖 GitHub API：AACR-Bench 等数据集提供 ``repo + base_commit +
head_commit``，我们直接调用 ``ReviewPipeline.run_local``，再把结果转换成
AACR 统一评测框架能消费的 JSON。
"""
