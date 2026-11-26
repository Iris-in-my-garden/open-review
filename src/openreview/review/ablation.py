"""固定消融阶梯。

只保留四个可解释的配置，避免“开关组合爆炸”：

1. ``diff``：纯 Diff，不注入任何检索上下文，也不做 verifier。
2. ``ast``：Diff + 单文件 AST 折叠上下文。
3. ``graph``：Diff + AST + 代码图谱（callee qualifier / import dependency）。
4. ``verifier``：在 graph 基础上增加后置防幻觉校验。

impact / static 在消融模式下一律关闭，避免与 graph 检索的跨文件证据重叠。
triage 是确定性的规则前置过滤，不是本实验的消融维度，因此默认保持开启。
"""

from __future__ import annotations

from dataclasses import dataclass

ABLATION_LEVELS = ("diff", "ast", "graph", "verifier")
RETRIEVAL_MODES = ("none", "ast", "graph")


@dataclass(frozen=True)
class AblationSpec:
    name: str
    retrieval_mode: str
    verifier_enabled: bool
    triage_enabled: bool = True
    impact_enabled: bool = False
    static_analysis_enabled: bool = False
    label: str = ""
    description: str = ""


_SPECS = {
    "diff": AblationSpec(
        name="diff",
        retrieval_mode="none",
        verifier_enabled=False,
        label="纯 Diff",
        description="只把 patch 交给模型，不注入任何检索上下文。",
    ),
    "ast": AblationSpec(
        name="ast",
        retrieval_mode="ast",
        verifier_enabled=False,
        label="Diff + AST Folding",
        description="在 Diff 上增加单文件 tree-sitter AST 折叠上下文。",
    ),
    "graph": AblationSpec(
        name="graph",
        retrieval_mode="graph",
        verifier_enabled=False,
        label="Diff + AST + Code Graph",
        description=(
            "在 AST 基础上增加 callee qualifier 与 import dependency 图谱上下文。"
        ),
    ),
    "verifier": AblationSpec(
        name="verifier",
        retrieval_mode="graph",
        verifier_enabled=True,
        label="Diff + AST + Code Graph + Verifier",
        description="在图谱上下文基础上增加后置 verifier 防幻觉校验。",
    ),
}


def resolve_ablation(name: str) -> AblationSpec:
    normalized = str(name).strip().lower().replace("-", "_")
    try:
        return _SPECS[normalized]
    except KeyError as exc:
        choices = ", ".join(ABLATION_LEVELS)
        raise ValueError(
            f"ablation must be one of {choices}, got {name!r}"
        ) from exc


__all__ = [
    "ABLATION_LEVELS",
    "RETRIEVAL_MODES",
    "AblationSpec",
    "resolve_ablation",
]
