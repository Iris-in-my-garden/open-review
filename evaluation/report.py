"""把多次评测 run 汇总成一张 Markdown 结果表。

评测通常会产生三类文件：

- ``deterministic_<run_id>.json``：validity / evidence / attribution / token 用量
- ``retrieval_<run_id>.json``：检索覆盖与 citation
- ``metrics_openreview_<timestamp>.json``：AACR 官方语义 P/R（可选）

这个模块只做汇总，不调用模型、不重新计算指标，因此可以离线跑，也适合放进 CI。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


@dataclass(frozen=True)
class RunSpec:
    """一次评测 run 的展示名和 metrics 目录。"""

    label: str
    path: Path
    semantic_path: Optional[Path] = None


def parse_run_spec(raw: str, *, option: str = "--run") -> RunSpec:
    """解析 ``label=path`` 形式，例如 ``single=evaluation/metrics/...``。"""
    label, separator, path = raw.partition("=")
    label = label.strip()
    path = path.strip()
    if not separator or not label or not path:
        raise ValueError(f"{option} 需要 label=path 形式，收到: {raw!r}")
    return RunSpec(label=label, path=Path(path).expanduser())


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 JSON: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层不是对象: {path}")
    return payload


def _latest(directory: Path, pattern: str) -> Optional[Path]:
    candidates = sorted(
        directory.glob(pattern),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_run(spec: RunSpec) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """读取一次 run 的 deterministic / retrieval / semantic 数据。

    semantic 文件是可选的；没有跑 AACR judge 时返回空 dict，表格里对应列显示 ``-``。
    """
    metrics_dir = spec.path
    if not metrics_dir.exists():
        raise FileNotFoundError(f"metrics 目录不存在: {metrics_dir}")
    if not metrics_dir.is_dir():
        raise ValueError(f"期望 metrics 目录，收到文件: {metrics_dir}")

    deterministic_path = _latest(metrics_dir, "deterministic*.json")
    if deterministic_path is None:
        raise FileNotFoundError(
            f"{metrics_dir} 下没有 deterministic*.json；请先运行 metrics 子命令"
        )
    deterministic = _load_json(deterministic_path)

    retrieval_path = _latest(metrics_dir, "retrieval*.json")
    retrieval = _load_json(retrieval_path) if retrieval_path else {}

    semantic_path = spec.semantic_path or _latest(
        metrics_dir, "metrics_openreview_*.json"
    )
    semantic = _load_json(semantic_path) if semantic_path else {}

    return deterministic, retrieval, semantic


def _semantic_summary(semantic: Dict[str, Any]) -> Dict[str, Any]:
    summary = semantic.get("summary")
    return summary if isinstance(summary, dict) else {}


def _first_present(summary: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        value = summary.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def build_row(
    label: str,
    deterministic: Dict[str, Any],
    retrieval: Dict[str, Any],
    semantic: Dict[str, Any],
) -> Dict[str, Any]:
    """把一次 run 的 JSON 压成表格需要的一行。"""
    cost = deterministic.get("cost") or {}
    validity = deterministic.get("validity") or {}
    evidence = deterministic.get("evidence") or {}
    attribution = deterministic.get("attribution") or {}
    retrieval_metrics = retrieval.get("retrieval") or {}
    semantic_summary = _semantic_summary(semantic)

    precision = _first_present(
        semantic_summary,
        ("file_semantic_match_rate", "semantic_match_rate"),
    )
    recall = _first_present(
        semantic_summary,
        ("file_semantic_recall_rate", "semantic_recall_rate"),
    )
    balanced = _first_present(
        semantic_summary,
        ("balanced_score", "semantic_f1"),
    )

    input_tokens = int(cost.get("input_tokens") or 0)
    output_tokens = int(cost.get("output_tokens") or 0)

    return {
        "config": label,
        "instances": int(deterministic.get("results_ok") or 0),
        "total_instances": int(deterministic.get("instances") or 0),
        "issues": int(deterministic.get("issues") or 0),
        "precision": precision,
        "recall": recall,
        "balanced": balanced,
        "invalid_line_rate": validity.get("invalid_line_rate"),
        "evidence_coverage": evidence.get("coverage"),
        "retrieval_file_recall": retrieval_metrics.get("file_recall"),
        "retrieval_symbol_coverage": retrieval_metrics.get("symbol_coverage"),
        "retrieval_citation_rate": retrieval_metrics.get("citation_rate"),
        "graph_contexts_avg": retrieval_metrics.get("graph_contexts_avg"),
        "attribution_coverage": attribution.get("coverage"),
        "llm_calls": int(cost.get("llm_calls") or 0),
        "llm_calls_avg": cost.get("llm_calls_avg"),
        "duration_seconds_avg": cost.get("duration_seconds_avg"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _format_rate(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{float(value) * 100:.1f}%"


def _format_number(value: Any, *, digits: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}"


def _format_tokens(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return str(int(number))


def _format_latency(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):.1f}s"


def render_table(rows: Sequence[Dict[str, Any]]) -> str:
    headers = [
        "Config",
        "N",
        "Findings",
        "Precision",
        "Recall",
        "Balanced",
        "Invalid line",
        "Evidence",
        "File recall",
        "Symbol cov",
        "Citation",
        "Graph ctx",
        "Attribution",
        "Calls/PR",
        "Tokens",
        "Latency",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for row in rows:
        n = f"{row['instances']}/{row['total_instances']}"
        calls_avg = row.get("llm_calls_avg")
        if not isinstance(calls_avg, (int, float)):
            calls_avg = (
                row["llm_calls"] / row["instances"] if row["instances"] else 0
            )
        values = [
            str(row["config"]),
            n,
            str(row["issues"]),
            _format_rate(row.get("precision")),
            _format_rate(row.get("recall")),
            _format_rate(row.get("balanced")),
            _format_rate(row.get("invalid_line_rate")),
            _format_rate(row.get("evidence_coverage")),
            _format_rate(row.get("retrieval_file_recall")),
            _format_rate(row.get("retrieval_symbol_coverage")),
            _format_rate(row.get("retrieval_citation_rate")),
            _format_number(row.get("graph_contexts_avg")),
            _format_rate(row.get("attribution_coverage")),
            _format_number(calls_avg),
            _format_tokens(row.get("total_tokens")),
            _format_latency(row.get("duration_seconds_avg")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def render_report(
    specs: Sequence[RunSpec],
    rows: Sequence[Dict[str, Any]],
    *,
    title: str = "OpenReview 评测结果",
) -> str:
    lines = [
        f"# {title}",
        "",
        "> 由 `python -m evaluation.cli report` 生成。"
        "Precision / Recall / Balanced 来自 AACR 官方语义 judge；"
        "未跑 semantic 时显示 `-`。",
        "",
        "## 汇总",
        "",
        render_table(rows),
        "",
        "## 指标说明",
        "",
        "- **N**: 成功结果数 / 计划样本数",
        "- **Invalid line**: finding 行号不在 diff 可见行内的比例，越低越好",
        "- **Evidence**: evidence 能在 head 源码或 patch 中定位的比例，越高越好",
        "- **File recall**: reference comment 指向文件被检索上下文覆盖的比例",
        "- **Symbol cov**: 变更符号被检索上下文覆盖的比例",
        "- **Citation**: finding 显式引用 `[ctx-xxxxxxxx]` 的比例",
        "- **Graph ctx**: 每个样本由 import/call graph 实际注入的上下文数",
        "- **Attribution**: 带 `source_aspects` 的 finding 比例",
        "- **Calls/PR**: 每个成功样本平均 LLM 调用数",
        "- **Tokens**: 成功样本的 input + output token 总量",
        "",
        "## 数据来源",
        "",
    ]
    for spec in specs:
        lines.append(f"- **{spec.label}**: `{spec.path}`")
    lines.append("")
    return "\n".join(lines)


def write_report(text: str, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path
