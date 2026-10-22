# OpenReview

一个面向 GitHub Pull Request 的 AI Code Review 工具：拉取 PR → 解析 Diff → AST 上下文检索 → 多维度并行审查 → 结果校验 → 生成 Markdown 报告。

> 目标不是“让 LLM 随便说几句”，而是把每条结论约束在**真实 Diff 与真实代码上下文**上，减少幻觉与重复。

## 功能

- 输入一个 PR URL，生成结构化 Review 报告
- 7 阶段流水线：URL 解析 → PR 元数据 → 仓库缓存 → Diff 解析 → 上下文检索 → LLM 审查 → 报告输出
- 多维度并行审查：`clean_code` / `bugs` / `performance` / `security`
- **AST 级上下文检索**（tree-sitter）：
  - 只要改动了函数体，就能自动定位并抽取所属函数/类
  - 区分“符号定义”和“符号引用”，分别赋分
  - 支持 Python / Go，自动跳过文档、依赖、构建目录
- **Pydantic 结构化输出**，避免解析自由文本
- **后置校验**：过滤低置信度、过滤 Diff 中不存在的文件，并基于过滤后的结果重新计算风险等级与合并建议
- `--mock_llm` 离线演示，无需 API Key 即可跑通全流程

## 架构

```
GitHub PR URL
      │
      ▼
┌─────────────────┐
│  PR URL Parser  │
└────────┬────────┘
         ▼
┌─────────────────┐     ┌──────────────────┐
│  GitHub Client  │────▶│ PR 元数据 / Files │
└────────┬────────┘     └──────────────────┘
         ▼
┌─────────────────┐
│   RepoManager   │  clone / fetch PR head 到本地缓存
└────────┬────────┘
         ▼
┌─────────────────┐
│   DiffParser    │  Hunk / 变更行 / 新增&删除符号 / 测试文件标记
└────────┬────────┘
         ▼
┌──────────────────────────────┐
│ TreeSitterLocalRetriever     │
│ 1. 变更行 → 所属函数/类       │
│ 2. 符号 → git grep → AST 片段 │
└────────┬─────────────────────┘
         ▼
┌──────────────────────────────┐
│ ReviewOrchestrator           │
│  ThreadPool: 4 个维度并行     │
│  Structured Output (Pydantic)│
└────────┬─────────────────────┘
         ▼
┌─────────────────┐
│ FindingValidator│  置信度 / 文件存在性过滤 + 结论重算
└────────┬────────┘
         ▼
┌─────────────────┐
│  ReportWriter   │  Markdown 报告
└─────────────────┘
```

## 安装

需要 Python 3.11+ 与 `git`。

```bash
uv sync
# 或
pip install -e .
```

## 配置

| 环境变量 | 是否必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 使用真实 LLM 时必填 | OpenAI 或兼容服务的 API Key |
| `OPENAI_MODEL` | 否 | 默认 `gpt-4o` |
| `OPENAI_BASE_URL` | 否 | 兼容 OpenAI 协议的服务地址（如代理、本地模型网关） |
| `GITHUB_TOKEN` | 否 | 访问私有仓库或避免 GitHub API 限流时填写 |

> GitHub Token 与 LLM API Key 是两套独立凭证，互不复用。

## 使用

```bash
# 离线演示（不调用真实 LLM，也不需要 API Key）
openreview review https://github.com/encode/httpx/pull/1055 --mock_llm

# 真实审查
export OPENAI_API_KEY=sk-...
export GITHUB_TOKEN=ghp_...
openreview review https://github.com/owner/repo/pull/123

# 指定模型 / 兼容网关
openreview review https://github.com/owner/repo/pull/123 \
  --model gpt-4.1-mini \
  --base-url https://your-gateway.example.com/v1
```

报告默认写入 `reports/<owner>_<repo>_pr_<number>_review.md`，可用 `--output` 指定目录。

## 项目结构

```
src/openreview/
├── cli.py                     # Typer CLI
├── github/                    # GitHub REST API 与 PR URL 解析
├── repo/
│   ├── manager.py             # 仓库 clone / fetch / checkout
│   └── diff_parser.py         # Hunk、变更行、符号提取
├── retriever/
│   ├── grep_retriever.py      # 纯 grep 基线实现
│   └── tree_sitter_retriever.py  # AST 边界 + 定义/引用区分
├── review/
│   ├── prompt_builder.py      # 通用 Prompt 与维度 Prompt
│   ├── aspect_reviewer.py     # 多维度并行编排
│   ├── output_parser.py       # 结构化 Schema 与聚合
│   ├── finding_validator.py   # 后置校验
│   ├── assessment.py          # 风险等级 / 决策统一规则
│   ├── report_writer.py       # Markdown 渲染
│   └── llm_client.py          # 真实 / Mock LLM 客户端
└── services/review_service.py # 7 阶段流水线
```

## 设计取舍

**为什么用 AST 而不是纯 grep？**
纯 grep 只能给出“命中了某一行”，无法确定代码边界，也无法判断是定义还是引用。AST 能抽取完整函数/类，并按“变更行所属定义 > 定义 > 引用”排序。

**为什么只改函数体也能检索到上下文？**
`DiffParser` 会从 Hunk 中计算新增行在 head 文件中的行号；检索器再用 tree-sitter 反查这些行所属的函数/类。因此即使函数签名没变，也能拿到完整上下文。

**如何控制幻觉？**
1. Prompt 要求每条结论必须有 Diff/Context 依据
2. Pydantic 约束输出结构
3. 校验阶段过滤低置信度问题
4. 过滤 Diff 中不存在的文件
5. 过滤后重新计算风险等级、合并建议与证据列表，保证报告内部一致

## 测试

```bash
uv run pytest -q
```

## 已知限制 / Roadmap

- [ ] 问题定位到具体行号，并支持通过 GitHub Review API 发表行内评论
- [ ] 跨维度问题去重（同一问题会被多个维度重复报告）
- [ ] 大 PR 的 token 预算与分块审查
- [ ] 测试文件关联与测试缺口分析（目前建议仍是通用文案）
- [ ] base/head 双 worktree，支持查看被删除代码的上下文
- [ ] 评测集与 precision / recall / 成本 / 延迟指标
- [ ] Prompt Injection 防护（PR 标题、描述、代码注释均为不可信输入）
