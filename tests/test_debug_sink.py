import json
import threading
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from openreview.review.debug_sink import DebugSink


class _Model(BaseModel):
    value: int


class _Result(BaseModel):
    metrics: dict


def test_debug_sink_records_prompts_responses_and_decisions(tmp_path: Path):
    sink = DebugSink(tmp_path / "debug")

    sink.record_prompt("single_shot", "system prompt", "user prompt")
    sink.record_llm_response("single_shot", _Model(value=1))
    sink.record_validation([{"status": "kept", "confidence_after": 0.9}])
    sink.record_verifier([{"verdict": "verified"}])

    assert (
        sink.root / "prompts" / "01_single_shot.system.txt"
    ).read_text(encoding="utf-8") == "system prompt"
    assert (
        sink.root / "prompts" / "01_single_shot.user.txt"
    ).read_text(encoding="utf-8") == "user prompt"
    assert json.loads(
        (sink.root / "llm" / "01_single_shot.parsed.json").read_text(
            encoding="utf-8"
        )
    ) == {"value": 1}
    assert json.loads(
        (sink.root / "validation" / "01.json").read_text(encoding="utf-8")
    ) == [{"status": "kept", "confidence_after": 0.9}]
    assert json.loads(
        (sink.root / "verifier" / "01.json").read_text(encoding="utf-8")
    ) == [{"verdict": "verified"}]


def test_debug_sink_is_thread_safe(tmp_path: Path):
    sink = DebugSink(tmp_path / "debug")
    workers = 8
    per_worker = 20

    def worker(worker_id: int) -> None:
        for index in range(per_worker):
            sink.record_prompt(f"w{worker_id}-{index}", "s", "u")

    threads = [
        threading.Thread(target=worker, args=(worker_id,))
        for worker_id in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    files = list((sink.root / "prompts").glob("*.user.txt"))
    assert len(files) == workers * per_worker


def test_debug_sink_record_state_writes_state_and_metrics(tmp_path: Path):
    sink = DebugSink(tmp_path / "debug")
    state = SimpleNamespace(
        pr_url="https://example.com/pr/1",
        pr_info=None,
        parsed_diff=None,
        batches=[],
        all_contexts=[],
        retrieval_stats={"hits": 2},
        validation_stats={"kept": 1},
        verifier_stats={"verified": 1},
        impact_findings=[],
        static_findings=[],
        notes=["note"],
        degraded_reasons=[],
        stages={"analyze": 1.0},
        trace=[],
        outcome=None,
        review_result=_Result(metrics={"llm_calls": 1}),
    )

    sink.record_state(state)

    payload = json.loads(
        (sink.root / "state.json").read_text(encoding="utf-8")
    )
    assert payload["pr_url"] == "https://example.com/pr/1"
    assert payload["stats"]["retrieval"] == {"hits": 2}
    assert payload["notes"] == ["note"]
    assert payload["result"] == {"metrics": {"llm_calls": 1}}

    metrics = json.loads(
        (sink.root / "metrics.json").read_text(encoding="utf-8")
    )
    assert metrics == {"llm_calls": 1}
