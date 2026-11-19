from types import SimpleNamespace

from openreview.services.metrics_collector import MetricsCollector


def test_call_details_are_scoped_to_current_run():
    """pipeline 复用同一个 LLM client；第二次 run 不能重复统计第一次的调用。"""
    state = SimpleNamespace(run_id="run-2", llm_call_start=1)
    client = SimpleNamespace(
        call_log=[
            {"model": "m", "latency_ms": 10, "total_tokens": 100},
            {"model": "m", "latency_ms": 20, "total_tokens": 200},
        ]
    )

    details = MetricsCollector._call_details(state, client)

    assert len(details) == 1
    assert details[0]["total_tokens"] == 200
    assert details[0]["run_id"] == "run-2"


def test_verification_metrics_separate_drops_from_events():
    state = SimpleNamespace(
        validation_stats={
            "candidates": 10,
            "validator_kept": 3,
            "dropped_unknown_file": 1,
            "dropped_pre_existing": 1,
            "invalid_line": 1,
            "low_confidence": 2,
            "deduplicated": 1,
            "cross_batch_deduplicated": 1,
            "patch_invalid": 2,
            "ungrounded_evidence": 3,
            "missing_evidence": 1,
            "line_relocated": 1,
        },
        verifier_stats={
            "candidates": 2,
            "verified": 0,
            "rejected": 1,
            "needs_more_context": 1,
            "dropped_low_confidence": 0,
        },
        review_result=SimpleNamespace(potential_issues=[object(), object()]),
    )

    metrics = MetricsCollector._verification_metrics(state)

    assert metrics["available"] is True
    assert metrics["stages"] == {
        "raw_count": 10,
        "validator_kept": 3,
        "verifier_candidates": 2,
        "final_count": 2,
    }
    assert metrics["drops"] == {
        "validator": {
            "by_diff_scope": 3,
            "by_evidence_mismatch": 0,
            "by_low_confidence": 2,
            "deduplicated": 2,
        },
        "verifier": {"rejected": 1, "dropped_by_low_confidence": 0},
    }
    assert metrics["events"] == {
        "stripped_invalid_patch": 2,
        "ungrounded_evidence": 3,
        "missing_evidence": 1,
        "line_relocated": 1,
        "needs_more_context": 1,
        "not_checked_by_verifier": 0,
    }
    assert metrics["conservation"] == {
        "validator_balanced": True,
        "verifier_balanced": True,
    }
    assert (
        metrics["stages"]["raw_count"]
        == metrics["stages"]["validator_kept"]
        + sum(metrics["drops"]["validator"].values())
    )
    assert (
        metrics["stages"]["final_count"]
        == metrics["stages"]["validator_kept"]
        - metrics["drops"]["verifier"]["rejected"]
        - metrics["drops"]["verifier"]["dropped_by_low_confidence"]
    )
