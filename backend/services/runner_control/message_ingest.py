"""Runner control-channel message ingest helpers for heartbeat/capacity handling.

This module owns backend-side interpretation of self-reported runner heartbeat
capacity payloads. It keeps metadata treatment explicit: labels/capabilities are
stored as runner-reported observability fields and never used for authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.services.runner_control.coordination import InboundIdempotencyRecord, RunnerCoordinationStore
from runtime_shared.runner_protocol import (
    RunnerCapacityPayload,
    RunnerEnvelope,
    RunnerHeartbeatPayload,
    RunnerMessageType,
    RunnerToolResultPayload,
    is_completed_process_tool_result_status,
    sanitize_tool_result_payload_for_persistence,
)

_RUNTIME_JOB_STATE_ORDER: dict[str, int] = {
    "queued": 10,
    "assigned": 20,
    "dispatching": 30,
    "dispatched": 40,
    "acknowledged": 50,
    "accepted": 60,
    "running": 70,
    "succeeded": 80,
    "failed": 90,
    "cancelled": 90,
    "lost": 90,
    "expired": 90,
}
_RUNTIME_JOB_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "lost", "expired"})


@dataclass(frozen=True, slots=True)
class InboundMessageLedgerOutcome:
    """Idempotency-ledger outcome for one inbound runner message."""

    record: InboundIdempotencyRecord
    should_apply_side_effects: bool


def capacity_snapshot_from_envelope(envelope: RunnerEnvelope) -> dict[str, Any]:
    """Return latest capacity JSON payload from a parsed heartbeat/capacity envelope."""
    payload = envelope.payload
    if isinstance(payload, RunnerHeartbeatPayload):
        return _capacity_payload_to_json(payload.capacity)
    if isinstance(payload, RunnerCapacityPayload):
        return _capacity_payload_to_json(payload)
    return {}


def lease_is_stale(*, now: datetime, lease_expires_at: datetime | None) -> bool:
    """Return true when a runner presence lease has expired or is missing."""
    if lease_expires_at is None:
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
    return lease_expires_at <= now


def build_runner_message_idempotency_key(envelope: RunnerEnvelope) -> str:
    """Build a stable idempotency key for one runner envelope."""
    return f"{str(envelope.tenant_id).strip()}:{str(envelope.runner_id).strip()}:{str(envelope.message_id).strip()}"


def build_runtime_job_transition_idempotency_key(
    *,
    envelope: RunnerEnvelope,
    transition_status: str,
) -> str | None:
    """Build a stable business-key idempotency value for runtime-job transitions."""
    runtime_job_id = str(envelope.runtime_job_id or "").strip()
    normalized_transition = _normalize_status(transition_status)
    if not runtime_job_id or not normalized_transition:
        return None
    return f"runtime_job:{runtime_job_id}:transition:{normalized_transition}"


def _runtime_job_transition_status_for_envelope(*, envelope: RunnerEnvelope) -> str | None:
    message_type = envelope.message_type
    if message_type in {
        RunnerMessageType.RUNTIME_STARTED,
        RunnerMessageType.RUNTIME_PAUSED,
        RunnerMessageType.RUNTIME_RESUMED,
        RunnerMessageType.RUNTIME_STOPPED,
        RunnerMessageType.RUNTIME_RETIRED,
        RunnerMessageType.TERMINAL_RESULT,
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_INVENTORY,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
    }:
        return "succeeded"
    if message_type is RunnerMessageType.RUNTIME_FAILED:
        return "failed"
    if message_type is RunnerMessageType.TOOL_RESULT and isinstance(envelope.payload, RunnerToolResultPayload):
        payload_status = str(envelope.payload.status or "").strip().lower()
        if is_completed_process_tool_result_status(payload_status):
            return None
        if payload_status in {"succeeded"} and envelope.payload.success:
            return "succeeded"
        return "failed"
    return None


def is_stale_runtime_job_transition(*, current_status: str | None, next_status: str) -> bool:
    """Return true when the incoming runtime-job transition is stale."""
    normalized_next = _normalize_status(next_status)
    if not normalized_next:
        return False

    normalized_current = _normalize_status(current_status)
    if not normalized_current:
        return False
    if normalized_current == normalized_next:
        return False
    if normalized_current in _RUNTIME_JOB_TERMINAL_STATES:
        return True

    current_order = _RUNTIME_JOB_STATE_ORDER.get(normalized_current, 0)
    next_order = _RUNTIME_JOB_STATE_ORDER.get(normalized_next, current_order)
    return next_order < current_order


def record_inbound_message_decision(
    *,
    coordination_store: RunnerCoordinationStore,
    tenant_id: int,
    runner_id: UUID,
    envelope: RunnerEnvelope,
    status: str,
    idempotency_key: str | None = None,
    payload_json: dict[str, Any] | None = None,
    runtime_job_id: UUID | None = None,
    task_id: int | None = None,
    correlation_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> InboundMessageLedgerOutcome:
    """Record/replay one inbound message decision and indicate side-effect safety."""
    resolved_runtime_job_id = _parse_optional_uuid(envelope.runtime_job_id) if runtime_job_id is None else runtime_job_id
    persisted_payload_json = dict(payload_json) if payload_json is not None else envelope.to_dict().get("payload")
    if envelope.message_type is RunnerMessageType.TOOL_RESULT and isinstance(persisted_payload_json, dict):
        persisted_payload_json = sanitize_tool_result_payload_for_persistence(persisted_payload_json)
    if envelope.message_type in {
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    } and isinstance(persisted_payload_json, dict):
        persisted_payload_json = _sanitize_artifact_payload_for_persistence(persisted_payload_json)

    record = coordination_store.record_inbound_message_idempotency(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_id=envelope.message_id,
        message_type=envelope.type,
        idempotency_key=idempotency_key or build_runner_message_idempotency_key(envelope),
        status=str(status).strip() or "accepted",
        payload_json=persisted_payload_json,
        runtime_job_id=resolved_runtime_job_id,
        task_id=envelope.task_id if task_id is None else task_id,
        correlation_id=envelope.correlation_id if correlation_id is None else correlation_id,
        error_code=error_code,
        error_message=error_message,
    )
    return InboundMessageLedgerOutcome(
        record=record,
        should_apply_side_effects=(not record.duplicate and record.status == "accepted"),
    )


def _capacity_payload_to_json(payload: RunnerCapacityPayload) -> dict[str, Any]:
    return {
        "active_tasks": int(payload.active_tasks),
        "max_active_tasks": int(payload.max_active_tasks),
        "available_tasks": int(payload.available_tasks),
        "max_parallel_commands_per_task": int(payload.max_parallel_commands_per_task),
        "docker_available": bool(payload.docker_available),
        "runtime_image": payload.runtime_image,
        "runtime_image_available": bool(payload.runtime_image_available),
        "version": payload.version,
        # Runner-reported metadata is observability data, not an authz source.
        "capabilities": list(payload.capabilities),
        "labels": dict(payload.labels),
        "active_runtime_jobs": [
            {
                "runtime_job_id": item.runtime_job_id,
                "task_id": item.task_id,
                "workspace_id": item.workspace_id,
                "status": item.status,
            }
            for item in payload.active_runtime_jobs
        ],
    }


def _normalize_status(status: str | None) -> str:
    return str(status or "").strip().lower()


def _parse_optional_uuid(raw_value: str | None) -> UUID | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        return UUID(normalized)
    except ValueError:
        return None


def _sanitize_artifact_payload_for_persistence(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        normalized_key = str(key).strip()
        lowered_key = normalized_key.lower()
        if lowered_key in {"upload_url", "signed_url"} or lowered_key.endswith("_url"):
            sanitized[normalized_key] = "<redacted>"
            continue
        if lowered_key in {"upload_headers", "authorization"}:
            sanitized[normalized_key] = "<redacted>"
            continue
        if isinstance(value, dict):
            sanitized[normalized_key] = _sanitize_artifact_payload_for_persistence(dict(value))
            continue
        if isinstance(value, list):
            sanitized[normalized_key] = [
                _sanitize_artifact_payload_for_persistence(dict(item))
                if isinstance(item, dict)
                else item
                for item in value
            ]
            continue
        sanitized[normalized_key] = value
    return sanitized
