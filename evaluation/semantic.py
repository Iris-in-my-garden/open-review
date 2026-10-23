"""可选：复用 AACR-Bench 官方的 LLM-as-a-judge 语义评测。

我们不在仓库里复制 AACR 的 judge 代码，而是把它的 ``evaluation/`` 目录加入
``sys.path`` 后直接调用 ``evaluate.evaluate``。这样：

- 指标定义始终与官方一致（文件级语义 P/R、balanced score）；
- judge prompt / 模型配置跟随 AACR，不会出现两份漂移的实现；
- 没有 AACR checkout 时仍然可以跑 deterministic metrics。

调用约定：本模块写出的结果 JSON 使用 AACR OCR 兼容的
``review.comments[].content`` / ``review.summary.input_tokens`` 字段；
``reviewer`` 传 ``openreview`` 时，AACR 的默认 builder 会走 OCR 分支。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.aacr import ManifestError


def _reference_comments(comments: List[Dict[str, Any]]) -> List[Any]:
    from schema import ReferenceComment

    result = []
    for raw in comments:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        text = raw.get("note") or raw.get("text")
        if not path or not text:
            continue
        start = raw.get("from_line")
        end = raw.get("to_line")
        if start is not None and end is not None and start > end:
            start, end = end, start
        side = raw.get("side")
        result.append(
            ReferenceComment(
                path=str(path),
                text=str(text),
                start_line=start,
                end_line=end,
                side=str(side) if side is not None else None,
            )
        )
    return result


def run_semantic_eval(
    instances: List[Dict[str, Any]],
    *,
    results_dir: Path,
    metrics_dir: Path,
    aacr_eval_dir: Path,
    line_k: int = 1,
    limit: Optional[int] = None,
    use_deprecated_score: bool = False,
) -> Dict[str, Any]:
    """调用 AACR 官方评测器；需要设置 JUDGE_* 环境变量才会走真实 LLM。"""
    eval_dir = Path(aacr_eval_dir).expanduser().resolve()
    if not (eval_dir / "evaluate.py").exists():
        raise ManifestError(
            f"AACR evaluation 目录无效: {eval_dir}（期望包含 evaluate.py）"
        )
    if str(eval_dir) not in sys.path:
        sys.path.insert(0, str(eval_dir))

    import evaluate as aacr_evaluate
    from schema import ReviewInstance

    review_instances = [
        ReviewInstance(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            head_commit=instance["head_commit"],
            clone_url=instance.get("clone_url")
            or f"https://github.com/{instance['repo']}.git",
            reference_comments=_reference_comments(instance.get("comments") or []),
        )
        for instance in instances
    ]

    print(f"[semantic] judge mode: {aacr_evaluate.judge_mode_description()}")
    data = asyncio.run(
        aacr_evaluate.evaluate(
            instances=review_instances,
            results_dir=Path(results_dir),
            reviewer="openreview",
            output_dir=Path(metrics_dir),
            line_k=line_k,
            limit=limit,
            use_deprecated_score=use_deprecated_score,
        )
    )
    aacr_evaluate.print_summary("openreview", data)
    return data
