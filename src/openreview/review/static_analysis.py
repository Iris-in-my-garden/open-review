"""可选的静态分析信号。

LLM 不擅长凭空发现所有机械性问题。这里把 ruff / mypy 的结果作为证据源：
- 静态工具负责发现确定性问题
- LLM 负责解释、归类、合并与给出修复建议
- 静态结果只作为 candidate / context，不直接替代最终结论

默认只跑 ruff；mypy 可按需开启。所有命令都限制超时，且只对变更文件运行。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Literal, Optional

from pydantic import BaseModel, Field

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext

logger = logging.getLogger(__name__)

MAX_STATIC_FINDINGS = 40
MAX_STATIC_CONTEXTS = 20
SUPPORTED_TOOLS = ("ruff", "mypy")
Severity = Literal["critical", "high", "medium", "low"]


class StaticFinding(BaseModel):
    tool: str
    file: str
    line: int
    code: str = ""
    message: str
    severity: Severity = "medium"


class StaticAnalysisReport(BaseModel):
    findings: List[StaticFinding] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    def to_notes(self) -> List[str]:
        if not self.findings:
            return []
        notes = [
            f"[static] 工具发现 {len(self.findings)} 个信号，已作为审查证据注入。"
        ]
        for item in self.findings[:10]:
            notes.append(
                f"[static] `{item.file}:{item.line}` {item.tool} "
                f"{item.code}: {item.message}"
            )
        if len(self.findings) > 10:
            notes.append(f"[static] 其余 {len(self.findings) - 10} 条已省略。")
        return notes

    def to_contexts(self, repo_path: Path) -> List[CodeContext]:
        contexts: List[CodeContext] = []
        for item in self.findings[:MAX_STATIC_CONTEXTS]:
            snippet = _read_snippet(repo_path, item.file, item.line)
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=item.file,
                    reason=f"static:{item.tool}:{item.code}",
                    snippet=snippet,
                    score=0.6,
                )
            )
        return contexts


class StaticAnalysisRunner:
    def __init__(
        self,
        tools: Optional[Iterable[str]] = None,
        timeout: int = 60,
        max_findings: int = MAX_STATIC_FINDINGS,
    ):
        requested = list(tools or ["ruff"])
        self.tools = [tool for tool in requested if tool in SUPPORTED_TOOLS]
        self.timeout = timeout
        self.max_findings = max_findings

    def run(self, repo_path: Path, parsed_diff: ParsedDiff) -> StaticAnalysisReport:
        repo_path = Path(repo_path).resolve()
        python_files = [
            item.filename
            for item in parsed_diff.files
            if item.filename.endswith(".py") and not item.is_docs_file
        ]
        if not python_files:
            return StaticAnalysisReport()

        findings: List[StaticFinding] = []
        errors: List[str] = []
        for tool in self.tools:
            executable = shutil.which(tool)
            if executable is None:
                errors.append(f"{tool} not installed")
                continue
            if tool == "ruff":
                findings.extend(self._run_ruff(executable, repo_path, python_files, errors))
            elif tool == "mypy":
                findings.extend(self._run_mypy(executable, repo_path, python_files, errors))
            if len(findings) >= self.max_findings:
                break

        return StaticAnalysisReport(findings=findings[: self.max_findings], errors=errors)

    def _run_ruff(
        self,
        executable: str,
        repo_path: Path,
        files: List[str],
        errors: List[str],
    ) -> List[StaticFinding]:
        command = [
            executable,
            "check",
            "--output-format=json",
            "--no-cache",
            *files,
        ]
        result = self._run(command, repo_path, errors)
        if result is None:
            return []
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            errors.append(f"ruff JSON parse error: {exc}")
            return []

        findings: List[StaticFinding] = []
        for item in payload:
            try:
                findings.append(
                    StaticFinding(
                        tool="ruff",
                        file=item["filename"],
                        line=int(item["location"]["row"]),
                        code=item.get("code") or "",
                        message=item.get("message", ""),
                        severity=_ruff_severity(item.get("code") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return findings

    def _run_mypy(
        self,
        executable: str,
        repo_path: Path,
        files: List[str],
        errors: List[str],
    ) -> List[StaticFinding]:
        command = [
            executable,
            "--no-error-summary",
            "--show-column-numbers",
            "--hide-error-context",
            *files,
        ]
        result = self._run(command, repo_path, errors)
        if result is None:
            return []

        findings: List[StaticFinding] = []
        pattern = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?\s*(?P<level>\w+):\s*(?P<msg>.*)$")
        for line in (result.stdout or "").splitlines():
            match = pattern.match(line)
            if not match:
                continue
            findings.append(
                StaticFinding(
                    tool="mypy",
                    file=match.group("file"),
                    line=int(match.group("line")),
                    code="mypy",
                    message=match.group("msg"),
                    severity="high" if match.group("level") == "error" else "medium",
                )
            )
        return findings

    def _run(
        self, command: List[str], repo_path: Path, errors: List[str]
    ) -> Optional[subprocess.CompletedProcess]:
        try:
            result = subprocess.run(
                command,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{command[0]}: {exc}")
            return None

        # ruff 用 1 表示“发现问题”，mypy 用 1 表示类型错误；都不是执行失败。
        if result.returncode not in (0, 1):
            errors.append(
                f"{command[0]} exited {result.returncode}: "
                f"{(result.stderr or '').strip()[:300]}"
            )
            return None
        return result


def _ruff_severity(code: str) -> Severity:
    if code.startswith(("F", "E9")):
        return "high"
    if code.startswith("B"):
        return "medium"
    return "low"


def _read_snippet(repo_path: Path, filename: str, line: int) -> str:
    candidate = (repo_path / filename).resolve()
    try:
        candidate.relative_to(repo_path)
    except ValueError:
        return ""
    try:
        lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    start = max(0, line - 3)
    end = min(len(lines), line + 2)
    return "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end))
