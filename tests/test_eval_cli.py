import argparse
from pathlib import Path

import pytest

from evaluation.aacr import ManifestError
from evaluation.cli import _load_manifest_instances

MANIFEST = Path(__file__).resolve().parents[1] / "evaluation" / "manifests" / "aacr_pygo_v3.json"


def _args(split: str, *, limit: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(manifest=MANIFEST, split=split, limit=limit)


def test_empty_split_fails_fast_instead_of_running_zero_prs():
    # v3 dropped the test split; asking for it must not look like a successful
    # run that silently wrote empty metrics.
    with pytest.raises(ManifestError, match="没有实例") as error:
        _load_manifest_instances(_args("test"))
    assert "dev=" in str(error.value)


def test_all_split_returns_every_instance():
    manifest, instances = _load_manifest_instances(_args("all"))

    assert len(instances) == manifest["counts"]["total"] == 40


def test_limit_zero_is_an_explicit_empty_run_not_a_split_error():
    _, instances = _load_manifest_instances(_args("dev", limit=0))

    assert instances == []
