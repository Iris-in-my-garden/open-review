"""Best-effort secret redaction for untrusted PR content.

The reviewer is explicitly asked to find hardcoded credentials. That makes it
the one tool that must not send those credentials to a third-party model or
echo them back into a public PR comment.

Redaction is intentionally conservative: only high-confidence token formats
and long credential assignments are masked. Masking every quoted string would
destroy code context and create false positives in the evidence validator.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

REDACTED_TEMPLATE = "<REDACTED:{kind}>"

_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "GITHUB_PAT",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "JWT",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
            r"\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "BEARER_TOKEN",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ),
    (
        "CREDENTIAL_ASSIGNMENT",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
            r"auth[_-]?token)\b\s*[:=]\s*"
            r"(?P<quote>['\"])[^'\"\n]{16,}(?P=quote)"
        ),
    ),
    (
        "URL_CREDENTIALS",
        re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
    ),
    (
        "INTERNAL_IP",
        re.compile(
            r"(?<![\w.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\w.])"
        ),
    ),
)


def _mask_url_credentials(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}<REDACTED:URL_CREDENTIALS>@"


def redact_text(text: str | None) -> Tuple[str, int]:
    """Return ``(redacted_text, replacement_count)``."""
    if not text:
        return text or "", 0
    redacted = text
    count = 0
    for kind, pattern in _PATTERNS:
        if kind == "URL_CREDENTIALS":
            redacted, replacements = pattern.subn(_mask_url_credentials, redacted)
        else:
            redacted, replacements = pattern.subn(
                REDACTED_TEMPLATE.format(kind=kind), redacted
            )
        count += replacements
    return redacted, count


def redact_many(values: Iterable[str | None]) -> Tuple[List[str], int]:
    output: List[str] = []
    total = 0
    for value in values:
        redacted, count = redact_text(value)
        output.append(redacted)
        total += count
    return output, total


__all__ = ["redact_many", "redact_text"]
