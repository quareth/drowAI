"""Runner-control audit helpers with tenant-safe event envelopes and redaction.

Scope:
- Defines a single audit event emitter used by runner control services.
- Enforces stable event envelope fields and metadata redaction defaults.

Boundaries:
- Emits structured events to an injected sink (logger by default).
- Does not persist audit records directly or depend on router/ORM internals.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
from typing import Any
from uuid import UUID

from backend.services.audit import build_tenant_audit_event, redact_audit_metadata

logger = logging.getLogger(__name__)

RunnerControlAuditEmitter = Callable[[dict[str, Any]], None]
__all__ = ["RunnerControlAuditEmitter", "RunnerControlAuditService", "redact_audit_metadata"]


class RunnerControlAuditService:
    """Emit runner-control audit events with required identity fields."""

    def __init__(self, *, emitter: RunnerControlAuditEmitter | None = None) -> None:
        self._emitter = emitter or _default_emitter

    def emit(
        self,
        *,
        event_type: str,
        tenant_id: int,
        runner_id: UUID | str | None = None,
        task_id: int | None = None,
        runtime_job_id: UUID | str | None = None,
        correlation_id: str | None = None,
        actor_user_id: int | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        action: str | None = None,
        result: str | None = None,
        reason_code: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit one audit event with redacted metadata."""

        event = build_tenant_audit_event(
            event_type=event_type,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            result=result,
            reason_code=reason_code,
            runner_id=_normalize_optional_id(runner_id),
            task_id=task_id,
            runtime_job_id=_normalize_optional_id(runtime_job_id),
            correlation_id=_normalize_optional_text(correlation_id),
            metadata=dict(metadata or {}),
        )
        self._emitter(event)


def _normalize_optional_id(value: UUID | str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _default_emitter(event: dict[str, Any]) -> None:
    logger.info("runner_control.audit event=%s", event)
