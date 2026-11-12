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
