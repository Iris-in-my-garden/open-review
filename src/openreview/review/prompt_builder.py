# openreview/prompt_builder.py
from typing import List

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.grep_retriever import CodeContext


DEFAULT_SYSTEM_PROMPT = """你是一位资深、严谨的软件架构师和 Code Reviewer。请根据提供的 GitHub PR 基础信息、Diff 摘要以及检索到的上下文代码，生成一份客观、准确的 PR Review 报告。

### 【输入数据】

1. PR Title:
{pr_title}

2. PR Description:
{pr_description}

3. Changed Files Summary:
{changed_files_summary}

4. Patch Snippets:
{patch_snippets}

5. Retrieved Context Snippets:
{retrieved_context_snippets}

---

### 【审查与判断原则】（严格遵守）
1. **基于事实，禁止幻觉**：所有的结论、问题和证据必须有输入数据（Diff 或 Context）的直接依据。严禁编造不存在的文件、函数、类名或测试结果。
2. **拒绝强行找茬**：不要为了显现审查价值而强行捏造低质量或不存在的问题。如果代码质量良好或信息不足，在 Potential Issues 中明确声明 "No high-confidence issues found"。
3. **关注核心风险**：优先关注 Correctness（正确性）、Edge Cases（边界条件）、Security（安全性）以及 Test Gap（测试缺失）。尽量忽略单纯的语法风格、命名偏好或格式问题。
4. **置信度把关**：只有在逻辑链条完整且有明确证据时才给出高置信度（Confidence > 0.8）；对于假设性或推测性的问题，标明低置信度或直接不予报告。

---

### 【输出格式与结构】

请严格使用中文，并严格按照以下 Markdown 格式输出 Review 报告，不要更改一级和二级标题名称：

# PR Review Report

## 1. PR Summary
[用 2-3 句话简要总结此 PR 的主要目的、核心改动范围及其要解决的问题。]

## 2. Risk Level
[从以下选项中选择一个，并附带一句话解释：Low / Medium / High]
- **Risk Level**: [Low/Medium/High]
- **Reason**: [选择该风险等级的简要理由]

## 3. Key Changes
[列出核心修改点，按模块或功能点组织]
- **[文件/模块名]**: [改动说明]
- **[文件/模块名]**: [改动说明]

## 4. Potential Issues
[如果存在问题，请按以下结构逐条列出；若无确切问题，请填写："No high-confidence issues found."]

### Issue 1
- **file**: `[存在问题的文件路径]`
- **severity**: [Low / Medium / High / Critical]
- **category**: [Correctness / Edge Case / Security / Performance / Maintainability]
- **confidence**: [0.0 到 1.0 之间的数值，例如 0.85]
- **reasoning**: [详细解释为什么这是一个潜在问题，说明在什么场景下会触发]
- **suggestion**: [具体的修复建议或改进代码]

*(如有更多问题，按 Issue 2, Issue 3 顺序列出)*

## 5. Missing Test Suggestions
[分析改动是否缺乏对应的单元测试或集成测试，给出具体的测试补充建议]
- [结合具体修改的函数/逻辑，建议补充的测试用例或边界条件测试]

## 6. Evidence
[提供支撑上述分析的关键代码片段引用或上下文线索，列出行号或上下文来源]
- **[引用点 1]**: `[引用相关文件及具体代码片段/Diff 行]`
- **[引用点 2]**: `[说明该证据支持了哪一条结论]`

## 7. Final Recommendation
[给出最终处理建议，例如：Approve / Request Changes / Comment]
- **Decision**: [Approve / Request Changes / Comment]
- **Summary**: [对作者的一句话总结建议]

"""

# 定义四大审查维度的专业 System Prompt
ASPECT_SYSTEM_PROMPTS = {
    "clean_code": (
        "你是一位资深的 Clean Code 与软件架构专家。你的任务是专注于检查代码的可读性、"
        "命名规范（如语义是否直观）、函数是否过大（违反单一职责原则 SRP）、重复代码（DRY），"
        "以及是否存在不必要的魔法数字（Magic Number）。请提供具体的重构建议。"
    ),
    "bugs": (
        "你是一位资深的 QA 与软件测试工程师。你的任务是找出代码中隐藏的逻辑漏洞、"
        "未处理的边界条件（Edge Cases，如空值、负数、极限值）、Null/Undefined 异常、"
        "异步处理遗漏 await、以及未妥善捕获的 Exception 异常。"
    ),
    "performance": (
        "你是一位高并发与系统性能优化专家。你的任务是评估代码的时间与空间复杂度、"
        "找出潜在的性能瓶颈（如嵌套循环导致的 O(n^2)、数据库 N+1 查询问题、"
        "不必要的内存拷贝、昂贵的同步阻塞操作），并提供优化方案。"
    ),
    "security": (
        "你是一位应用安全专家（AppSec Engineer）。你的任务是严格审查 OWASP Top 10 安全风险，"
        "特别关注 SQL 注入、跨站脚本（XSS）、硬编码密钥（Hardcoded Secrets / API Keys）、"
        "不安全的反序列化以及鉴权与权限校验（Authorization/Authentication）漏洞。"
    )
}

class PromptBuilder:
    def __init__(self, system_prompt: str = DEFAULT_SYSTEM_PROMPT):
        self.system_prompt = system_prompt

    def build_system_prompt(self) -> str:
        return self.system_prompt

    def build_system_prompt_for_aspect(self, aspect: str) -> str:
        """根据维度获取专属的 System Prompt"""
        return ASPECT_SYSTEM_PROMPTS.get(aspect, self.system_prompt)

    def build_user_prompt(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
    ) -> str:
        # Changed files 摘要
        files_summary_list = []
        for f in parsed_diff.files:
            symbols_str = ", ".join(f.changed_symbols) if f.changed_symbols else "None"
            files_summary_list.append(
                f"- {f.filename} ({f.status}): +{f.additions}/-{f.deletions}, Symbols: {symbols_str}"
            )
        files_summary_str = "\n".join(files_summary_list) if files_summary_list else "No files changed."

        # Patch 块
        patch_list = [
            f"### {f.filename}\n```diff\n{f.patch}\n```"
            for f in parsed_diff.files if f.patch
        ]
        patch_str = "\n\n".join(patch_list) if patch_list else "No patches available."

        # Context 块
        context_list = [
            f"### File: {ctx.file_path} (Reason: {ctx.reason}, Score: {ctx.score})\n```\n{ctx.snippet}\n```"
            for ctx in contexts
        ]
        context_str = "\n\n".join(context_list) if context_list else "No relevant contexts retrieved."

        pr_title = pr_info.title
        pr_body = pr_info.body

        return f"""
### 【输入数据】
1. PR Title: {pr_title}
2. PR Description: {pr_body}
3. Changed Files Summary:
{files_summary_str}
4. Patch Snippets:
{patch_str}
5. Retrieved Context Snippets:
{context_str}
"""

    def build_user_prompt_for_aspect(
            self,
            aspect: str,
            pr_info: PullRequestInfo,
            parsed_diff: ParsedDiff,
            contexts: List[CodeContext],
    ) -> str:
        """针对特定维度追加专属审查指令的 User Prompt"""
        base_prompt = self.build_user_prompt(pr_info, parsed_diff, contexts)

        aspect_instructions = {
            "clean_code": "【审查重点】请严格检查此 PR 的代码规范、命名可读性与架构合理性。",
            "bugs": "【审查重点】请彻底排查此 PR 可能引入的逻辑错误、异常分支与边界条件。",
            "performance": "【审查重点】请评估此 PR 的算法复杂度、数据库交互与潜在的性能瓶颈。",
            "security": "【审查重点】请地毯式检查此 PR 是否包含安全漏洞、敏感信息泄露或权限缺陷。"
        }

        instruction = aspect_instructions.get(aspect, "请进行全面审查。")

        return f"{base_prompt}\n\n### 【当前任务指令】\n{instruction}"