"""Retry-context reader for graph runtime / prompt context modules.

Phase 2.4 of the checkpoint-retry foundation surfaces the canonical
retry identity (``retry_attempt``, ``retry_max_attempts``) and a
sanitized ``previous_failure`` projection onto the LangGraph run config
under ``configurable``. This helper is the single read-side accessor
graph nodes and prompt builders use so they can distinguish a retry
continuation from a fresh resume and choose a corrected/alternate path
on retryable agent-process failures (bad tool arguments, invalid tool
calls, structured-output validation, ...).

Read-only: this module never mutates configurable, never mutates state,
and never re-derives retry identity from the checkpointer. The
authoritative producer is
``backend.services.langgraph_chat.checkpoint.execution_config.build_checkpoint_execution_config``.
The sanitization invariant from Phase 1.1 is preserved here — only the
whitelisted previous-failure fields are exposed; raw provider payloads,
auth headers, and secrets never reach this surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional


_PREVIOUS_FAILURE_WHITELIST = (
    "error_code",
    "failure_stage",
    "graph_name",
    "tool_name",
    "tool_call_id",
    "summary",
)


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class RetryContext:
    """Sanitized retry context surfaced to graph runtime / prompt code.

    All fields are optional because resume/HITL continuations carry no
    retry context. Callers MUST treat ``RetryContext.is_retry`` as the
    only signal that the current run is a retry attempt.
    """

    retry_attempt: Optional[int] = None
    retry_max_attempts: Optional[int] = None
    previous_failure: Optional[Dict[str, Any]] = None

    @property
    def is_retry(self) -> bool:
        """True when the current run is a checkpoint retry attempt."""
        return self.retry_attempt is not None or bool(self.previous_failure)


def _extract_configurable(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    candidate = config.get("configurable")
    if isinstance(candidate, Mapping):
        return candidate
    return {}


def _sanitize_previous_failure(
    raw: Any,
) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping) or not raw:
        return None
    sanitized: Dict[str, Any] = {}
    for key in _PREVIOUS_FAILURE_WHITELIST:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            sanitized[key] = value.strip()
    return sanitized or None


def read_retry_context(
    config: Optional[Mapping[str, Any]],
) -> RetryContext:
    """Read sanitized retry context off a LangGraph run config.

    Returns an empty ``RetryContext`` when the config has no retry
    identity (resume/HITL or fresh-turn paths). Reads are defensive:
    malformed values are treated as missing.
    """
    configurable = _extract_configurable(config)
    if not configurable:
        return RetryContext()

    retry_attempt = _coerce_int(configurable.get("retry_attempt"))
    retry_max_attempts = _coerce_int(configurable.get("retry_max_attempts"))
    previous_failure = _sanitize_previous_failure(configurable.get("previous_failure"))
    return RetryContext(
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        previous_failure=previous_failure,
    )


__all__ = ["RetryContext", "read_retry_context"]
