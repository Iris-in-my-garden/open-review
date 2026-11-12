"""把 ReviewResult 写成 Markdown 文件。

The result models are canonical domain models. They are re-exported here for
backward compatibility with callers that imported them from the renderer.
"""

from pathlib import Path

from openreview.domain.models import (
    IssueScope,
    IssueSide,
    PotentialIssue,
    ReviewResult,
    normalize_scope,
    normalize_side,
)
from openreview.domain.severity import Severity, normalize_severity

__all__ = [
    "IssueScope",
    "IssueSide",
    "PotentialIssue",
    "ReportWriter",
    "ReviewResult",
    "Severity",
    "normalize_scope",
    "normalize_severity",
    "normalize_side",
]


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
                    if issue.side == "LEFT":
                        location += " (deleted line)"
                location = f"`{location}`"

                issue_lines = [
                    f"### Issue {idx}",
                    f"- **file**: {location}",
                    f"- **severity**: {issue.severity}",
                    f"- **category**: {issue.category}",
                    f"- **confidence**: {issue.confidence}",
                ]
                if issue.scope == "pre_existing":
                    issue_lines.append("- **scope**: pre_existing")
                issue_lines.extend(
                    [
                        f"- **reasoning**: {issue.reasoning}",
                        f"- **suggestion**: {issue.suggestion}",
                    ]
                )
                if issue.evidence:
                    issue_lines.append(f"- **evidence**: `{issue.evidence}`")
                if issue.context_refs:
                    issue_lines.append(
                        "- **context_refs**: "
                        + ", ".join(f"`{ref}`" for ref in issue.context_refs)
                    )
                if issue.suggested_patch:
                    issue_lines.extend(
                        [
                            "- **suggested_patch**:",
                            "```diff",
                            issue.suggested_patch,
                            "```",
                        ]
                    )
                    if issue.suggested_patch_valid is False:
                        issue_lines.append(
                            "- **suggested_patch_check**: INVALID"
                            + (
                                f" (`{issue.suggested_patch_error}`)"
                                if issue.suggested_patch_error
                                else ""
                            )
                        )
                    elif issue.suggested_patch_valid is True:
                        issue_lines.append(
                            "- **suggested_patch_check**: valid"
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

        # 8. Review Notes & Run Metrics
        if result.review_notes or result.metrics:
            md_lines.append("## 8. Review Notes")
            for note in result.review_notes:
                md_lines.append(f"- {note}")

            metrics = result.metrics or {}
            if metrics:
                md_lines.extend(["", "### Run Metrics"])
                stages = metrics.get("stages_seconds")
                if isinstance(stages, dict):
                    stage_text = ", ".join(
                        f"{name}={seconds:.2f}s"
                        for name, seconds in stages.items()
                    )
                    md_lines.append(f"- **Stages**: {stage_text}")
                for key, label in (
                    ("files", "Files"),
                    ("batches", "Batches"),
                    ("reviewer_mode", "Reviewer Mode"),
                    ("aspects", "Focus Aspects"),
                    ("llm_calls", "LLM Calls"),
                    ("estimated_patch_tokens", "Estimated Patch Tokens"),
                    ("estimated_context_tokens", "Estimated Context Tokens"),
                    ("total_seconds", "Total Seconds"),
                ):
                    if key in metrics:
                        md_lines.append(f"- **{label}**: {metrics[key]}")

                for key, label in (
                    ("triage", "Triage"),
                    ("impact", "Impact Analysis"),
                    ("static_analysis", "Static Analysis"),
                    ("verifier", "Verifier"),
                    ("cache", "Cache"),
                    ("budget", "Budget"),
                    ("llm_usage", "LLM Usage"),
                ):
                    value = metrics.get(key)
                    if isinstance(value, dict) and value:
                        rendered = ", ".join(
                            f"{name}={item}" for name, item in value.items()
                        )
                        md_lines.append(f"- **{label}**: {rendered}")
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

    @classmethod
    def write_json(cls, result: ReviewResult, path: Path) -> Path:
        """导出机器可读结果（含 source_aspects / metrics），供评测消费。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path
