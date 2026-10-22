""" 把 ReviewResult 写成 Markdown 文件。 """
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


class PotentialIssue(BaseModel):
    file: str
    severity: str
    category: str
    confidence: float
    reasoning: str
    suggestion: str
    line: Optional[int] = None
    end_line: Optional[int] = None
    evidence: Optional[str] = None
    suggested_patch: Optional[str] = None


class ReviewResult(BaseModel):
    owner: str
    repo: str
    pr_number: int
    summary: str
    risk_level: str
    risk_reason: str
    key_changes: List[str] = Field(default_factory=list)
    potential_issues: List[PotentialIssue] = Field(default_factory=list)
    missing_test_suggestions: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    final_decision: str
    final_summary: str
    review_notes: List[str] = Field(default_factory=list)


class ReportWriter:

    @classmethod
    def render_markdown(cls, result: ReviewResult) -> str:
        """将 ReviewResult 渲染为符合 Prompt 格式要求的 Markdown 文本"""
        md_lines = ["# PR Review Report", ""]

        # 1. PR Summary
        md_lines.extend(["## 1. PR Summary", result.summary, ""])

        # 2. Risk Level
        md_lines.extend(
            [
                "## 2. Risk Level",
                f"- **Risk Level**: {result.risk_level}",
                f"- **Reason**: {result.risk_reason}",
                "",
            ]
        )

        # 3. Key Changes
        md_lines.append("## 3. Key Changes")
        if result.key_changes:
            for change in result.key_changes:
                md_lines.append(f"- {change}")
        else:
            md_lines.append("No significant key changes recorded.")
        md_lines.append("")

        # 4. Potential Issues
        md_lines.append("## 4. Potential Issues")
        if not result.potential_issues:
            md_lines.append("No high-confidence issues found.")
        else:
            for idx, issue in enumerate(result.potential_issues, 1):
                location = issue.file
                if issue.line:
                    location += f":{issue.line}"
                    if issue.end_line and issue.end_line != issue.line:
                        location += f"-{issue.end_line}"
                location = f"`{location}`"

                issue_lines = [
                    f"### Issue {idx}",
                    f"- **file**: {location}",
                    f"- **severity**: {issue.severity}",
                    f"- **category**: {issue.category}",
                    f"- **confidence**: {issue.confidence}",
                    f"- **reasoning**: {issue.reasoning}",
                    f"- **suggestion**: {issue.suggestion}",
                ]
                if issue.evidence:
                    issue_lines.append(f"- **evidence**: `{issue.evidence}`")
                if issue.suggested_patch:
                    issue_lines.extend(
                        [
                            "- **suggested_patch**:",
                            "```diff",
                            issue.suggested_patch,
                            "```",
                        ]
                    )
                issue_lines.append("")
                md_lines.extend(issue_lines)
        md_lines.append("")

        # 5. Missing Test Suggestions
        md_lines.append("## 5. Missing Test Suggestions")
        if result.missing_test_suggestions:
            for test_sug in result.missing_test_suggestions:
                md_lines.append(f"- {test_sug}")
        else:
            md_lines.append("No additional tests required.")
        md_lines.append("")

        # 6. Evidence
        md_lines.append("## 6. Evidence")
        if result.evidence:
            for item in result.evidence:
                md_lines.append(f"- {item}")
        else:
            md_lines.append("No specific evidence attached.")
        md_lines.append("")

        # 7. Final Recommendation
        md_lines.extend(
            [
                "## 7. Final Recommendation",
                f"- **Decision**: {result.final_decision}",
                f"- **Summary**: {result.final_summary}",
                "",
            ]
        )

        # 8. Review Notes（可选：批次、失败维度、注入告警等）
        if result.review_notes:
            md_lines.append("## 8. Review Notes")
            for note in result.review_notes:
                md_lines.append(f"- {note}")
            md_lines.append("")

        return "\n".join(md_lines)

    @classmethod
    def write(cls, result: ReviewResult, output_dir: Path) -> Path:
        """把 ReviewResult 格式化并写入 Markdown 文件"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{result.owner}_{result.repo}_pr_{result.pr_number}_review.md"
        file_path = output_dir / filename

        content = cls.render_markdown(result)
        file_path.write_text(content, encoding="utf-8")

        return file_path