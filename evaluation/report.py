"""把多次评测 run 汇总成一张 Markdown 结果表。

评测通常会产生三类文件：

- ``deterministic_<run_id>.json``：validity / evidence / attribution / token 用量
- ``retrieval_<run_id>.json``：检索覆盖与 citation
- ``metrics_openreview_<timestamp>.json``：AACR 官方语义 P/R（可选）

这个模块只做汇总，不调用模型、不重新计算指标，因此可以离线跑，也适合放进 CI。
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
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


def _semantic_rates(counts: Dict[str, int]) -> Dict[str, Optional[float]]:
    generated = counts["generated"]
    expected = counts["expected"]
    precision = counts["matched_generated"] / generated if generated else 0.0
    recall = counts["matched_reference"] / expected if expected else 0.0
    balanced = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "instances": counts["instances"],
        "generated": generated,
        "expected": expected,
        "precision": precision,
        "recall": recall,
        "balanced": balanced,
    }


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_confidence_intervals(
    repo_counts: Dict[str, Dict[str, Any]],
    *,
    iterations: int = 2000,
    seed: int = 42,
) -> Dict[str, Any]:
    """Stratified cluster bootstrap: resample repositories within each language."""
    repos_by_language: Dict[str, list[str]] = defaultdict(list)
    for repo, counts in repo_counts.items():
        language = counts.get("language")
        if language:
            repos_by_language[str(language)].append(repo)
    for repos in repos_by_language.values():
        repos.sort()

    if not repos_by_language:
        return {}
    insufficient_languages = sorted(
        language
        for language, repos in repos_by_language.items()
        if len(repos) < 2
    )

    rng = random.Random(seed)
    samples: Dict[str, Dict[str, list[float]]] = {
        scope: {metric: [] for metric in ("precision", "recall", "balanced")}
        for scope in ("overall", *sorted(repos_by_language))
    }
    for _ in range(iterations):
        overall = {
            "instances": 0,
            "generated": 0,
            "expected": 0,
            "matched_generated": 0,
            "matched_reference": 0,
        }
        for language, repos in sorted(repos_by_language.items()):
            language_total = {
                "instances": 0,
                "generated": 0,
                "expected": 0,
                "matched_generated": 0,
                "matched_reference": 0,
            }
            for repo in rng.choices(repos, k=len(repos)):
                source = repo_counts[repo]
                for key in language_total:
                    language_total[key] += int(source.get(key, 0))
            for key in overall:
                overall[key] += language_total[key]
            rates = _semantic_rates(language_total)
            for metric in ("precision", "recall", "balanced"):
                samples[language][metric].append(float(rates[metric] or 0.0))

        rates = _semantic_rates(overall)
        for metric in ("precision", "recall", "balanced"):
            samples["overall"][metric].append(float(rates[metric] or 0.0))

    intervals: Dict[str, Any] = {}
    for scope, metrics in samples.items():
        scope_has_enough_clusters = (
            not insufficient_languages
            if scope == "overall"
            else len(repos_by_language[scope]) >= 2
        )
        intervals[scope] = {
            metric: (
                [
                    _percentile(values, 0.025),
                    _percentile(values, 0.975),
                ]
                if scope_has_enough_clusters
                else [None, None]
            )
            for metric, values in metrics.items()
        }
    return {
        "method": "language-stratified repository-cluster percentile bootstrap",
        "confidence_level": 0.95,
        "iterations": iterations,
        "seed": seed,
        "repositories_by_language": {
            language: len(repos)
            for language, repos in sorted(repos_by_language.items())
        },
        "insufficient_languages": insufficient_languages,
        "intervals": intervals,
    }


def _semantic_strata(
    deterministic: Dict[str, Any], semantic: Dict[str, Any]
) -> Dict[str, Any]:
    """Aggregate AACR's per-instance judge records by repository and language."""
    if (semantic.get("ex_info") or {}).get("score_mode", "file_semantic") != (
        "file_semantic"
    ):
        return {}
    eval_rows = semantic.get("eval_res")
    if not isinstance(eval_rows, list):
        return {}

    metadata = {
        str(row.get("instance_id")): row
        for row in (deterministic.get("per_instance") or [])
        if isinstance(row, dict) and row.get("instance_id")
    }
    repos: Dict[str, Dict[str, Any]] = {}
    languages: Dict[str, Dict[str, Any]] = {}
    by_instance: Dict[str, Dict[str, Any]] = {}
    for row in eval_rows:
        if not isinstance(row, dict):
            continue
        comments = row.get("comments")
        generated = row.get("generated_comments")
        if not isinstance(comments, list) or not isinstance(generated, list):
            continue
        instance = metadata.get(str(row.get("instance_id")), {})
        repo = str(instance.get("repo") or row.get("repo") or "")
        language = str(instance.get("language") or "")
        if not repo:
            continue
        instance_counts = {
            "instances": 1,
            "generated": len(generated),
            "expected": sum(
                1 for comment in comments if isinstance(comment, dict)
            ),
            "matched_generated": sum(
                1
                for comment in generated
                if isinstance(comment, dict) and comment.get("hit_new_gt")
            ),
            "matched_reference": sum(
                1
                for comment in comments
                if isinstance(comment, dict)
                and comment.get("file_semantic_match")
            ),
        }
        instance_id = str(row.get("instance_id") or "")
        if instance_id:
            by_instance[instance_id] = {
                **instance_counts,
                "repo": repo,
                "language": language or "unknown",
            }
        if repo not in repos:
            repos[repo] = {
                **{key: 0 for key in instance_counts},
                "language": language or "unknown",
            }
        elif language and repos[repo].get("language") not in (
            "unknown",
            language,
        ):
            raise ValueError(f"仓库 {repo} 在语义结果中出现多个语言")
        if language:
            repos[repo]["language"] = language
        for key, value in instance_counts.items():
            repos[repo][key] += value

        group_key = language or "unknown"
        if group_key not in languages:
            languages[group_key] = {key: 0 for key in instance_counts}
        for key, value in instance_counts.items():
            languages[group_key][key] += value

    if not repos:
        return {}
    by_repo = {
        repo: _semantic_rates(counts) for repo, counts in sorted(repos.items())
    }
    by_language = {
        language: _semantic_rates(counts)
        for language, counts in sorted(languages.items())
    }
    return {
        "by_language": by_language,
        "by_repo": by_repo,
        "by_instance": by_instance,
        "bootstrap_95_ci": _bootstrap_confidence_intervals(repos),
    }


def _paired_bootstrap_comparison(
    left: Dict[str, Any],
    right: Dict[str, Any],
    *,
    iterations: int = 2000,
    seed: int = 42,
) -> Dict[str, Any]:
    left_instances = (left.get("semantic_strata") or {}).get("by_instance") or {}
    right_instances = (right.get("semantic_strata") or {}).get("by_instance") or {}
    common_ids = sorted(set(left_instances) & set(right_instances))
    if not common_ids:
        return {}

    ids_by_language: Dict[str, list[str]] = defaultdict(list)
    for instance_id in common_ids:
        row = left_instances[instance_id]
        language = str(row.get("language") or "unknown")
        ids_by_language[language].append(instance_id)
        right_language = str(
            right_instances[instance_id].get("language") or "unknown"
        )
        if language != right_language:
            raise ValueError(
                f"PR {instance_id} 在两个 run 中语言不一致: "
                f"{language} / {right_language}"
            )

    keys = (
        "instances",
        "generated",
        "expected",
        "matched_generated",
        "matched_reference",
    )
    left_total = {key: 0 for key in keys}
    right_total = {key: 0 for key in keys}
    for instance_id in common_ids:
        for key in keys:
            left_total[key] += int(left_instances[instance_id].get(key, 0))
            right_total[key] += int(right_instances[instance_id].get(key, 0))
    left_rates = _semantic_rates(left_total)
    right_rates = _semantic_rates(right_total)
    metrics = ("precision", "recall", "balanced")
    point_delta = {
        metric: float(left_rates[metric] or 0.0)
        - float(right_rates[metric] or 0.0)
        for metric in metrics
    }

    rng = random.Random(seed)
    samples: Dict[str, list[float]] = {metric: [] for metric in metrics}
    enough_pairs_per_stratum = all(
        len(instance_ids) >= 2 for instance_ids in ids_by_language.values()
    )
    for _ in range(iterations):
        sampled_left = {key: 0 for key in keys}
        sampled_right = {key: 0 for key in keys}
        for instance_ids in ids_by_language.values():
            for instance_id in rng.choices(instance_ids, k=len(instance_ids)):
                for key in keys:
                    sampled_left[key] += int(left_instances[instance_id].get(key, 0))
                    sampled_right[key] += int(right_instances[instance_id].get(key, 0))
        left_sample_rates = _semantic_rates(sampled_left)
        right_sample_rates = _semantic_rates(sampled_right)
        for metric in metrics:
            samples[metric].append(
                float(left_sample_rates[metric] or 0.0)
                - float(right_sample_rates[metric] or 0.0)
            )

    return {
        "left": left["config"],
        "right": right["config"],
        "paired_instances": len(common_ids),
        "language_strata": {
            language: len(instance_ids)
            for language, instance_ids in sorted(ids_by_language.items())
        },
        "delta_left_minus_right": point_delta,
        "bootstrap_95_ci": {
            metric: (
                [
                    _percentile(values, 0.025),
                    _percentile(values, 0.975),
                ]
                if enough_pairs_per_stratum
                else [None, None]
            )
            for metric, values in samples.items()
        },
        "sufficient_pairs_per_language": enough_pairs_per_stratum,
        "method": "language-stratified paired PR bootstrap",
        "iterations": iterations,
        "seed": seed,
    }


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
        "evidence_evaluable_rate": evidence.get("evaluable_rate"),
        "evidence_unevaluable": evidence.get("unevaluable"),
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
        "semantic_strata": _semantic_strata(deterministic, semantic),
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


def _format_ci(values: Any) -> str:
    if (
        not isinstance(values, list)
        or len(values) != 2
        or not all(isinstance(value, (int, float)) for value in values)
    ):
        return "-"
    return f"{float(values[0]) * 100:.1f}–{float(values[1]) * 100:.1f}%"


def _format_delta(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{float(value) * 100:+.1f}%"


def render_table(rows: Sequence[Dict[str, Any]]) -> str:
    headers = [
        "Config",
        "N",
        "Findings",
        "Precision",
        "Recall",
        "Balanced",
        "Invalid line",
        "Evidence eval",
        "Evidence",
        "Unevaluable",
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
            _format_rate(row.get("evidence_evaluable_rate")),
            _format_rate(row.get("evidence_coverage")),
            _format_number(row.get("evidence_unevaluable")),
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
        "- **Evidence eval**: 有 evidence 的 finding 中，行号与对应 commit 源码可核验的比例",
        "- **Evidence**: 核验成功的 finding 中，精确源码证据匹配比例",
        "- **Unevaluable**: 因 diff、文件、行号或源码不可用而无法核验的 evidence 数",
        "- **Unevaluable**: 因 diff、文件、行号或源码不可用而无法核验的 evidence 数",
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
    stratified_rows = [
        row for row in rows if row.get("semantic_strata")
    ]
    if stratified_rows:
        lines.extend(
            [
                "",
                "## 语义指标分层与置信区间",
                "",
                "分层指标基于 AACR 每个 PR 的 judge 记录计算。单 run 的 95% CI 使用"
                "语言分层的仓库簇 bootstrap；配置对比则对相同 PR 做配对 bootstrap。"
                "固定 seed=42；任一语言少于 2 个仓库簇/配对 PR 时不报告区间，"
                "以免给出虚假的零宽区间。",
                "",
            ]
        )
        for row in stratified_rows:
            strata = row["semantic_strata"]
            lines.extend(
                [
                    f"### {row['config']}",
                    "",
                    "| Group | N | Precision | Recall | Balanced |",
                    "|---|---:|---:|---:|---:|",
                ]
            )
            for language, metrics in strata.get("by_language", {}).items():
                lines.append(
                    "| {} | {} | {} | {} | {} |".format(
                        language,
                        metrics["instances"],
                        _format_rate(metrics["precision"]),
                        _format_rate(metrics["recall"]),
                        _format_rate(metrics["balanced"]),
                    )
                )
            for repo, metrics in strata.get("by_repo", {}).items():
                lines.append(
                    "| {} | {} | {} | {} | {} |".format(
                        repo,
                        metrics["instances"],
                        _format_rate(metrics["precision"]),
                        _format_rate(metrics["recall"]),
                        _format_rate(metrics["balanced"]),
                    )
                )
            lines.extend(
                [
                    "",
                    "| Scope | Precision 95% CI | Recall 95% CI | Balanced 95% CI |",
                    "|---|---:|---:|---:|",
                ]
            )
            intervals = (
                strata.get("bootstrap_95_ci", {}).get("intervals", {})
            )
            for scope in ("overall", *strata.get("by_language", {}).keys()):
                interval = intervals.get(scope, {})
                lines.append(
                    "| {} | {} | {} | {} |".format(
                        scope,
                        _format_ci(interval.get("precision")),
                        _format_ci(interval.get("recall")),
                        _format_ci(interval.get("balanced")),
                    )
                )
            lines.append("")
    comparisons = [
        _paired_bootstrap_comparison(left, right)
        for left_index, left in enumerate(stratified_rows)
        for right in stratified_rows[left_index + 1 :]
    ]
    comparisons = [comparison for comparison in comparisons if comparison]
    if comparisons:
        lines.extend(
            [
                "## 配置间配对比较",
                "",
                "对相同 PR 计算左侧配置减右侧配置的差值，并在语言层内对 PR 有放回"
                "抽样；每次抽样在两个配置间使用相同的 PR，给出配对 95% bootstrap CI。",
                "",
                "| Comparison | Paired PRs | Metric | Δ (left − right) | 95% paired bootstrap CI |",
                "|---|---:|---|---:|---:|",
            ]
        )
        for comparison in comparisons:
            deltas = comparison["delta_left_minus_right"]
            intervals = comparison["bootstrap_95_ci"]
            for metric in ("precision", "recall", "balanced"):
                lines.append(
                    "| {} vs {} | {} | {} | {} | {} |".format(
                        comparison["left"],
                        comparison["right"],
                        comparison["paired_instances"],
                        metric,
                        _format_delta(deltas[metric]),
                        _format_ci(intervals[metric]),
                    )
                )
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def write_report(text: str, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path
