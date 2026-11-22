# OpenReview 文档

本目录区分三类内容，避免把“设计解释”“契约参考”“操作手册”混在一篇里：

- **Explanation**：为什么这样设计、取舍是什么 → `ARCHITECTURE.md`、`decisions/`
- **Reference**：节点、状态、配置、指标的准确契约 → `architecture/`、`reference/`
- **How-to**：怎么排障、怎么跑评测、怎么扩展 → `architecture/07-operations.md`、`architecture/08-evaluation.md`

## 阅读路径

| 时间 | 读什么 | 目标 |
|---|---|---|
| 5 分钟 | `ARCHITECTURE.md` §1–§4 | 知道系统是什么、不是什么、一次 run 怎么走 |
| 30 分钟 | `ARCHITECTURE.md` + `decisions/ADR-0001..0005` | 理解五个关键取舍及其代价 |
| 排障 | `architecture/07-operations.md` | 从 outcome/metrics 定位失败点 |
| 改代码 | `architecture/03-workflow.md` + `reference/config.md` | 知道节点契约、配置默认值和 cache key |
| 做评测 | `architecture/08-evaluation.md` | 指标定义、切分、消融、当前状态 |

## 文档地图

| 文档 | 内容 |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 系统地图：定位、C4、生命周期、决策索引、失败总览 |
| [`architecture/01-context.md`](architecture/01-context.md) | 外部系统、容器、信任边界、部署与并发形态 |
| [`architecture/02-lifecycle.md`](architecture/02-lifecycle.md) | 一次 review 的时序、artifact、outcome 状态机、缓存 |
| [`architecture/03-workflow.md`](architecture/03-workflow.md) | 13 个节点的输入/输出/跳过/失败契约 |
| [`architecture/04-retrieval.md`](architecture/04-retrieval.md) | 索引 schema、增量、P1–P6、cap、降级、batch 规划 |
| [`architecture/05-budget.md`](architecture/05-budget.md) | 预算账本、并发、reserve/settle/refund、默认值 |
| [`architecture/06-trust.md`](architecture/06-trust.md) | Validator/Verifier、威胁模型、发布与脱敏 |
| [`architecture/07-operations.md`](architecture/07-operations.md) | metrics schema、trace、debug、排障 runbook |
| [`architecture/08-evaluation.md`](architecture/08-evaluation.md) | 指标定义、数据切分、四档消融、CI gate、当前状态 |
| [`decisions/`](decisions/) | ADR：有界工作流、结构化检索、预算下沉、单遍审查、partial outcome |
| [`reference/glossary.md`](reference/glossary.md) | 术语表 |
| [`reference/config.md`](reference/config.md) | 配置默认值、CLI 映射、cache key 组成 |

## 维护约定

1. **单一事实源**：测试数量、消融结果、版本号只在本文档体系中出现一次；README 只保留 Quickstart 和链接。
2. **数字带来源**：例如 `338 tests (uv run pytest -q, 2026-11-20)`。数字变化时同步更新。
3. **契约变更必须改文档**：节点顺序、`ReviewState` 字段、预算语义、优先级/cap、metrics schema 变化时，更新对应 reference 文档。
4. **架构决策必须加 ADR**：新增/替换关键机制时，在 `decisions/` 新增 ADR，并标注状态与复审条件。
5. **代码锚点**：优先写 `src/openreview/...::Symbol`，便于重命名后 grep 到。

## 版本矩阵

本文档描述的行为对应：

| Artifact | Version |
|---|---|
| Prompt | `review-v8` |
| Validator | `validator-v2` |
| Retriever | `hybrid-v4` |
| Review cache schema | `4` |
| Metrics schema | `5` |
| Verification metrics schema | `1` |

版本常量定义在 `src/openreview/domain/versions.py`，修改时必须同步本文档与 cache key 说明。
