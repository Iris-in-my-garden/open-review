"""AACR-Bench 数据集加载、Python/Go 子集切分与固定 manifest。

为什么单独维护 manifest：
- 评测最怕“每次跑的数据不一样”。把 instance_id、base/head SHA、split 固化到
  仓库里，后续指标才能横向比较。
- 原始数据体积大（1MB+），不适合每次评测重新下载；manifest 只存索引和元数据。
- 切分使用 SHA256 排序而不是 ``random.shuffle``，不依赖 Python 版本的随机数
  实现，任何人重跑都能得到同一批样本。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

AACR_REPO_URL = "https://github.com/alibaba/aacr-bench"
AACR_DATA_URL = (
    "https://raw.githubusercontent.com/alibaba/aacr-bench/main/dataset/positive_samples.json"
)
AACR_DATA_SHA256 = (
    "02c8561f1c3994a26f427d479b2a3f6e6c8e6858161fbc903b45248fa2cb2097"
)
SUPPORTED_LANGUAGES = {"python": "Python", "go": "Go"}
MANIFEST_VERSION = "aacr-pygo-v1"
DEFAULT_SEED = 42

_GITHUB_PR_PATTERN = re.compile(
    r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE
)


class ManifestError(ValueError):
    """manifest 或原始数据不一致时抛出。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_source(
    source: Optional[Path],
    *,
    download: bool = False,
    cache_dir: Optional[Path] = None,
    verify: bool = True,
) -> Path:
    """返回 AACR 原始数据路径，默认校验官方 SHA256。

    ``source`` 显式给定但校验失败时直接报错，避免悄悄评测了另一份数据。
    本地自定义 fixture / 镜像数据可以显式传 ``verify=False``；此时只跳过校验，
    不会把数据当成官方 AACR 数据。
    """
    if source is not None:
        source = Path(source).expanduser().resolve()
        if not source.exists():
            raise ManifestError(f"AACR 原始数据不存在: {source}")
        if not verify:
            print(f"[aacr] WARNING: 跳过 SHA256 校验: {source}")
            return source
        actual = sha256_file(source)
        if actual != AACR_DATA_SHA256:
            raise ManifestError(
                "AACR 原始数据 SHA256 不匹配：\n"
                f"  期望 {AACR_DATA_SHA256}\n  实际 {actual}\n"
                f"  文件 {source}\n"
                "如果是官方数据更新，请同步修改 evaluation/aacr.py 中的常量；"
                "本地 fixture 可加 --no-verify-source。"
            )
        return source

    if not download:
        raise ManifestError(
            "未指定 AACR 原始数据。请用 --source 指向 positive_samples.json，"
            "或加 --download 让程序下载到 evaluation/.cache/aacr/。"
        )

    base = Path(cache_dir) if cache_dir is not None else Path(__file__).parent / ".cache" / "aacr"
    target = base / "positive_samples.json"
    if target.exists() and sha256_file(target) == AACR_DATA_SHA256:
        return target

    base.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.part")
    print(f"[aacr] 下载 {AACR_DATA_URL}")
    try:
        with urllib.request.urlopen(AACR_DATA_URL, timeout=120) as response:
            with tmp.open("wb") as handle:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    handle.write(chunk)
        actual = sha256_file(tmp)
        if actual != AACR_DATA_SHA256:
            raise ManifestError(
                f"下载数据 SHA256 不匹配：期望 {AACR_DATA_SHA256}，实际 {actual}"
            )
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def load_records(source: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"AACR 数据 JSON 解析失败: {error}") from error
    if not isinstance(data, list):
        raise ManifestError(f"AACR 数据必须是 JSON 数组，实际为 {type(data).__name__}")
    return data


def parse_github_pr(url: Any) -> Optional[tuple[str, int]]:
    if not isinstance(url, str):
        return None
    match = _GITHUB_PR_PATTERN.search(url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def instance_id_for(record: Dict[str, Any]) -> Optional[str]:
    parsed = parse_github_pr(record.get("githubPrUrl"))
    head = record.get("target_commit")
    if parsed is None or not head:
        return None
    repo, _ = parsed
    return f"{repo.replace('/', '__')}@{str(head)[:7]}"


def supported_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只保留 Python / Go，并补上派生字段；缺关键字段的记录直接跳过。"""
    result: List[Dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        language = SUPPORTED_LANGUAGES.get(
            str(raw.get("project_main_language", "")).strip().lower()
        )
        parsed = parse_github_pr(raw.get("githubPrUrl"))
        base = raw.get("source_commit")
        head = raw.get("target_commit")
        if not language or parsed is None or not base or not head:
            continue
        repo, pr_number = parsed
        instance_id = instance_id_for(raw)
        if not instance_id:
            continue
        item = dict(raw)
        item["_repo"] = repo
        item["_language"] = language
        item["_instance_id"] = instance_id
        item["_pr_number"] = pr_number
        result.append(item)
    return result


def _hash_key(seed: int, *parts: object) -> str:
    payload = ":".join(str(part) for part in (seed, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_manifest(
    records: Sequence[Dict[str, Any]],
    *,
    seed: int = DEFAULT_SEED,
    version: str = MANIFEST_VERSION,
    source_sha256: Optional[str] = None,
    source_name: str = "AACR-Bench",
) -> Dict[str, Any]:
    """构造固定 split：每个 repo 1 条 dev，每个语言 4 条 holdout，其余 test。

    真实 AACR Python/Go 子集上应得到 10 dev / 8 holdout / 25 test。
    选择顺序完全由 ``seed + instance_id`` 的 SHA256 决定，可复现。
    """
    supported = supported_records(records)
    if not supported:
        raise ManifestError("过滤后没有 Python/Go 记录")

    by_repo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in supported:
        by_repo[record["_repo"]].append(record)

    for repo in by_repo:
        by_repo[repo].sort(key=lambda item: _hash_key(seed, item["_instance_id"]))

    repos_by_language: Dict[str, List[str]] = defaultdict(list)
    for repo, repo_records in by_repo.items():
        repos_by_language[repo_records[0]["_language"]].append(repo)
    for language in repos_by_language:
        repos_by_language[language].sort(
            key=lambda repo: _hash_key(seed, "repo", repo)
        )

    split_by_id: Dict[str, str] = {}
    # dev：每个 repo 取 1 条，保证所有仓库/语言都有覆盖。
    for repo_records in by_repo.values():
        split_by_id[repo_records[0]["_instance_id"]] = "dev"

    # holdout：每个语言按 repo 哈希顺序取最多 4 个仍有剩余样本的仓库，
    # 各取 1 条。Go 的 LocalAI 只有 1 个 PR，会在 dev 用掉后自然被跳过。
    for language in sorted(repos_by_language):
        eligible = [
            repo
            for repo in repos_by_language[language]
            if any(
                record["_instance_id"] not in split_by_id
                for record in by_repo[repo]
            )
        ]
        for repo in eligible[:4]:
            for record in by_repo[repo]:
                if record["_instance_id"] not in split_by_id:
                    split_by_id[record["_instance_id"]] = "holdout"
                    break

    for record in supported:
        split_by_id.setdefault(record["_instance_id"], "test")

    instances: List[Dict[str, Any]] = []
    for record in supported:
        split = split_by_id[record["_instance_id"]]
        instances.append(
            {
                "instance_id": record["_instance_id"],
                "split": split,
                "repo": record["_repo"],
                "language": record["_language"],
                "pr_number": record["_pr_number"],
                "base_commit": str(record["source_commit"]).strip(),
                "head_commit": str(record["target_commit"]).strip(),
                "github_pr_url": record["githubPrUrl"],
                "clone_url": record.get("clone_url")
                or f"https://github.com/{record['_repo']}.git",
                "reference_comments": len(record.get("comments") or []),
                "change_line_count": record.get("change_line_count"),
                "category": record.get("category"),
            }
        )

    instances.sort(
        key=lambda item: (
            _split_order(item["split"]),
            item["language"],
            item["repo"],
            item["instance_id"],
        )
    )
    _validate_manifest_instances(instances)
    counts = Counter(item["split"] for item in instances)
    by_language = Counter(item["language"] for item in instances)

    return {
        "version": version,
        "source": {
            "name": source_name,
            "url": AACR_REPO_URL,
            "data_url": AACR_DATA_URL,
            "sha256": source_sha256 or AACR_DATA_SHA256,
        },
        "languages": sorted(SUPPORTED_LANGUAGES.values()),
        "seed": seed,
        "selection": {
            "dev": "每个 repo 取 1 条（按 seed+instance_id 哈希排序）",
            "holdout": "每个语言最多 4 个仍有剩余样本的 repo，各取 1 条",
            "test": "其余全部",
        },
        "counts": {
            "total": len(instances),
            "dev": counts.get("dev", 0),
            "test": counts.get("test", 0),
            "holdout": counts.get("holdout", 0),
            "by_language": dict(sorted(by_language.items())),
        },
        "instances": instances,
    }


def _split_order(split: str) -> int:
    return {"dev": 0, "test": 1, "holdout": 2}.get(split, 3)


def _validate_manifest_instances(instances: Sequence[Dict[str, Any]]) -> None:
    seen: set[str] = set()
    for item in instances:
        instance_id = item["instance_id"]
        if instance_id in seen:
            raise ManifestError(f"manifest 中出现重复 instance_id: {instance_id}")
        seen.add(instance_id)
        if not item["base_commit"] or not item["head_commit"]:
            raise ManifestError(f"{instance_id}: base/head commit 不能为空")
        if item["base_commit"] == item["head_commit"]:
            raise ManifestError(f"{instance_id}: base 与 head 相同")


def write_manifest(manifest: Dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_manifest(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"manifest 不存在: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"manifest JSON 解析失败: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("instances"), list):
        raise ManifestError("manifest 结构异常：缺少 instances 数组")
    _validate_manifest_instances(manifest["instances"])
    return manifest


def instances_for_split(
    manifest: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    split: str = "all",
) -> List[Dict[str, Any]]:
    """把 manifest 实例与原始记录合并，返回带 reference_comments 的列表。"""
    if split not in {"all", "dev", "test", "holdout"}:
        raise ManifestError(f"未知 split: {split}")

    records_by_id = {
        item["_instance_id"]: item for item in supported_records(records)
    }
    result: List[Dict[str, Any]] = []
    for entry in manifest["instances"]:
        if split != "all" and entry["split"] != split:
            continue
        record = records_by_id.get(entry["instance_id"])
        if record is None:
            raise ManifestError(
                f"原始数据中找不到 {entry['instance_id']}，manifest 与数据源不一致"
            )
        if (
            str(record["source_commit"]).strip() != entry["base_commit"]
            or str(record["target_commit"]).strip() != entry["head_commit"]
        ):
            raise ManifestError(
                f"{entry['instance_id']}: manifest 中的 base/head 与原始数据不一致"
            )
        merged = dict(entry)
        merged["comments"] = record.get("comments") or []
        merged["raw_record"] = record
        result.append(merged)
    return result


def manifest_summary(manifest: Dict[str, Any]) -> str:
    counts = manifest.get("counts", {})
    return (
        f"{manifest.get('version')} | seed={manifest.get('seed')} | "
        f"total={counts.get('total')} "
        f"(dev={counts.get('dev')}, test={counts.get('test')}, "
        f"holdout={counts.get('holdout')})"
    )
