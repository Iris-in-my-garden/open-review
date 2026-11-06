import json
from pathlib import Path

from openreview.review.cache import ReviewCache, fingerprint
from openreview.review.config import REVIEW_CACHE_SCHEMA_VERSION, ReviewConfig
from openreview.review.report_writer import ReviewResult


def _result() -> ReviewResult:
    return ReviewResult(
        owner="org",
        repo="repo",
        pr_number=7,
        summary="summary",
        risk_level="MEDIUM",
        risk_reason="reason",
        potential_issues=[],
        final_decision="COMMENT",
        final_summary="done",
    )


def test_review_cache_roundtrip(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=True)
    key = fingerprint("pr-url", "head-sha", "model")

    assert cache.get(key) is None
    cache.set(key, _result())
    cached = cache.get(key)

    assert cached is not None
    assert cached.owner == "org"
    assert cached.pr_number == 7


def test_review_cache_corrupt_entry_is_ignored(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=True)
    key = fingerprint("pr-url")
    path = cache._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert cache.get(key) is None


def test_review_cache_stores_schema_version(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=True)
    key = fingerprint("pr-url")
    cache.set(key, _result())

    payload = json.loads(cache._path(key).read_text(encoding="utf-8"))
    assert payload["schema_version"] == REVIEW_CACHE_SCHEMA_VERSION
    assert payload["result"]["owner"] == "org"


def test_review_cache_rejects_legacy_payload_without_schema(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=True)
    key = fingerprint("pr-url")
    path = cache._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_result().model_dump(mode="json")), encoding="utf-8"
    )

    # 旧格式没有 schema_version，无法确认是哪个 cache key 规则生成的。
    assert cache.get(key) is None


def test_review_cache_key_covers_output_affecting_config():
    base = ReviewConfig()
    variants = [
        ReviewConfig(max_batch_tokens=1),
        ReviewConfig(max_patch_tokens=100),
        ReviewConfig(max_context_tokens=100),
        ReviewConfig(static_tools=("ruff", "mypy")),
        ReviewConfig(verify_severities=("high",)),
        ReviewConfig(max_workers=2),
        ReviewConfig(aspect_timeout=10.0),
        ReviewConfig(reviewer_mode="multi_aspect"),
        ReviewConfig(verifier_enabled=False),
    ]

    for variant in variants:
        assert variant.cache_key_parts() != base.cache_key_parts()


def test_disabled_review_cache_is_a_noop(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=False)
    key = fingerprint("pr-url")
    cache.set(key, _result())

    assert cache.get(key) is None
    assert not (tmp_path / key[:2]).exists()
