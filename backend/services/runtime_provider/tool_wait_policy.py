"""Shared wait policy helpers for runner tool-command results.

Providers use these helpers to distinguish short control-plane socket timeouts
from per-tool execution deadlines and optional result wait grace periods.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.services.runtime_provider.contracts import RuntimeOperationRequest


def should_wait_for_tool_result(request: RuntimeOperationRequest) -> bool:
    """Return whether this operation should block until a tool result is terminal."""
    raw = request.payload.get("wait_for_result", request.metadata.get("wait_for_result", True))
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def resolve_tool_result_wait_timeout_seconds(
    *,
    request: RuntimeOperationRequest,
    timeout_seconds: float,
    timeout_policy: Mapping[str, Any],
) -> float:
    """Resolve how long the provider may poll for the runner result."""
    explicit_wait_timeout = request.metadata.get("wait_timeout_seconds")
    if explicit_wait_timeout is not None:
        return coerce_non_negative_float(explicit_wait_timeout, default=0.0)

    deadline_value = timeout_policy.get("deadline_seconds")
    grace_value = timeout_policy.get("grace_seconds")
    if deadline_value is not None:
        deadline_seconds = coerce_non_negative_float(deadline_value, default=0.0)
        grace_seconds = coerce_non_negative_float(grace_value, default=0.0)
        return max(0.0, deadline_seconds + grace_seconds)

    return max(0.0, coerce_non_negative_float(timeout_seconds, default=30.0))


def coerce_non_negative_float(value: object, *, default: float) -> float:
    """Coerce a value to a non-negative float."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return default
    return number


__all__ = [
    "coerce_non_negative_float",
    "resolve_tool_result_wait_timeout_seconds",
    "should_wait_for_tool_result",
]
