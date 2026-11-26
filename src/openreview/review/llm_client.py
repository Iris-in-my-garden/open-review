import contextvars
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Type, TypeVar
from urllib.parse import urlparse

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
)
from pydantic import BaseModel, ValidationError

from openreview.domain.errors import (
    BudgetExceeded,
    ProviderError,
    StructuredOutputError,
)
from openreview.review.output_parser import PotentialIssueSchema
from openreview.review.token_budget import estimate_tokens
from openreview.review.workflow import ReviewBudget

T = TypeVar("T", bound=BaseModel)

# 项目默认使用 DeepSeek：review 用 pro + thinking，verifier 用 flash。
# 所有值都可以被显式参数或环境变量覆盖，非 DeepSeek 模型不会收到
# ``thinking`` 这类厂商扩展字段。
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_VERIFIER_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_REASONING_EFFORT = "high"
REASONING_EFFORTS = frozenset(
    {"none", "low", "high", "max", "minimal", "medium", "xhigh"}
)
STRUCTURED_OUTPUT_MODES = frozenset({"auto", "json_schema", "json_object"})
JSON_OUTPUT_INSTRUCTION = (
    "\n\n【结构化输出】只输出一个 JSON 对象，不要 Markdown 代码块，"
    "不要额外解释。输出必须符合以下 JSON Schema：\n{schema}"
)


def is_deepseek_model(model: Optional[str]) -> bool:
    return "deepseek" in (model or "").strip().lower()


def resolve_model(model: Optional[str] = None) -> str:
    return model or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL


def resolve_verifier_model(
    review_model: str, verifier_model: Optional[str] = None
) -> str:
    """Verifier defaults to Flash for DeepSeek, otherwise matches review."""
    if verifier_model:
        return verifier_model
    from_env = os.getenv("OPENREVIEW_VERIFIER_MODEL")
    if from_env:
        return from_env
    return DEFAULT_VERIFIER_MODEL if is_deepseek_model(review_model) else review_model


def resolve_base_url(
    model: Optional[str], base_url: Optional[str] = None
) -> Optional[str]:
    if base_url:
        return base_url
    from_env = os.getenv("OPENAI_BASE_URL")
    if from_env:
        return from_env
    if is_deepseek_model(model):
        return DEFAULT_BASE_URL
    return None


def resolve_api_key(api_key: Optional[str] = None) -> Optional[str]:
    return api_key or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")


def _is_deepseek_endpoint(model: Optional[str], base_url: Optional[str]) -> bool:
    return is_deepseek_model(model) or "deepseek" in (base_url or "").lower()


def resolve_structured_output(
    model: Optional[str],
    base_url: Optional[str],
    structured_output: Optional[str] = None,
) -> str:
    """DeepSeek only supports ``json_object``; OpenAI supports JSON schema."""
    mode = (
        structured_output
        or os.getenv("OPENREVIEW_STRUCTURED_OUTPUT")
        or "auto"
    ).strip().lower()
    if mode not in STRUCTURED_OUTPUT_MODES:
        raise ValueError(
            "structured_output must be one of "
            f"{', '.join(sorted(STRUCTURED_OUTPUT_MODES))}, got {mode!r}"
        )
    if mode == "auto":
        return (
            "json_object"
            if _is_deepseek_endpoint(model, base_url)
            else "json_schema"
        )
    return mode


def _json_schema_hint(response_schema: Type[BaseModel]) -> str:
    schema = json.dumps(
        response_schema.model_json_schema(), ensure_ascii=False, sort_keys=True
    )
    return JSON_OUTPUT_INSTRUCTION.format(schema=schema)


def _parse_json_object(text: str, response_schema: Type[T]) -> T:
    """Parse a JSON-mode response, tolerating Markdown code fences."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if not stripped:
        raise StructuredOutputError(
            "model returned empty content in JSON mode "
            "(output cap may be too small)"
        )
    try:
        return response_schema.model_validate_json(stripped)
    except ValidationError as first_error:
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            try:
                return response_schema.model_validate_json(
                    stripped[start : end + 1]
                )
            except ValidationError:
                pass
        raise StructuredOutputError(
            "model returned invalid JSON for "
            f"{response_schema.__name__}: {first_error}"
        ) from first_error

_REQUEST_RUN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "openreview_request_run_id", default=None
)
_REQUEST_TIMEOUT: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "openreview_request_timeout", default=None
)
_REQUEST_OUTPUT_LIMIT: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "openreview_request_output_limit", default=None
)
_REQUEST_ATTEMPT: contextvars.ContextVar[int] = contextvars.ContextVar(
    "openreview_request_attempt", default=1
)


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, StructuredOutputError):
        # Empty/truncated JSON is usually a per-call accident, not a schema bug.
        return True
    if isinstance(exc, ProviderError):
        cause = exc.__cause__
        return isinstance(cause, Exception) and _is_retryable_provider_error(cause)
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or exc.status_code >= 500
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _estimate_request_tokens(
    system_prompt: str, user_prompt: str, response_schema: Type[BaseModel]
) -> int:
    schema = json.dumps(response_schema.model_json_schema(), sort_keys=True)
    return (
        estimate_tokens(system_prompt)
        + estimate_tokens(user_prompt)
        + estimate_tokens(schema)
    )


class AbstractLLMClient(Protocol):
    """LLM 客户端统一接口规范"""
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        ...


class BudgetedLLMClient:
    """在每次 LLM 调用前统一检查/扣减预算。

    之前预算只在 ``ReviewPipeline._node_analyze`` 里按“维度数”记账，
    重试、超时和 verifier 的额外调用都不会被扣减，``--budget-calls``
    并不是真正的硬上限。把预算放到客户端层后，aspect / verifier / retry
    共享同一本账。
    """

    has_budget = True

    def __init__(
        self,
        inner: AbstractLLMClient,
        budget: Optional[ReviewBudget] = None,
        run_id: Optional[str] = None,
    ):
        self.inner = inner
        self._budget = budget
        self.run_id = run_id

    def bind_budget(self, budget: Optional[ReviewBudget]) -> None:
        self._budget = budget

    @property
    def budget(self) -> Optional[ReviewBudget]:
        return self._budget

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", type(self.inner).__name__))

    @property
    def usage(self) -> Dict[str, int]:
        return getattr(self.inner, "usage", {})

    @property
    def temperature(self) -> Optional[float]:
        return getattr(self.inner, "temperature", None)

    @property
    def seed(self) -> Optional[int]:
        return getattr(self.inner, "seed", None)

    @property
    def max_retries(self) -> int:
        return max(0, int(getattr(self.inner, "max_retries", 0) or 0))

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        estimated = _estimate_request_tokens(
            system_prompt, user_prompt, response_schema
        )
        for attempt in range(self.max_retries + 1):
            budget = self._budget
            output_reservation = 0
            configured_output_limit = getattr(
                self.inner, "max_completion_tokens", None
            )
            if budget is not None:
                remaining_output = budget.remaining_output_tokens()
                if remaining_output is not None:
                    output_reservation = min(
                        remaining_output,
                        max(0, int(configured_output_limit or remaining_output)),
                    )
                # Check and reserve the full possible response atomically.
                budget.reserve(
                    calls=1,
                    tokens=estimated,
                    output_tokens=output_reservation,
                )

            remaining_seconds = budget.remaining_seconds() if budget else None
            configured_timeout = float(getattr(self.inner, "timeout", 60.0))
            timeout = (
                min(configured_timeout, remaining_seconds)
                if remaining_seconds is not None
                else configured_timeout
            )
            run_token = _REQUEST_RUN_ID.set(self.run_id)
            timeout_token = _REQUEST_TIMEOUT.set(timeout)
            output_token = _REQUEST_OUTPUT_LIMIT.set(
                output_reservation
                if budget is not None and budget.max_output_tokens > 0
                else None
            )
            attempt_token = _REQUEST_ATTEMPT.set(attempt + 1)
            try:
                result = self.inner.generate_structured(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                )
            except Exception as exc:
                if isinstance(exc, BudgetExceeded):
                    if budget is not None:
                        budget.refund(
                            calls=1,
                            tokens=estimated,
                            output_tokens=output_reservation,
                        )
                    raise
                actual = getattr(self.inner, "last_usage", None)
                if budget is not None:
                    if isinstance(actual, dict) and actual:
                        budget.settle(
                            reserved_input_tokens=estimated,
                            actual_input_tokens=int(
                                actual.get("prompt_tokens", 0) or 0
                            ),
                            actual_output_tokens=int(
                                actual.get("completion_tokens", 0) or 0
                            ),
                            reserved_output_tokens=output_reservation,
                        )
                    else:
                        # A timeout may happen after the provider accepted the
                        # request. Charge conservatively instead of assuming free.
                        budget.settle(
                            reserved_input_tokens=estimated,
                            actual_input_tokens=estimated,
                            reserved_output_tokens=output_reservation,
                            charge_reserved_output=True,
                        )
                if attempt >= self.max_retries or not _is_retryable_provider_error(exc):
                    raise
                delay = min(0.5 * (2**attempt), 8.0)
                if budget is not None:
                    remaining = budget.remaining_seconds()
                    if remaining is not None:
                        delay = min(delay, remaining)
                time.sleep(delay)
            else:
                actual = getattr(self.inner, "last_usage", None)
                if budget is not None:
                    if isinstance(actual, dict) and actual:
                        budget.settle(
                            reserved_input_tokens=estimated,
                            actual_input_tokens=int(
                                actual.get("prompt_tokens", 0) or 0
                            ),
                            actual_output_tokens=int(
                                actual.get("completion_tokens", 0) or 0
                            ),
                            reserved_output_tokens=output_reservation,
                        )
                    else:
                        budget.settle(
                            reserved_input_tokens=estimated,
                            actual_input_tokens=estimated,
                            reserved_output_tokens=output_reservation,
                            charge_reserved_output=True,
                        )
                return result
            finally:
                _REQUEST_ATTEMPT.reset(attempt_token)
                _REQUEST_OUTPUT_LIMIT.reset(output_token)
                _REQUEST_TIMEOUT.reset(timeout_token)
                _REQUEST_RUN_ID.reset(run_token)

        raise RuntimeError("LLM retry loop finished without a result or exception.")

    def __getattr__(self, name: str) -> Any:
        # 透传 cache wrapper 的 hits/misses 等统计字段。
        return getattr(self.inner, name)


class CompositeLLMClient:
    """把 review / verifier 两个分阶段客户端聚合成一个 facade。

    Reviewer 与 Verifier 可以使用不同模型（本项目默认 review=pro、verify=flash）。
    Executor、cache key 和 metrics 仍然只认识一个 client，因此：

    - ``generate_structured`` 走 review 客户端；
    - ``usage`` / ``call_log`` / cache ``hits`` / ``misses`` 汇总两个阶段；
    - ``bind_budget`` 把同一个 run budget 绑定到两个客户端。
    """

    def __init__(
        self,
        primary: AbstractLLMClient,
        verifier: Optional[AbstractLLMClient] = None,
    ):
        self.primary = primary
        self.verifier = (
            None if verifier is None or verifier is primary else verifier
        )
        self._clients = [primary]
        if self.verifier is not None:
            self._clients.append(self.verifier)

    @property
    def has_budget(self) -> bool:
        return any(
            bool(getattr(client, "has_budget", False))
            for client in self._clients
        )

    @property
    def model(self) -> str:
        return str(
            getattr(self.primary, "model", type(self.primary).__name__)
        )

    @property
    def temperature(self) -> Optional[float]:
        return getattr(self.primary, "temperature", None)

    @property
    def seed(self) -> Optional[int]:
        return getattr(self.primary, "seed", None)

    @property
    def thinking(self) -> Optional[bool]:
        return getattr(self.primary, "thinking", None)

    @property
    def reasoning_effort(self) -> Optional[str]:
        return getattr(self.primary, "reasoning_effort", None)

    @property
    def structured_output(self) -> Optional[str]:
        return getattr(self.primary, "structured_output", None)

    @property
    def timeout(self) -> float:
        return float(getattr(self.primary, "timeout", 60.0))

    @property
    def max_completion_tokens(self) -> Optional[int]:
        return getattr(self.primary, "max_completion_tokens", None)

    @property
    def max_retries(self) -> int:
        return max(0, int(getattr(self.primary, "max_retries", 0) or 0))

    @property
    def last_usage(self) -> Optional[Dict[str, int]]:
        return getattr(self.primary, "last_usage", None)

    @property
    def usage(self) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for client in self._clients:
            for key, value in (getattr(client, "usage", {}) or {}).items():
                totals[key] = totals.get(key, 0) + int(value or 0)
        return totals

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        return [
            dict(item)
            for client in self._clients
            for item in (getattr(client, "call_log", []) or [])
        ]

    @property
    def hits(self) -> int:
        return sum(int(getattr(client, "hits", 0) or 0) for client in self._clients)

    @property
    def misses(self) -> int:
        return sum(
            int(getattr(client, "misses", 0) or 0) for client in self._clients
        )

    def bind_budget(self, budget: Optional[ReviewBudget]) -> None:
        for client in self._clients:
            bind = getattr(client, "bind_budget", None)
            if callable(bind):
                bind(budget)

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        return self.primary.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
        )


class LLMClient:
    """真实的 OpenAI 兼容客户端。

    配置与 GitHub Token 完全解耦：
    - api_key:``OPENAI_API_KEY``，回退到 ``DEEPSEEK_API_KEY``
    - model:``OPENAI_MODEL``，默认 ``deepseek-v4-pro``
    - base_url:``OPENAI_BASE_URL``；DeepSeek 模型未配置时默认官方地址
    - thinking:仅对 DeepSeek 发送 ``thinking`` / ``reasoning_effort``
    - structured_output:DeepSeek 走 ``json_object``，其他走 JSON Schema
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        seed: Optional[int] = None,
        max_completion_tokens: Optional[int] = None,
        thinking: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        structured_output: Optional[str] = None,
    ):
        self.api_key = resolve_api_key(api_key)
        if not self.api_key:
            raise ValueError(
                "缺少 LLM API Key。请设置环境变量 OPENAI_API_KEY"
                "（或 DEEPSEEK_API_KEY），"
                "或使用 `openreview review <PR_URL> --mock_llm` 进行离线演示。"
            )

        self.model: str = resolve_model(model)
        self.base_url = resolve_base_url(self.model, base_url)
        self.endpoint_host = (
            urlparse(self.base_url).netloc
            if self.base_url
            else "api.openai.com"
        )
        self.provider_is_deepseek = _is_deepseek_endpoint(
            self.model, self.base_url
        )
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        # 固定采样参数，保证同一 prompt 的结果可复现；评测/回放需要这一点。
        self.temperature = temperature
        self.seed = seed
        # Thinking 模式需要为 reasoning 预留输出预算；具体值由 config 控制。
        self.max_completion_tokens = (
            int(max_completion_tokens)
            if max_completion_tokens and max_completion_tokens > 0
            else None
        )
        self.thinking = thinking
        if reasoning_effort is not None:
            normalized_effort = reasoning_effort.strip().lower()
            if normalized_effort not in REASONING_EFFORTS:
                raise ValueError(
                    "reasoning_effort must be one of "
                    f"{', '.join(sorted(REASONING_EFFORTS))}, "
                    f"got {reasoning_effort!r}"
                )
            reasoning_effort = normalized_effort
        self.reasoning_effort = reasoning_effort
        self.structured_output = resolve_structured_output(
            self.model, self.base_url, structured_output
        )

        client_kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": timeout,
            # BudgetedLLMClient retries one provider attempt at a time so each
            # attempt is charged against the run's call and time limits.
            "max_retries": 0,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self._client = OpenAI(**client_kwargs)

        # 真实用量：报告里的成本估算不能只靠字符数猜测。
        self._usage_lock = threading.Lock()
        self.usage: Dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        # Per-run observability: one record per real provider call. Metrics
        # collector takes a slice, so reusing a pipeline does not mix runs.
        self.call_log: List[Dict[str, Any]] = []
        self._call_log_lock = threading.Lock()
        self._thread_local = threading.local()

    @property
    def last_usage(self) -> Optional[Dict[str, int]]:
        """Usage of the most recent call on the current thread."""
        return getattr(self._thread_local, "last_usage", None)

    def _record_usage(self, usage: Any) -> Dict[str, int]:
        if usage is None:
            return {}
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or prompt + completion
        with self._usage_lock:
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += prompt
            self.usage["completion_tokens"] += completion
            self.usage["total_tokens"] += total
        actual = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
        self._thread_local.last_usage = actual
        return actual

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self._thread_local.last_usage = {}
        timeout = _REQUEST_TIMEOUT.get()
        if timeout is None:
            timeout = self.timeout
        if timeout <= 0:
            raise BudgetExceeded("Review time budget exhausted before LLM request.")

        system_content = system_prompt
        if self.structured_output == "json_object":
            # DeepSeek 的 JSON Output 要求 prompt 里出现 "json" 并给出格式示例。
            system_content = system_prompt + _json_schema_hint(response_schema)

        request: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_prompt},
            ],
            # 把 HTTP 超时压到 aspect_timeout 以下，避免线程超时后请求还挂很久。
            "timeout": timeout,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        # DeepSeek 不支持 seed；其他 OpenAI 兼容端点保留可复现采样。
        if self.seed is not None and not self.provider_is_deepseek:
            request["seed"] = self.seed
        output_limit = _REQUEST_OUTPUT_LIMIT.get()
        if output_limit is None:
            output_limit = self.max_completion_tokens
        if output_limit is not None:
            # DeepSeek 文档只暴露 max_tokens；JSON Schema 路径沿用 SDK 参数。
            if self.structured_output == "json_object":
                request["max_tokens"] = output_limit
            else:
                request["max_completion_tokens"] = output_limit

        extra_body: Dict[str, Any] = {}
        if self.provider_is_deepseek:
            if self.thinking is not None:
                extra_body["thinking"] = {
                    "type": "enabled" if self.thinking else "disabled"
                }
            if self.thinking and self.reasoning_effort:
                extra_body["reasoning_effort"] = self.reasoning_effort
        if extra_body:
            request["extra_body"] = extra_body

        if self.structured_output == "json_schema":
            request["response_format"] = response_schema
        else:
            request["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        actual: Dict[str, int] = {}
        status = "error"
        error_type = ""
        try:
            if self.structured_output == "json_schema":
                response = self._client.beta.chat.completions.parse(**request)
                actual = self._record_usage(getattr(response, "usage", None))
                message = response.choices[0].message
                if message.refusal:
                    raise ValueError(
                        f"Model refused to respond: {message.refusal}"
                    )
                if message.parsed is None:
                    raise StructuredOutputError(
                        "model returned no parsed structured output"
                    )
                parsed = message.parsed
            else:
                response = self._client.chat.completions.create(**request)
                actual = self._record_usage(getattr(response, "usage", None))
                message = response.choices[0].message
                if message.refusal:
                    raise ValueError(
                        f"Model refused to respond: {message.refusal}"
                    )
                parsed = _parse_json_object(
                    getattr(message, "content", "") or "", response_schema
                )
            status = "ok"
            return parsed
        except Exception as exc:
            error_type = type(exc).__name__
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(
                "LLM provider request failed "
                f"(model={self.model}, endpoint={self.endpoint_host}): {exc}"
            ) from exc
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            with self._call_log_lock:
                self.call_log.append(
                    {
                        "run_id": _REQUEST_RUN_ID.get(),
                        "attempt": _REQUEST_ATTEMPT.get(),
                        "status": status,
                        "error_type": error_type,
                        "model": self.model,
                        "thinking": self.thinking,
                        "reasoning_effort": self.reasoning_effort,
                        "structured_output": self.structured_output,
                        "latency_ms": elapsed_ms,
                        "estimated_prompt_tokens": _estimate_request_tokens(
                            system_prompt, user_prompt, response_schema
                        ),
                        "prompt_tokens": int(actual.get("prompt_tokens", 0)),
                        "completion_tokens": int(
                            actual.get("completion_tokens", 0)
                        ),
                        "total_tokens": int(actual.get("total_tokens", 0)),
                    }
                )


class MockLLMClient:
    """专门用于离线开发和测试的 Mock 客户端（支持根据 Schema 动态生成对应的 Mock 数据）"""

    def __init__(self, custom_schema: Optional[BaseModel] = None):
        self.custom_schema = custom_schema
        self.model = "mock"
        self.temperature = 0.0
        self.seed: Optional[int] = None
        self.thinking: Optional[bool] = None
        self.reasoning_effort: Optional[str] = None
        self.structured_output = "mock"
        self.timeout = 0.0
        self.max_retries = 0
        self.max_completion_tokens: Optional[int] = None
        # 与真实客户端保持同一接口，方便报告统一读取。
        self.usage: Dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        # 并发调用时未加锁的 ``+=`` 会丢计数。
        self._usage_lock = threading.Lock()
        self.call_log: List[Dict[str, Any]] = []
        self._call_log_lock = threading.Lock()
        self._thread_local = threading.local()

    @property
    def last_usage(self) -> Optional[Dict[str, int]]:
        return getattr(self._thread_local, "last_usage", None)

    @staticmethod
    def _extract_anchor(user_prompt: str, filename: str) -> tuple[Optional[int], Optional[str]]:
        """从带行号的 Patch 中取第一处新增/上下文行，作为 line + evidence。"""
        pattern = rf"^### {re.escape(filename)}\n```diff\n(.*?)\n```"
        block = re.search(pattern, user_prompt, flags=re.MULTILINE | re.DOTALL)
        if not block:
            return None, None

        for raw_line in block.group(1).splitlines():
            added = re.match(r"^\s*(\d+) \| \+(.*)$", raw_line)
            if added and added.group(2).strip():
                return int(added.group(1)), added.group(2).strip()
            context = re.match(r"^\s*(\d+) \|  (.*)$", raw_line)
            if context and context.group(2).strip():
                return int(context.group(1)), context.group(2).strip()
        return None, None

    def generate_structured(
            self,
            system_prompt: str,
            user_prompt: str,
            response_schema: Type[T],
    ) -> T:
        started = time.perf_counter()
        self._thread_local.last_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        with self._usage_lock:
            self.usage["calls"] += 1

        # 1. 如果用户自定义了匹配的实例，优先返回
        if self.custom_schema and isinstance(self.custom_schema, response_schema):
            self._record_mock_call(
                started, system_prompt, user_prompt, response_schema
            )
            return self.custom_schema

        schema_name = response_schema.__name__

        # Verifier 的 Mock：默认认为所有候选问题都成立，方便离线跑通。
        if "VerifierBatchOutput" in schema_name:
            issue_ids = re.findall(r'"id"\s*:\s*"([^"]+)"', user_prompt)
            result = response_schema(
                results=[
                    {
                        "issue_id": issue_id,
                        "verdict": "verified",
                        "confidence": 0.8,
                        "reason": "[MOCK] 证据可定位。",
                        "evidence": "",
                        "counter_argument": "[MOCK] 周边代码不存在可绕过的校验路径。",
                    }
                    for issue_id in issue_ids
                ]
            )
            self._record_mock_call(
                started, system_prompt, user_prompt, response_schema
            )
            return result

        # 2. 审查输出 Schema（single-pass）
        if "AspectReviewOutputSchema" in schema_name:
            # 从 User Prompt 的 Patch 区块中取出真实文件名，
            # 避免 Mock 问题因“文件不在 Diff 中”被校验器全部过滤掉。
            changed_files = re.findall(
                r"^### (.+?)\n```diff", user_prompt, flags=re.MULTILINE
            )
            primary_file = changed_files[0] if changed_files else "src/example.py"
            secondary_file = (
                changed_files[1] if len(changed_files) > 1 else primary_file
            )
            primary_line, primary_evidence = self._extract_anchor(
                user_prompt, primary_file
            )
            secondary_line, secondary_evidence = self._extract_anchor(
                user_prompt, secondary_file
            )

            # 依据不同情况塞入模拟的 PotentialIssueSchema 列表
            mock_issues = [
                PotentialIssueSchema(
                    file=primary_file,
                    severity="high",
                    category="security",
                    confidence=0.92,
                    reasoning="发现硬编码的 JWT 密钥或敏感凭证，存在严重的安全隐患。",
                    suggestion="请将密钥移除，并改用环境变量（如 os.environ.get('JWT_SECRET')）进行加载。",
                    line=primary_line,
                    evidence=primary_evidence,
                ),
                PotentialIssueSchema(
                    file=secondary_file,
                    severity="medium",
                    category="clean_code",
                    confidence=0.85,
                    reasoning="函数体过长且嵌套层级较深，可读性较差。",
                    suggestion="建议将核心逻辑拆分为独立的小函数，遵循单一职责原则（SRP）。",
                    line=secondary_line,
                    evidence=secondary_evidence,
                )
            ]

            result = response_schema(
                aspect_summary="[MOCK] 本次审查完成，发现了一些需要注意的潜在问题。",
                issues=mock_issues
            )
            self._record_mock_call(
                started, system_prompt, user_prompt, response_schema
            )
            return result

        # 3. 针对全局总表的 Schema (ReviewResultSchema)
        result = response_schema(
            summary="[MOCK] 这是一个测试 PR Review 总结",
            risk_level="MEDIUM",
            risk_reason="[MOCK] 发现少量高风险或中风险问题，建议修复后再合并。",
            key_changes=["[MOCK] 调整了认证逻辑与参数传递"],
            potential_issues=[],
            missing_test_suggestions=["[MOCK] 建议针对修改后的认证流程补充单元测试。"],
            evidence=["src/auth/service.py"],
            final_decision="REQUEST_CHANGES",
            final_summary="[MOCK] 审查结束。最终决策: REQUEST_CHANGES (风险等级: MEDIUM)"
        )
        self._record_mock_call(started, system_prompt, user_prompt, response_schema)
        return result

    def _record_mock_call(
        self,
        started: float,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> None:
        with self._call_log_lock:
            self.call_log.append(
                {
                    "run_id": _REQUEST_RUN_ID.get(),
                    "attempt": _REQUEST_ATTEMPT.get(),
                    "status": "ok",
                    "error_type": "",
                    "model": self.model,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "estimated_prompt_tokens": _estimate_request_tokens(
                        system_prompt, user_prompt, response_schema
                    ),
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
            )