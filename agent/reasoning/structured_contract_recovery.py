"""Structured-contract failure types and bounded async retry helpers.

This module centralizes the shared error model for contract-invalid LLM output
and the small retry policy used by turn-critical runtime stages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Literal, Optional, TypeVar

from backend.services.metrics.utils import safe_inc
from agent.providers.llm.core.exceptions import LLMRefusalError
from core.llm.timeout_runtime import LLMTimeoutError

StructuredContractFailureKind = Literal[
    "provider_parse_error",
    "schema_validation_error",
    "semantic_validation_error",
]

_SENSITIVE_DIAGNOSTIC_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "token",
        "raw_content",
        "response",
        "response_text",
        "prompt",
        "system_prompt",
        "user_prompt",
    }
)

T = TypeVar("T")


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """Return one exception and its causal/context chain without duplicates."""
    seen: set[int] = set()
    ordered: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop(0)
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        ordered.append(current)
        cause = getattr(current, "__cause__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        context = getattr(current, "__context__", None)
        if isinstance(context, BaseException):
            stack.append(context)
    return ordered


def contains_retryable_llm_timeout(exc: BaseException) -> bool:
    """Return whether an exception chain contains a retryable LLM timeout."""
    return any(isinstance(candidate, LLMTimeoutError) for candidate in _iter_exception_chain(exc))


def sanitize_structured_contract_diagnostics(
    diagnostics: Optional[Dict[str, Any]],
    *,
    max_depth: int = 3,
    max_items: int = 20,
    max_string_len: int = 240,
) -> Dict[str, Any]:
    """Return bounded, redacted diagnostics safe for logs and workflow metadata."""
    if not isinstance(diagnostics, dict):
        return {}

    def _sanitize_value(value: Any, depth: int) -> Any:
        if depth >= max_depth:
            return "<omitted>"
        if isinstance(value, str):
            text = value.strip()
            if len(text) > max_string_len:
                return f"{text[:max_string_len]}..."
            return text
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, dict):
            sanitized_map: Dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(value.items()):
                if index >= max_items:
                    sanitized_map["<truncated>"] = f"{len(value) - max_items} entries omitted"
                    break
                key = str(raw_key)
                if key.lower() in _SENSITIVE_DIAGNOSTIC_KEYS:
                    sanitized_map[key] = "<redacted>"
                    continue
                sanitized_map[key] = _sanitize_value(raw_value, depth + 1)
            return sanitized_map
        if isinstance(value, list):
            sanitized_list = []
            for index, entry in enumerate(value):
                if index >= max_items:
                    sanitized_list.append("<truncated>")
                    break
                sanitized_list.append(_sanitize_value(entry, depth + 1))
            return sanitized_list
        return str(value)

    return _sanitize_value(diagnostics, 0) if isinstance(diagnostics, dict) else {}


class StructuredContractViolationError(RuntimeError):
    """Typed failure for contract-invalid LLM output in runtime stages."""

    def __init__(
        self,
        *,
        error_code: str,
        stage: str,
        contract: str,
        kind: StructuredContractFailureKind,
        details: str,
        retryable: bool = True,
        retry_mode: str = "checkpoint",
        diagnostics: Optional[Dict[str, Any]] = None,
        graph_name: Optional[str] = None,
    ) -> None:
        self.error_code = error_code.strip() if isinstance(error_code, str) else ""
        self.stage = stage.strip() if isinstance(stage, str) else ""
        self.contract = contract.strip() if isinstance(contract, str) else ""
        self.kind: StructuredContractFailureKind = kind
        self.retryable = bool(retryable)
        self.retry_mode = (
            retry_mode.strip() if isinstance(retry_mode, str) and retry_mode.strip() else "checkpoint"
        )
        self.diagnostics = sanitize_structured_contract_diagnostics(diagnostics)
        self.graph_name = graph_name.strip() if isinstance(graph_name, str) and graph_name.strip() else None
        message = details.strip() if isinstance(details, str) and details.strip() else error_code
        super().__init__(message)


def _default_retryable_check(exc: BaseException) -> bool:
    if contains_retryable_llm_timeout(exc):
        return True
    return isinstance(exc, StructuredContractViolationError) and bool(getattr(exc, "retryable", False))


async def run_structured_contract_retry(
    *,
    operation: Callable[[], Awaitable[T]],
    logger: logging.Logger,
    stage: str,
    contract: str,
    max_attempts: int = 2,
    backoff_seconds: float = 0.25,
    is_retryable_error: Optional[Callable[[BaseException], bool]] = None,
) -> T:
    """Run one async operation with bounded retries for retryable contract failures."""
    attempts = max(1, int(max_attempts))
    retry_check = is_retryable_error or _default_retryable_check

    attempt = 1
    while True:
        try:
            return await operation()
        except LLMRefusalError:
            raise
        except Exception as exc:
            retryable = retry_check(exc)
            if not retryable or attempt >= attempts:
                if retryable:
                    safe_inc("structured_contract_retry_exhausted")
                    logger.warning(
                        "[STRUCTURED_CONTRACT] Retry exhausted (stage=%s contract=%s attempts=%s error=%s)",
                        stage,
                        contract,
                        attempt,
                        exc,
                    )
                raise

            safe_inc("structured_contract_retry_attempts")
            logger.warning(
                "[STRUCTURED_CONTRACT] Retrying after contract failure (stage=%s contract=%s attempt=%s/%s error=%s)",
                stage,
                contract,
                attempt,
                attempts,
                exc,
            )
            attempt += 1
            if backoff_seconds > 0:
                await asyncio.sleep(backoff_seconds)


__all__ = [
    "StructuredContractFailureKind",
    "StructuredContractViolationError",
    "contains_retryable_llm_timeout",
    "run_structured_contract_retry",
    "sanitize_structured_contract_diagnostics",
]
