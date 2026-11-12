"""Canonical domain models.

These models used to live in ``openreview.github.client`` and
``openreview.review.report_writer``. Keeping them here removes the inverted
dependency where review logic imported GitHub infrastructure and GitHub
infrastructure imported review rendering.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from openreview.domain.severity import Severity, normalize_severity

# 问题是否由本次 diff 引入。历史问题默认不进入风险计算和行内评论，
# 避免把“既有代码里的老问题”算到本次 PR 头上，稀释真正的新增风险。
IssueScope = Literal["introduced", "pre_existing"]
# 行号属于 diff 的哪一侧：RIGHT=新文件侧，LEFT=删除行（旧文件侧）。
IssueSide = Literal["RIGHT", "LEFT"]

_SCOPE_ALIASES = {
    "introduced": "introduced",
    "introduced_by_diff": "introduced",
    "diff": "introduced",
    "new": "introduced",
    "changed": "introduced",
    "本次": "introduced",
    "本次引入": "introduced",
    "pre_existing": "pre_existing",
    "preexisting": "pre_existing",
    "existing": "pre_existing",
    "historical": "pre_existing",
    "history": "pre_existing",
    "old": "pre_existing",
    "历史": "pre_existing",
    "历史问题": "pre_existing",
}

_SIDE_ALIASES = {
    "right": "RIGHT",
    "new": "RIGHT",
    "new_side": "RIGHT",
    "added": "RIGHT",
    "left": "LEFT",
    "old": "LEFT",
    "old_side": "LEFT",
    "deleted": "LEFT",
    "deletion": "LEFT",
    "removed": "LEFT",
}


def normalize_scope(value: object) -> IssueScope:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _SCOPE_ALIASES.get(text, "introduced")  # type: ignore[return-value]


def normalize_side(value: object) -> IssueSide:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _SIDE_ALIASES.get(text, "RIGHT")  # type: ignore[return-value]


class PullRequestInfo(BaseModel):
    """PR 基本元数据。"""

    owner: str
    repo: str
    pr_number: int
    title: str
    body: Optional[str] = None
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    html_url: str


class ChangedFile(BaseModel):
    """PR 中变更文件的信息。"""

    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None


class PotentialIssue(BaseModel):
    file: str
    severity: Severity
    category: str
    confidence: float
    reasoning: str
    suggestion: str
    line: Optional[int] = None
    end_line: Optional[int] = None
    evidence: Optional[str] = None
    suggested_patch: Optional[str] = None
    # 本次 diff 引入 vs 未改动代码中的历史问题；默认只保留前者。
    scope: IssueScope = "introduced"
    # RIGHT 使用新文件行号，LEFT 使用删除行的旧文件行号。
    side: IssueSide = "RIGHT"
    # 引用了哪些检索上下文；评测用于 citation rate / context precision。
    context_refs: List[str] = Field(default_factory=list)
    # 由 FindingValidator 写入：git apply --check 的结果（None 表示未检查）。
    suggested_patch_valid: Optional[bool] = None
    suggested_patch_error: Optional[str] = None
    # 内部归因：哪些审查维度报告过这个问题（跨维度去重时合并）。
    # 不参与渲染，供评测统计“维度贡献度”使用。
    source_aspects: List[str] = Field(default_factory=list)

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: object) -> Severity:
        return normalize_severity(str(value))

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: object) -> IssueScope:
        return normalize_scope(value)

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: object) -> IssueSide:
        return normalize_side(value)


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
    metrics: Dict[str, Any] = Field(default_factory=dict)
