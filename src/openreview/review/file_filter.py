"""Deterministic file routing applied before review batching.

The goal is not to decide whether a PR is important, but to keep the LLM from
spending prompt budget on files where an LLM adds little value: lockfiles,
images/binaries, generated sources, pure data dumps, and Markdown docs.

The rules are intentionally conservative and explainable.  Every skipped file
is recorded with a stable category/reason so evaluation can measure the effect
and so a reviewer can see what was omitted.  Test files and real config files
are kept: tests encode behaviour, and manifests/CI/workflow changes can change
what gets shipped.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff

FILE_FILTER_POLICY_VERSION = "file-filter-v3"

REVIEW = "review"
SKIP = "skip"

# Lockfiles are generated dependency graphs; a human/CI already validates them.
LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-lock.yml",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "cargo.lock",
        "go.sum",
        "composer.lock",
        "gemfile.lock",
        "podfile.lock",
        "packages.lock.json",
        "mix.lock",
        "pubspec.lock",
        "flake.lock",
        "deno.lock",
        "gradle.lockfile",
        "package.resolved",
        "npm-debug.log",
        "yarn-error.log",
    }
)

# Images, media, fonts, archives and native/compiled artifacts.
BINARY_ASSET_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".svg",
        ".webp",
        ".avif",
        ".heic",
        ".psd",
        ".ai",
        ".eps",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".m4a",
        ".mov",
        ".avi",
        ".wav",
        ".ogg",
        ".webm",
        ".flac",
        ".aac",
        ".zip",
        ".tar",
        ".tgz",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".war",
        ".class",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".lib",
        ".obj",
        ".pdb",
        ".wasm",
        ".sqlite",
        ".db",
        ".parquet",
        ".arrow",
        ".npy",
        ".npz",
        ".pkl",
        ".pickle",
        ".onnx",
        ".pt",
        ".pth",
        ".h5",
        ".pb",
    }
)

# Data dumps and snapshots.  These are not "config" even if they are JSON/YAML.
DATA_EXTENSIONS = frozenset(
    {
        ".json",
        ".jsonl",
        ".ndjson",
        ".geojson",
        ".xml",
        ".xsd",
        ".wsdl",
        ".csv",
        ".tsv",
        ".snap",
    }
)
YAML_EXTENSIONS = frozenset({".yaml", ".yml"})
DATA_PATH_PARTS = frozenset(
    {
        "data",
        "dataset",
        "datasets",
        "fixture",
        "fixtures",
        "testdata",
        "test_data",
        "sample",
        "samples",
        "snapshot",
        "snapshots",
        "__snapshots__",
        "seed",
        "seeds",
        "starter_projects",
    }
)

# Generated baseline/snapshot artifacts and vendored patches are not source
# code. They are expected outputs (or third-party diffs) and reviewing them
# line-by-line mostly produces noise; a deterministic note is enough.
SNAPSHOT_PATH_PARTS = frozenset(
    {"baselines", "snapshots", "__snapshots__"}
)
TESTDATA_PATH_PARTS = frozenset({"testdata", "test_data"})
SNAPSHOT_EXTENSIONS = frozenset(
    {".types", ".symbols", ".diff", ".snap", ".baseline"}
)
VENDORED_PATCH_EXTENSIONS = frozenset({".patch"})
BUNDLED_PATH_PARTS = frozenset({"bundled"})

DOCS_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"})

# Directories whose contents are generated, vendored or build output.
GENERATED_DIRS = frozenset(
    {
        "dist",
        "build",
        "out",
        "target",
        "vendor",
        "node_modules",
        "coverage",
        "__pycache__",
        ".next",
        ".nuxt",
        ".venv",
        "venv",
        "site-packages",
        "third_party",
        "generated",
        "gen",
    }
)

GENERATED_NAME_PATTERNS = (
    "*.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "*.gen.go",
    "*.gen.ts",
    "*.gen.tsx",
    "*.gen.js",
    "*.g.cs",
    "*.designer.cs",
    "*.g.dart",
    "*.freezed.dart",
    "*.mocks.dart",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.bundle.js",
    "*_bundle.js",
    "*.chunk.js",
)

GENERATED_MARKERS = (
    "code generated by",
    "do not edit",
    "@generated",
    "auto-generated",
    "autogenerated",
    "this file was generated",
    "generated by the",
    "generated from",
)

# Config/manifests that must stay reviewable even though they use a data-ish
# extension.  Keep this list explicit rather than trying to infer intent.
CONFIG_BASENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "pipfile",
        "requirements.txt",
        "constraints.txt",
        "tsconfig.json",
        "jsconfig.json",
        "dockerfile",
        "makefile",
        "cmakelists.txt",
        "go.mod",
        "go.work",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "directory.build.props",
        "directory.packages.props",
        "nuget.config",
        "global.json",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "manifest.json",
        "config.json",
        "settings.json",
        "appsettings.json",
        "firebase.json",
        "angular.json",
        "vercel.json",
        "netlify.json",
        "babel.json",
        "deno.json",
        "composer.json",
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "swagger.json",
        "swagger.yaml",
        "swagger.yml",
        "renovate.json",
        "biome.json",
        "turbo.json",
        "nx.json",
        "lerna.json",
        "rush.json",
        "mkdocs.yml",
        "mkdocs.yaml",
        "values.yaml",
        "values.yml",
        "chart.yaml",
        "kustomization.yaml",
        "kustomization.yml",
    }
)

CONFIG_PATTERNS = (
    "requirements*.txt",
    "constraints*.txt",
    "tsconfig*.json",
    "jsconfig*.json",
    "*.config.json",
    "config*.json",
    "settings*.json",
    "appsettings*.json",
    ".eslintrc*",
    "eslint.config.*",
    ".prettierrc*",
    "prettier.config.*",
    "*.config.js",
    "*.config.ts",
    "*.config.mjs",
    "*.config.cjs",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "values*.yml",
    "values*.yaml",
    "kustomization*.yml",
    "kustomization*.yaml",
    "*.csproj",
    "*.fsproj",
    "*.vbproj",
    "*.sln",
    "*.props",
    "*.targets",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "jenkinsfile",
)

DATA_BASENAMES = frozenset(
    {
        ".test_durations",
        ".coverage",
        ".nyc_output",
        ".ds_store",
    }
)


@dataclass(frozen=True)
class FileFilterDecision:
    filename: str
    action: str
    category: str
    reason: str
    additions: int = 0
    deletions: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "filename": self.filename,
            "action": self.action,
            "category": self.category,
            "reason": self.reason,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass(frozen=True)
class FileFilterResult:
    diff: ParsedDiff
    decisions: List[FileFilterDecision]

    @property
    def skipped(self) -> List[FileFilterDecision]:
        return [
            decision for decision in self.decisions if decision.action == SKIP
        ]

    def skipped_summary(self, limit: int = 4) -> str:
        counts: Dict[str, int] = {}
        for decision in self.skipped:
            counts[decision.category] = counts.get(decision.category, 0) + 1
        if not counts:
            return ""
        parts = [
            f"{category} {count}"
            for category, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        ]
        return "、".join(parts[:limit])


def _normalized_parts(filename: str) -> Tuple[str, ...]:
    return tuple(
        part.lower()
        for part in Path(filename.replace("\\", "/")).parts
        if part not in ("", ".")
    )


def _basename(filename: str) -> str:
    return Path(filename.replace("\\", "/")).name.lower()


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _is_lockfile(filename: str) -> bool:
    name = _basename(filename)
    return (
        name in LOCKFILE_NAMES
        or name.endswith(".lock")
        or name.endswith(".lockb")
        or name.endswith(".lockfile")
    )


def _is_binary_or_asset(filename: str) -> bool:
    name = _basename(filename)
    suffix = Path(name).suffix
    return (
        suffix in BINARY_ASSET_EXTENSIONS
        or name in DATA_BASENAMES
        or name.endswith((".min.js", ".min.css", ".map"))
    )


def _is_generated(filename: str, patch: str | None) -> bool:
    name = _basename(filename)
    if _matches_any(name, GENERATED_NAME_PATTERNS):
        return True

    parts = _normalized_parts(filename)
    if any(part in GENERATED_DIRS for part in parts[:-1]):
        return True

    # Markers are usually at the top of a generated file.  Looking at added
    # lines catches new files; the filename rules cover most edits to existing
    # generated files.
    added = [
        line[1:]
        for line in (patch or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    header = "\n".join(added[:30]).lower()
    return any(marker in header for marker in GENERATED_MARKERS)


def _is_reviewable_config(filename: str) -> bool:
    name = _basename(filename)
    normalized = filename.replace("\\", "/").lower()
    parts = _normalized_parts(filename)
    return (
        name in CONFIG_BASENAMES
        or _matches_any(name, CONFIG_PATTERNS)
        or normalized.startswith(".github/workflows/")
        or (
            Path(name).suffix in {".json", ".yaml", ".yml"}
            and any(
                part
                in {"config", "configs", "settings", ".vscode", ".devcontainer"}
                for part in parts[:-1]
            )
        )
    )


def _is_docs(filename: str) -> bool:
    parts = _normalized_parts(filename)
    suffix = Path(filename).suffix.lower()
    return suffix in DOCS_EXTENSIONS or "docs" in parts[:-1]


def _is_data(filename: str) -> bool:
    name = _basename(filename)
    suffix = Path(name).suffix
    parts = _normalized_parts(filename)
    if suffix in DATA_EXTENSIONS:
        return True
    if suffix in YAML_EXTENSIONS:
        # YAML is often real configuration (k8s/Helm/CI), so only skip it when
        # the path clearly marks it as fixture/sample data.
        return any(part in DATA_PATH_PARTS for part in parts[:-1])
    return name in DATA_BASENAMES


def _is_snapshot(filename: str) -> bool:
    parts = _normalized_parts(filename)
    if any(part in SNAPSHOT_PATH_PARTS for part in parts[:-1]):
        return True
    return Path(filename).suffix.lower() in SNAPSHOT_EXTENSIONS


def _is_testdata(filename: str) -> bool:
    parts = _normalized_parts(filename)
    return any(part in TESTDATA_PATH_PARTS for part in parts[:-1])


def _is_vendored_patch(filename: str) -> bool:
    return Path(filename).suffix.lower() in VENDORED_PATCH_EXTENSIONS


def _is_bundled_generated(filename: str) -> bool:
    parts = _normalized_parts(filename)
    return (
        any(part in BUNDLED_PATH_PARTS for part in parts[:-1])
        and _basename(filename).endswith(".d.ts")
    )


def classify_file(file_diff: ParsedChangedFile) -> FileFilterDecision:
    """Return a deterministic review/skip decision for one changed file."""
    filename = file_diff.filename
    additions = int(file_diff.additions or 0)
    deletions = int(file_diff.deletions or 0)

    def decision(action: str, category: str, reason: str) -> FileFilterDecision:
        return FileFilterDecision(
            filename=filename,
            action=action,
            category=category,
            reason=reason,
            additions=additions,
            deletions=deletions,
        )

    if _is_lockfile(filename):
        return decision(SKIP, "lockfile", "依赖锁文件，由包管理器/CI 校验")

    if _is_binary_or_asset(filename):
        return decision(SKIP, "asset", "图片/字体/媒体/压缩包/二进制产物")

    if _is_generated(filename, file_diff.patch):
        return decision(
            SKIP,
            "generated",
            "自动生成代码或构建产物，应修改生成源而不是直接审查",
        )

    if _is_snapshot(filename):
        return decision(
            SKIP,
            "snapshot",
            "生成的 baseline/快照预期输出，应由生成器或 CI 校验",
        )

    if _is_testdata(filename):
        return decision(
            SKIP,
            "testdata",
            "testdata/fixture 数据文件，不包含需要逐行审查的实现逻辑",
        )

    if _is_vendored_patch(filename):
        return decision(
            SKIP,
            "vendored_patch",
            "第三方/vendored patch 文件，不是本仓库的实现源码",
        )

    if _is_bundled_generated(filename):
        return decision(
            SKIP,
            "bundled",
            "bundled/generated 类型声明，应修改生成源而不是直接审查",
        )

    if _is_reviewable_config(filename):
        if not file_diff.patch:
            return decision(
                SKIP,
                "unavailable_patch",
                "配置文件没有可用文本 patch，无法审查；未将其误判为二进制文件",
            )
        return decision(REVIEW, "config", "工程配置/依赖清单/CI 配置，保留审查")

    if _is_docs(filename):
        return decision(SKIP, "docs", "纯 Markdown/文档变更，不注入重型推理模型")

    if _is_data(filename):
        return decision(
            SKIP,
            "data",
            "纯 JSON/YAML/CSV/XML 数据或快照，不包含可推理的实现逻辑",
        )

    if not file_diff.patch:
        return decision(
            SKIP,
            "unavailable_patch",
            "没有可用文本 patch，无法审查；未将其误判为二进制文件",
        )

    if file_diff.is_test_file:
        return decision(REVIEW, "test", "测试代码，保留审查（测试逻辑也可能有 bug）")

    if file_diff.is_config_file:
        return decision(REVIEW, "config", "配置文件，保留审查")

    return decision(REVIEW, "code", "源代码，保留审查")


def filter_parsed_diff(parsed_diff: ParsedDiff) -> FileFilterResult:
    """Split a parsed diff into reviewable files and skipped files."""
    kept: List[ParsedChangedFile] = []
    decisions: List[FileFilterDecision] = []
    for file_diff in parsed_diff.files:
        decision = classify_file(file_diff)
        decisions.append(decision)
        if decision.action == REVIEW:
            kept.append(file_diff)

    filtered = ParsedDiff(
        files=kept,
        total_additions=sum(file.additions for file in kept),
        total_deletions=sum(file.deletions for file in kept),
    )
    return FileFilterResult(diff=filtered, decisions=decisions)


def summarize_skipped(decisions: Iterable[Dict[str, object]]) -> str:
    """Summarize serialized skipped-file decisions for notes/metrics."""
    counts: Dict[str, int] = {}
    for item in decisions:
        category = str(item.get("category") or "other")
        counts[category] = counts.get(category, 0) + 1
    if not counts:
        return ""
    return "、".join(
        f"{category} {count}"
        for category, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    )
