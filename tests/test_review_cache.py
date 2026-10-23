from pathlib import Path

from openreview.review.cache import ReviewCache, fingerprint
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


def test_disabled_review_cache_is_a_noop(tmp_path: Path):
    cache = ReviewCache(tmp_path, enabled=False)
    key = fingerprint("pr-url")
    cache.set(key, _result())

    assert cache.get(key) is None
    assert not (tmp_path / key[:2]).exists()
