# openreview/finding_validator.py
import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever import ast_utils
from openreview.retriever.code_context import CodeContext
from openreview.review.assessment import (
    assess_risk,
    decide,
    deduplicate_issues,
    normalize_severity,
    unique_files,
)
from openreview.review.report_writer import PotentialIssue, ReviewResult

logger = logging.getLogger(__name__)

MAX_GROUNDING_FILE_SIZE = 500 * 1024
MAX_SUGGESTED_PATCH_SIZE = 100_000
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
COMMON_EVIDENCE_TOKENS = {
    "self",
    "this",
    "return",
    "def",
    "class",
    "func",
    "function",
    "const",
    "let",
    "var",
    "if",
    "else",
    "for",
    "while",
    "try",
    "except",
    "catch",
    "finally",
    "with",
    "as",
    "import",
    "from",
    "pass",
    "raise",
    "none",
    "null",
    "nil",
    "true",
    "false",
    "and",
    "or",
    "not",
    "in",
    "is",
    "new",
    "type",
    "string",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "print",
    "len",
    "str",
    "object",
    "super",
}


class FindingValidator:
    """对 LLM 产出做后置校验，并基于“过滤后”的问题重新计算结论。

    校验不只过滤低置信度，还会用真实代码做证据核对（evidence grounding）：
    - 行号必须落在该文件 Patch 的新文件侧可见行内
    - evidence 必须在 Patch / 检索上下文 / 本地源码中能找到
    - 通过证据校验加分，找不到证据减分
    """

    def __init__(
        self,
        min_confidence: float = 0.6,
        invalid_line_penalty: float = 0.15,
        missing_line_penalty: float = 0.10,
        missing_evidence_penalty: float = 0.05,
        ungrounded_evidence_penalty: float = 0.20,
        grounded_evidence_bonus: float = 0.05,
    ):
        self.min_confidence = min_confidence
        self.invalid_line_penalty = invalid_line_penalty
        self.missing_line_penalty = missing_line_penalty
        self.missing_evidence_penalty = missing_evidence_penalty
        self.ungrounded_evidence_penalty = ungrounded_evidence_penalty
        self.grounded_evidence_bonus = grounded_evidence_bonus
        # 最近一次 validate 的计数，供 pipeline 写入 metrics / 评测分析。
        self.last_stats: Dict[str, int] = {}

    def validate(
        self,
        result: ReviewResult,
        parsed_diff: ParsedDiff,
        contexts: Optional[Sequence[CodeContext]] = None,
        repo_path: Optional[Path] = None,
    ) -> ReviewResult:
        files_by_name = {file.filename: file for file in parsed_diff.files}
        contexts = contexts or []
        valid_context_ids = {context.ensure_context_id() for context in contexts}
        stats = {
            "candidates": len(result.potential_issues),
            "kept": 0,
            "dropped_unknown_file": 0,
            "invalid_line": 0,
            "missing_line": 0,
            "missing_evidence": 0,
            "ungrounded_evidence": 0,
            "grounded_evidence": 0,
            "evidence_exact": 0,
            "evidence_line_match": 0,
            "evidence_token_match": 0,
            "invalid_context_ref": 0,
            "low_confidence": 0,
            "deduplicated": 0,
            "patch_checked": 0,
            "patch_valid": 0,
            "patch_invalid": 0,
        }

        filtered_issues: List[PotentialIssue] = []
        for issue in result.potential_issues:
            file_diff = files_by_name.get(issue.file)
            # 规则 1: 防止文件路径幻觉（确保 Issue 指向的文件在 Diff 中存在）
            if file_diff is None:
                stats["dropped_unknown_file"] += 1
                continue

            # 规则 1.5: context_refs 只能引用本次实际注入的上下文。
            if issue.context_refs:
                original_refs = list(issue.context_refs)
                issue.context_refs = [
                    ref for ref in original_refs if ref in valid_context_ids
                ]
                stats["invalid_context_ref"] += len(original_refs) - len(
                    issue.context_refs
                )

            confidence = issue.confidence

            # 规则 2: 行号必须落在 Patch 的新文件侧可见行内
            if issue.line is not None:
                if issue.line not in file_diff.visible_lines:
                    issue.line = None
                    issue.end_line = None
                    confidence -= self.invalid_line_penalty
                    stats["invalid_line"] += 1
                elif issue.end_line is not None:
                    if issue.end_line < issue.line:
                        issue.end_line = None
                    elif issue.end_line not in file_diff.visible_lines:
                        issue.end_line = None
            elif file_diff.visible_lines:
                confidence -= self.missing_line_penalty
                stats["missing_line"] += 1

            # 规则 3: evidence 必须能在真实代码中找到
            grounding, method = self._check_evidence(
                issue, file_diff, contexts, repo_path
            )
            if grounding is None:
                confidence -= self.missing_evidence_penalty
                stats["missing_evidence"] += 1
            elif grounding:
                confidence += self.grounded_evidence_bonus
                stats["grounded_evidence"] += 1
                if method:
                    stats[method] += 1
            else:
                confidence -= self.ungrounded_evidence_penalty
                stats["ungrounded_evidence"] += 1
                logger.debug(
                    "Ungrounded evidence for %s:%s: %r",
                    issue.file,
                    issue.line,
                    issue.evidence,
                )

            # 规则 3.5: suggested_patch 必须能通过 git apply --check。
            # 只做 --check，不写工作区，也不会执行 patch 中的代码。
            patch_valid, patch_error = self._check_suggested_patch(
                issue, repo_path
            )
            if patch_valid is not None:
                stats["patch_checked"] += 1
                issue.suggested_patch_valid = patch_valid
                issue.suggested_patch_error = patch_error
                if patch_valid:
                    stats["patch_valid"] += 1
                else:
                    stats["patch_invalid"] += 1

            issue.confidence = round(max(0.0, min(1.0, confidence)), 2)
            # 规则 4: 过滤低置信度
            if issue.confidence < self.min_confidence:
                stats["low_confidence"] += 1
                continue

            issue.severity = normalize_severity(issue.severity)
            filtered_issues.append(issue)

        # 规则过滤会改变问题集合，风险等级 / 决策 / 证据必须同步重算，
        # 否则报告会出现 “HIGH + 8 issues” 但正文 “No issues found” 的矛盾。
        # 同时再做一次跨维度去重，覆盖不经过 OutputParser.aggregate_aspects 的单 Reviewer 路径。
        before_dedupe = len(filtered_issues)
        filtered_issues = deduplicate_issues(filtered_issues)
        stats["deduplicated"] = before_dedupe - len(filtered_issues)
        stats["kept"] = len(filtered_issues)
        self.last_stats = stats
        result.potential_issues = filtered_issues
        result.risk_level, result.risk_reason = assess_risk(filtered_issues)
        result.final_decision = decide(result.risk_level)
        result.evidence = unique_files(filtered_issues)
        result.final_summary = (
            f"校验后保留 {len(filtered_issues)} 个问题。"
            f"最终决策: {result.final_decision} (风险等级: {result.risk_level})"
        )
        return result

    def _check_evidence(
        self,
        issue: PotentialIssue,
        file_diff: ParsedChangedFile,
        contexts: Sequence[CodeContext],
        repo_path: Optional[Path],
    ) -> Tuple[Optional[bool], Optional[str]]:
        """返回 (True/False/None, 命中方式)。

        校验顺序刻意从严格到宽松：
        1. 行级归一化后精确子串匹配
        2. 逐行 token 包含（处理缩进/空格差异）
        3. 全文 token 覆盖 + 至少两个“有信息量”的 token
        这样既不会因为空格/缩进把真证据判错，也不会让 `return`、`self`
        这类通用 token 把编造的证据蒙混过关。
        """
        evidence = (issue.evidence or "").strip()
        if len(evidence) < 6:
            return None, None

        haystack_parts = [file_diff.patch or ""]
        haystack_parts.extend(
            ctx.snippet for ctx in contexts if ctx.file_path == issue.file
        )

        if repo_path is not None:
            source = self._read_source_for_issue(repo_path, issue)
            if source:
                haystack_parts.append(source)

        haystack = "\n".join(part for part in haystack_parts if part)
        if not haystack:
            return None, None

        normalized_evidence = _normalize_text(evidence)
        normalized_haystack = _normalize_text(haystack)
        if normalized_evidence and normalized_evidence in normalized_haystack:
            return True, "evidence_exact"

        evidence_lines = [
            _normalize_text(line)
            for line in evidence.splitlines()
            if len(TOKEN_PATTERN.findall(line)) >= 2
        ]
        haystack_lines = [
            _normalize_text(line) for line in haystack.splitlines() if line.strip()
        ]
        if evidence_lines and all(
            any(_line_token_containment(line, hay) >= 0.9 for hay in haystack_lines)
            for line in evidence_lines
        ):
            return True, "evidence_line_match"

        evidence_tokens = set(TOKEN_PATTERN.findall(evidence.lower()))
        if len(evidence_tokens) < 3:
            return False, None

        haystack_tokens = set(TOKEN_PATTERN.findall(haystack.lower()))
        overlap = len(evidence_tokens & haystack_tokens) / len(evidence_tokens)
        distinctive = evidence_tokens - COMMON_EVIDENCE_TOKENS
        if overlap >= 0.85 and len(distinctive) >= 2:
            return True, "evidence_token_match"
        return False, None

    @staticmethod
    def _check_suggested_patch(
        issue: PotentialIssue, repo_path: Optional[Path]
    ) -> Tuple[Optional[bool], Optional[str]]:
        """用 ``git apply --check`` 验证 patch 是否真的能应用。

        - 只读检查，不修改工作区
        - 不执行 patch 里的任何代码
        - 模型经常只给 hunk、不带 file header，这里自动补 `---/+++`
        """
        patch = (issue.suggested_patch or "").strip()
        if not patch or repo_path is None:
            return None, None
        if len(patch) > MAX_SUGGESTED_PATCH_SIZE:
            return False, "patch too large"

        repo_path = Path(repo_path).resolve()
        if not (repo_path / issue.file).is_file():
            return False, f"file not found: {issue.file}"

        if not patch.startswith(("diff --git", "--- ")):
            patch = f"--- a/{issue.file}\n+++ b/{issue.file}\n{patch}"
        if not patch.endswith("\n"):
            patch += "\n"

        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "apply",
                    "--check",
                    "--whitespace=nowarn",
                    "-",
                ],
                input=patch.encode("utf-8", errors="ignore"),
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"git apply failed: {type(exc).__name__}"

        if completed.returncode == 0:
            return True, None
        error = completed.stderr.decode("utf-8", errors="ignore").strip()
        # stderr 可能有多行；保留最后一行，避免把整段 git 输出塞进报告。
        last_line = error.splitlines()[-1] if error else "patch does not apply"
        return False, last_line[:300]

    @staticmethod
    def _read_source_for_issue(
        repo_path: Path, issue: PotentialIssue
    ) -> Optional[str]:
        repo_path = Path(repo_path).resolve()
        candidate = (repo_path / issue.file).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError:
            return None

        try:
            if not candidate.is_file() or candidate.stat().st_size > MAX_GROUNDING_FILE_SIZE:
                return None
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        lines = text.splitlines()
        if not lines:
            return text

        if issue.line is None:
            # 没有行号时只看文件头部，避免“证据在文件任意角落”造成假阳性。
            return "\n".join(lines[:200])

        # 优先用 AST 包围定义作为证据窗口；解析失败再退回 ±20 行。
        try:
            span = ast_utils.enclosing_definition_span(candidate, issue.line)
        except Exception:  # noqa: BLE001 - parser 失败只影响证据增强
            span = None
        if span is not None:
            start, end = span
            return "\n".join(lines[max(0, start - 1) : min(len(lines), end)])

        start = max(0, issue.line - 21)
        end = min(len(lines), issue.line + 20)
        return "\n".join(lines[start:end])


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def _line_token_containment(evidence_line: str, haystack_line: str) -> float:
    evidence_tokens = set(TOKEN_PATTERN.findall(evidence_line))
    if not evidence_tokens:
        return 0.0
    haystack_tokens = set(TOKEN_PATTERN.findall(haystack_line))
    return len(evidence_tokens & haystack_tokens) / len(evidence_tokens)
