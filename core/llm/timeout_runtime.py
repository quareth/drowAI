"""Reusable runtime helpers for LLM timeout enforcement and logging.

This module provides consistent timeout wrappers for non-streaming requests and
streaming iterators so backend and agent/runtime call sites share one timeout
format and behavior.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional

_SENSITIVE_DETAIL_MARKERS = (
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "jwt",
    "password",
    "secret",
    "token",
)
_MAX_TIMEOUT_DETAIL_LENGTH = 240


class LLMTimeoutError(asyncio.TimeoutError):
    """Retryable LLM timeout with sanitized metadata for shared failure handling."""

    error_code = "llm_timeout"
    retryable = True
    retry_mode = "checkpoint"
    graph_name = None

    def __init__(
        self,
        *,
        task_id: Optional[Any],
        component: str,
        operation: str,
        timeout_sec: float,
        outcome: str,
        details: str = "",
    ) -> None:
        self.task_id = task_id
        self.component = component
        self.operation = operation
        self.timeout_sec = timeout_sec
        self.outcome = outcome
        self.details = _sanitize_timeout_details(details)
        self.diagnostics = {
            "component": component,
            "operation": operation,
            "timeout_sec": timeout_sec,
            "outcome": outcome,
        }
        if task_id is not None:
            self.diagnostics["task_id"] = str(task_id)
        if self.details:
            self.diagnostics["details"] = self.details
        super().__init__(
            "LLM request timed out during "
            f"{component}.{operation} after {timeout_sec:.2f} seconds"
        )


def _sanitize_timeout_details(details: str) -> str:
    """Return bounded timeout details safe for retry metadata."""
    if not isinstance(details, str) or not details.strip():
        return ""
    normalized = details.strip()
    lower = normalized.lower()
    if any(marker in lower for marker in _SENSITIVE_DETAIL_MARKERS):
        return "<redacted>"
    if len(normalized) > _MAX_TIMEOUT_DETAIL_LENGTH:
        return f"{normalized[:_MAX_TIMEOUT_DETAIL_LENGTH]}..."
    return normalized


def format_timeout_log_message(
    *,
    task_id: Optional[Any],
    component: str,
    operation: str,
    timeout_sec: float,
    outcome: str,
    details: str = "",
) -> str:
    """Return the canonical timeout log message used across runtimes."""
    task_label = "n/a" if task_id is None else str(task_id)
    detail_str = f" | {details}" if details else ""
    return (
        f"TIMEOUT | Task {task_label} | {component} | {operation} | "
        f"timeout_sec={timeout_sec:.2f} | outcome={outcome}{detail_str}"
    )


def log_timeout_event(
    logger: Any,
    *,
    task_id: Optional[Any],
    component: str,
    operation: str,
    timeout_sec: float,
    outcome: str,
    details: str = "",
) -> None:
    """Emit one timeout event using the canonical log format."""
    logger.warning(
        format_timeout_log_message(
            task_id=task_id,
            component=component,
            operation=operation,
            timeout_sec=timeout_sec,
            outcome=outcome,
            details=details,
        )
    )


async def _run_timeout_callback(
    callback: Optional[Callable[[], Any]],
) -> None:
    """Execute an optional timeout callback, awaiting it when needed."""
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


async def wait_for_with_timeout(
    awaitable: Awaitable[Any],
    *,
    timeout_sec: float,
    component: str,
    operation: str,
    logger: Any,
    task_id: Optional[Any] = None,
    outcome: str = "request_timeout",
    details: str = "",
    on_timeout: Optional[Callable[[], Any]] = None,
) -> Any:
    """Wrap one awaitable with timeout logging and re-raise on timeout."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_sec)
    except asyncio.TimeoutError as exc:
        log_timeout_event(
            logger,
            task_id=task_id,
            component=component,
            operation=operation,
            timeout_sec=timeout_sec,
            outcome=outcome,
            details=details,
        )
        await _run_timeout_callback(on_timeout)
        raise LLMTimeoutError(
            task_id=task_id,
            component=component,
            operation=operation,
            timeout_sec=timeout_sec,
            outcome=outcome,
            details=details,
        ) from exc


async def iter_with_idle_timeout(
    async_iterable: AsyncIterator[Any],
    *,
    timeout_sec: float,
    component: str,
    operation: str,
    logger: Any,
    task_id: Optional[Any] = None,
    outcome: str = "stream_idle_timeout",
    details: str = "",
    on_timeout: Optional[Callable[[], Any]] = None,
) -> AsyncIterator[Any]:
    """Yield async-iterator items, timing out only when chunk delivery stalls."""
    iterator = async_iterable.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_sec)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            log_timeout_event(
                logger,
                task_id=task_id,
                component=component,
                operation=operation,
                timeout_sec=timeout_sec,
                outcome=outcome,
                details=details,
            )
            await _run_timeout_callback(on_timeout)
            raise LLMTimeoutError(
                task_id=task_id,
                component=component,
                operation=operation,
                timeout_sec=timeout_sec,
                outcome=outcome,
                details=details,
            ) from exc
        else:
            yield item


__all__ = [
    "LLMTimeoutError",
    "format_timeout_log_message",
    "iter_with_idle_timeout",
    "log_timeout_event",
    "wait_for_with_timeout",
]
