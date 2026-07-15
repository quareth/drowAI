"""Safe audit payload helpers for retention runs.

This module normalizes retention audit reason codes and serializes audit
payloads while rejecting or removing fields that could carry secrets, prompts,
transcripts, object keys, or raw content.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from enum import Enum
import logging
from typing import Any, Mapping, Sequence

from backend.services.retention.contracts import (
    RetentionExecutorResult,
    RetentionRunResult,
    normalize_reason_code,
)


logger = logging.getLogger(__name__)
RetentionAuditEmitter = Callable[[dict[str, Any]], None]


UNSAFE_AUDIT_KEY_PARTS: frozenset[str] = frozenset(
    {
        "api_key",
        "authorization",
        "content",
        "cookie",
        "object_key",
        "payload",
        "prompt",
        "secret",
        "token",
        "transcript",
    }
)


class UnsafeAuditPayloadError(ValueError):
    """Raised when an audit payload contains an unsafe field name."""


class RetentionAuditService:
    """Emit safe structured audit events for retention runs."""

    def __init__(self, *, emitter: RetentionAuditEmitter | None = None) -> None:
        self._emitter = emitter or _default_audit_emitter

    def emit_executor_result(
        self,
        result: RetentionExecutorResult,
        *,
        duration_seconds: float,
    ) -> None:
        """Emit one no-secret executor completion audit event."""

        self._emit(
            {
                "event_type": "retention.executor_completed",
                "tenant_id": result.tenant_id,
                "executor_name": result.executor_name,
                "retention_class": result.retention_class,
                "mode": result.mode,
                "succeeded": result.succeeded,
                "error_code": result.error_code,
                "duration_seconds": _duration_value(duration_seconds),
                "counts": result.counts,
                "reason_counts": _reason_count_records(result.reason_counts),
            }
        )

    def emit_run_result(
        self,
        result: RetentionRunResult,
        *,
        duration_seconds: float,
    ) -> None:
        """Emit one no-secret aggregate run completion audit event."""

        self._emit(
            {
                "event_type": "retention.run_completed",
                "mode": result.mode,
                "scope": result.scope,
                "tenant_id": result.tenant_id,
                "succeeded": result.succeeded,
                "duration_seconds": _duration_value(duration_seconds),
                "executor_count": len(result.results),
                "failed_executor_count": sum(
                    1 for item in result.results if not item.succeeded
                ),
                "counts": _aggregate_counts(result),
            }
        )

    def _emit(self, payload: Mapping[str, Any]) -> None:
        safe_payload = to_safe_audit_payload(payload)
        try:
            self._emitter(safe_payload)
        except Exception:
            logger.error("retention audit emission failed", exc_info=True)


def normalize_audit_reason_code(value: str) -> str:
    """Return a canonical retention audit reason code."""

    return normalize_reason_code(value)


def to_safe_audit_payload(
    payload: Mapping[str, Any],
    *,
    strip_unsafe: bool = False,
) -> dict[str, Any]:
    """Return a JSON-compatible no-secret audit payload.

    Unsafe keys are rejected by default. Callers that need best-effort audit
    output can set ``strip_unsafe=True`` to remove unsafe keys and their values.
    """

    serialized = _serialize_audit_value(payload)
    if not isinstance(serialized, dict):
        raise TypeError("audit payload root must be a mapping")
    return _sanitize_audit_mapping(serialized, strip_unsafe=strip_unsafe)


def _serialize_audit_value(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize_audit_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _serialize_audit_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_audit_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_serialize_audit_value(item) for item in value]
    return value


def _sanitize_audit_value(value: Any, *, strip_unsafe: bool) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_audit_mapping(value, strip_unsafe=strip_unsafe)
    if isinstance(value, list):
        return [
            _sanitize_audit_value(item, strip_unsafe=strip_unsafe)
            for item in value
        ]
    return value


def _sanitize_audit_mapping(
    value: Mapping[str, Any],
    *,
    strip_unsafe: bool,
) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if _audit_key_is_unsafe(key):
            if strip_unsafe:
                continue
            raise UnsafeAuditPayloadError(f"unsafe audit payload field: {key}")
        sanitized[key] = _sanitize_audit_value(item, strip_unsafe=strip_unsafe)
    return sanitized


def _audit_key_is_unsafe(key: str) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return any(unsafe_part in normalized for unsafe_part in UNSAFE_AUDIT_KEY_PARTS)


def _aggregate_counts(result: RetentionRunResult) -> dict[str, int | None]:
    totals: dict[str, int | None] = {}
    for executor_result in result.results:
        for key, value in asdict(executor_result.counts).items():
            if value is None:
                totals.setdefault(key, None)
                continue
            totals[key] = int(totals.get(key) or 0) + int(value)
    return totals


def _reason_count_records(
    reason_counts: Mapping[str, int],
) -> tuple[dict[str, int | str], ...]:
    return tuple(
        {"reason_code": normalize_audit_reason_code(reason_code), "count": int(count)}
        for reason_code, count in sorted(reason_counts.items())
    )


def _duration_value(duration_seconds: float) -> float:
    return max(0.0, float(duration_seconds))


def _default_audit_emitter(event: dict[str, Any]) -> None:
    logger.info("retention.audit event=%s", event)


__all__ = [
    "RetentionAuditEmitter",
    "RetentionAuditService",
    "UNSAFE_AUDIT_KEY_PARTS",
    "UnsafeAuditPayloadError",
    "normalize_audit_reason_code",
    "to_safe_audit_payload",
]
