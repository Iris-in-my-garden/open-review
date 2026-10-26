"""评测成本估算：所有付费 review 先做 preflight，避免误跑全量。

价格单位是 USD / 1M tokens。价格只是估算，真实账单以 provider 为准；
未知模型必须显式传 ``--input-price`` / ``--output-price``，不猜价格。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

MODEL_PRICES: Dict[str, Tuple[float, float]] = {
    # input, output (USD / 1M tokens)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


class CostError(ValueError):
    """缺少价格或预算参数。"""


def resolve_prices(
    model: Optional[str],
    *,
    input_price: Optional[float] = None,
    output_price: Optional[float] = None,
    mock_llm: bool = False,
) -> Tuple[float, float]:
    if mock_llm:
        return 0.0, 0.0
    if input_price is not None or output_price is not None:
        if input_price is None or output_price is None:
            raise CostError("--input-price 和 --output-price 必须同时提供")
        if input_price < 0 or output_price < 0:
            raise CostError("价格不能为负数")
        return float(input_price), float(output_price)
    if model and model in MODEL_PRICES:
        return MODEL_PRICES[model]
    raise CostError(
        "未知模型价格；请显式传 --input-price / --output-price "
        f"（USD / 1M tokens），或使用已知模型: {', '.join(sorted(MODEL_PRICES))}"
    )


def estimate_usd(
    prompt_tokens: int,
    completion_tokens: int,
    input_price: float,
    output_price: float,
) -> float:
    cost = (
        max(0, prompt_tokens) / 1_000_000 * input_price
        + max(0, completion_tokens) / 1_000_000 * output_price
    )
    return round(cost, 6)


__all__ = ["MODEL_PRICES", "CostError", "estimate_usd", "resolve_prices"]
