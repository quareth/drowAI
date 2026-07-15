"""Runtime-job orchestration service for Remote Runtime runner-control lifecycle records.

Scope:
- Creates tenant-bound runtime jobs with idempotency enforcement.
- Assigns jobs to tenant-bound runners and validates task/runner tenant ownership.
- Applies Remote Runtime runtime-job status transitions with stale/invalid transition checks.

Boundaries:
- Persists runtime-job metadata only; does not dispatch runner-channel messages.
- Uses existing ORM records and does not perform remote runtime side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.runner_control import Runner, RuntimeJob
from backend.services.runner_control.audit import RunnerControlAuditEmitter, RunnerControlAuditService
from backend.services.runner_control.metrics import RunnerControlMetrics
from runtime_shared.durable_secret_masking import mask_durable_secrets

_RUNTIME_JOB_STATUS_ORDER: dict[str, int] = {
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
_RUNTIME_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "lost", "expired"})
_RUNTIME_JOB_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"assigned", "cancelled", "expired", "failed"}),
    "assigned": frozenset({"dispatching", "cancelled", "lost", "expired", "failed"}),
    "dispatching": frozenset({"dispatched", "cancelled", "lost", "expired", "failed"}),
    "dispatched": frozenset({"acknowledged", "accepted", "running", "succeeded", "cancelled", "lost", "expired", "failed"}),
    "acknowledged": frozenset({"accepted", "running", "succeeded", "cancelled", "lost", "expired", "failed"}),
    "accepted": frozenset({"running", "succeeded", "cancelled", "lost", "expired", "failed"}),
    "running": frozenset({"succeeded", "cancelled", "lost", "expired", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "lost": frozenset(),
    "expired": frozenset(),
}

_SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "credential",
)
_REDACTED_MARKERS = {"<key_set>", "<no_key>", "<redacted>", "***"}
_ENCRYPTED_PREFIXES = ("enc:", "encrypted:")
logger = logging.getLogger(__name__)


class RuntimeJobServiceError(RuntimeError):
    """Raised when runtime-job operations fail with a deterministic error code."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class RuntimeJobCreateRequest:
    """Input payload for creating one tenant-bound runtime job."""

    tenant_id: int
    job_type: str
    idempotency_key: str
    task_id: int | None = None
    payload_json: Mapping[str, Any] | None = None
    correlation_id: str | None = None
    lease_expires_at: datetime | None = None


class RuntimeJobService:
    """Manage tenant-bound runtime-job records for the Runner Control control plane."""

    def __init__(
        self,
        db: Session,
        *,
        audit_emitter: RunnerControlAuditEmitter | None = None,
        metrics: RunnerControlMetrics | None = None,
    ) -> None:
        self._db = db
        self._audit = RunnerControlAuditService(emitter=audit_emitter)
        self._metrics = metrics or RunnerControlMetrics(db)

    def create_runtime_job(self, request: RuntimeJobCreateRequest) -> RuntimeJob:
        """Create one runtime job with tenant/task validation and idempotency checks."""

        normalized_job_type = _normalize_required_text(request.job_type, field_name="job_type")
        normalized_idempotency_key = _normalize_required_text(request.idempotency_key, field_name="idempotency_key")
        normalized_correlation_id = _normalize_optional_text(request.correlation_id)
        lease_expires_at = _ensure_utc(request.lease_expires_at)
        payload_json = _mask_optional_mapping(
            _clone_mapping(request.payload_json),
            source="runtime_job_payload",
        )

        if request.task_id is not None:
            self._require_task_in_tenant(tenant_id=request.tenant_id, task_id=request.task_id)

        existing = self._db.execute(
            select(RuntimeJob).where(
                RuntimeJob.tenant_id == request.tenant_id,
                RuntimeJob.job_type == normalized_job_type,
                RuntimeJob.idempotency_key == normalized_idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_IDEMPOTENCY_CONFLICT",
                message="Runtime job idempotency key already exists for tenant and job type.",
            )

        runtime_job = RuntimeJob(
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            runner_id=None,
            execution_site_id=None,
            job_type=normalized_job_type,
            status="queued",
            idempotency_key=normalized_idempotency_key,
            correlation_id=normalized_correlation_id,
            payload_json=payload_json,
            lease_expires_at=lease_expires_at,
        )

        try:
            with self._transaction_context():
                self._db.add(runtime_job)
                self._db.flush()
                self._audit.emit(
                    event_type="runtime_job.created",
                    tenant_id=runtime_job.tenant_id,
                    task_id=runtime_job.task_id,
                    runtime_job_id=runtime_job.id,
                    correlation_id=runtime_job.correlation_id,
                    metadata={
                        "job_type": runtime_job.job_type,
                        "idempotency_key": runtime_job.idempotency_key,
                        "status": runtime_job.status,
                    },
                )
                self._metrics.record_runtime_job_queue_depth(tenant_id=runtime_job.tenant_id)
                logger.info(
                    "runner_control.runtime_job_event tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s event=%s status=%s",
                    runtime_job.tenant_id,
                    runtime_job.runner_id,
                    runtime_job.id,
                    runtime_job.task_id,
                    None,
                    runtime_job.correlation_id,
                    "created",
                    runtime_job.status,
                )
        except IntegrityError as exc:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_IDEMPOTENCY_CONFLICT",
                message="Runtime job idempotency key already exists for tenant and job type.",
            ) from exc

        return runtime_job

    def assign_runtime_job(
        self,
        *,
        tenant_id: int,
        runtime_job_id: UUID,
        runner_id: UUID,
        lease_expires_at: datetime | None = None,
    ) -> RuntimeJob:
        """Assign one runtime job to a tenant-bound runner after ownership checks."""

        runtime_job = self._require_runtime_job(tenant_id=tenant_id, runtime_job_id=runtime_job_id)
        runner = self._require_runner_in_tenant(tenant_id=tenant_id, runner_id=runner_id)

        if runtime_job.task_id is not None:
            task = self._require_task_exists(task_id=runtime_job.task_id)
            if task.tenant_id != tenant_id:
                raise RuntimeJobServiceError(
                    error_code="RUNTIME_JOB_TASK_TENANT_MISMATCH",
                    message="Runtime job task does not belong to tenant.",
                )

        current_status = _normalize_status(runtime_job.status)
        if current_status == "assigned" and runtime_job.runner_id == runner.id:
            return runtime_job
        if current_status == "assigned" and runtime_job.runner_id != runner.id:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_ASSIGNMENT_CONFLICT",
                message="Runtime job is already assigned to a different runner.",
            )

        _assert_valid_transition(current_status=current_status, next_status="assigned")

        with self._transaction_context():
            runtime_job.runner_id = runner.id
            runtime_job.execution_site_id = runner.execution_site_id
            runtime_job.status = "assigned"
            if lease_expires_at is not None:
                runtime_job.lease_expires_at = _ensure_utc(lease_expires_at)
            self._db.flush()
            self._audit.emit(
                event_type="runtime_job.assigned",
                tenant_id=runtime_job.tenant_id,
                runner_id=runtime_job.runner_id,
                task_id=runtime_job.task_id,
                runtime_job_id=runtime_job.id,
                correlation_id=runtime_job.correlation_id,
                metadata={
                    "execution_site_id": str(runtime_job.execution_site_id) if runtime_job.execution_site_id else None,
                    "status": runtime_job.status,
                },
            )
            self._audit.emit(
                event_type="runner.assignment_created",
                tenant_id=runtime_job.tenant_id,
                runner_id=runtime_job.runner_id,
                task_id=runtime_job.task_id,
                runtime_job_id=runtime_job.id,
                correlation_id=runtime_job.correlation_id,
                metadata={
                    "source": "runtime_job.assign",
                    "job_type": runtime_job.job_type,
                },
            )
            self._metrics.record_runtime_job_queue_depth(tenant_id=runtime_job.tenant_id)
            logger.info(
                "runner_control.runtime_job_event tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s event=%s status=%s",
                runtime_job.tenant_id,
                runtime_job.runner_id,
                runtime_job.id,
                runtime_job.task_id,
                None,
                runtime_job.correlation_id,
                "assigned",
                runtime_job.status,
            )

        return runtime_job

    def transition_runtime_job(
        self,
        *,
        tenant_id: int,
        runtime_job_id: UUID,
        next_status: str,
        payload_json: Mapping[str, Any] | None = None,
        result_json: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> RuntimeJob:
        """Apply one validated Runner Control transition and persist metadata updates."""

        runtime_job = self._require_runtime_job(tenant_id=tenant_id, runtime_job_id=runtime_job_id)
        normalized_next = _normalize_required_status(next_status)
        current_status = _normalize_status(runtime_job.status)
        _assert_valid_transition(current_status=current_status, next_status=normalized_next)

        payload_copy = _mask_optional_mapping(
            _clone_mapping(payload_json),
            source="runtime_job_payload_transition",
        )
        result_copy = _mask_optional_mapping(
            _clone_mapping(result_json),
            source="runtime_job_result",
        )

        with self._transaction_context():
            runtime_job.status = normalized_next
            if payload_copy is not None:
                runtime_job.payload_json = payload_copy
            if result_copy is not None:
                runtime_job.result_json = result_copy
            if error_code is not None:
                runtime_job.error_code = _normalize_optional_text(error_code)
            if error_message is not None:
                runtime_job.error_message = _normalize_optional_text(
                    str(mask_durable_secrets(error_message, source="runtime_job_error_message"))
                )
            if lease_expires_at is not None:
                runtime_job.lease_expires_at = _ensure_utc(lease_expires_at)
            self._db.flush()
            self._metrics.record_runtime_job_queue_depth(tenant_id=runtime_job.tenant_id)
            logger.info(
                "runner_control.runtime_job_event tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s event=%s status=%s",
                runtime_job.tenant_id,
                runtime_job.runner_id,
                runtime_job.id,
                runtime_job.task_id,
                None,
                runtime_job.correlation_id,
                "transition",
                runtime_job.status,
            )

        return runtime_job

    def _require_runtime_job(self, *, tenant_id: int, runtime_job_id: UUID) -> RuntimeJob:
        runtime_job = self._db.execute(
            select(RuntimeJob).where(
                RuntimeJob.id == runtime_job_id,
                RuntimeJob.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if runtime_job is None:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_NOT_FOUND",
                message="Runtime job not found.",
            )
        return runtime_job

    def _require_runner_in_tenant(self, *, tenant_id: int, runner_id: UUID) -> Runner:
        runner = self._db.execute(select(Runner).where(Runner.id == runner_id)).scalar_one_or_none()
        if runner is None:
            raise RuntimeJobServiceError(
                error_code="RUNNER_NOT_FOUND",
                message="Runner not found.",
            )
        if runner.tenant_id != tenant_id:
            raise RuntimeJobServiceError(
                error_code="RUNNER_TENANT_MISMATCH",
                message="Runner does not belong to tenant.",
            )
        return runner

    def _require_task_in_tenant(self, *, tenant_id: int, task_id: int) -> Task:
        task = self._db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if task is None:
            raise RuntimeJobServiceError(
                error_code="TASK_NOT_FOUND",
                message="Task not found in tenant.",
            )
        return task

    def _require_task_exists(self, *, task_id: int) -> Task:
        task = self._db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
        if task is None:
            raise RuntimeJobServiceError(
                error_code="TASK_NOT_FOUND",
                message="Task not found.",
            )
        return task

    def _transaction_context(self) -> AbstractContextManager[object]:
        if self._db.in_transaction():
            return self._db.begin_nested()
        return self._db.begin()


def _assert_valid_transition(*, current_status: str, next_status: str) -> None:
    if current_status == next_status:
        if current_status in _RUNTIME_JOB_TERMINAL_STATUSES:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_TRANSITION_STALE",
                message="Runtime job transition is stale because job is already terminal.",
            )
        return

    if current_status in _RUNTIME_JOB_TERMINAL_STATUSES:
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_TRANSITION_STALE",
            message="Runtime job transition is stale because job is already terminal.",
        )

    allowed = _RUNTIME_JOB_ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None:
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_TRANSITION_INVALID",
            message=f"Unknown runtime job status: {current_status}",
        )

    if next_status in allowed:
        return

    current_order = _RUNTIME_JOB_STATUS_ORDER.get(current_status, 0)
    next_order = _RUNTIME_JOB_STATUS_ORDER.get(next_status, 0)
    if next_order <= current_order:
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_TRANSITION_STALE",
            message="Runtime job transition is stale.",
        )

    raise RuntimeJobServiceError(
        error_code="RUNTIME_JOB_TRANSITION_INVALID",
        message=f"Invalid runtime job transition: {current_status} -> {next_status}",
    )


def _normalize_required_text(value: str | None, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_VALIDATION_ERROR",
            message=f"{field_name} is required.",
        )
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_status(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_required_status(value: str) -> str:
    normalized = _normalize_status(value)
    if normalized not in _RUNTIME_JOB_STATUS_ORDER:
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_STATUS_INVALID",
            message=f"Unsupported runtime job status: {value}",
        )
    return normalized


def _clone_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {str(key): val for key, val in value.items()}


def _mask_optional_mapping(value: dict[str, Any] | None, *, source: str) -> dict[str, Any] | None:
    if value is None:
        return None
    masked = mask_durable_secrets(value, source=source)
    return masked if isinstance(masked, dict) else {}


def _assert_no_unredacted_secrets(payload: Mapping[str, Any], *, field_name: str) -> None:
    for path, value in _iter_leaf_nodes(payload):
        key_name = path.rsplit(".", 1)[-1].lower()
        if not _looks_sensitive_key(key_name):
            continue
        if _value_looks_redacted_or_encrypted(value):
            continue
        raise RuntimeJobServiceError(
            error_code="RUNTIME_JOB_PAYLOAD_POLICY_VIOLATION",
            message=(
                f"{field_name} contains sensitive field `{path}` without redaction/encryption marker. "
                "Store redacted or encrypted values only."
            ),
        )


def _iter_leaf_nodes(value: Any, *, base_path: str = "$") -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        if not value:
            return [(base_path, value)]
        for key, child in value.items():
            child_path = f"{base_path}.{key}"
            leaves.extend(_iter_leaf_nodes(child, base_path=child_path))
        return leaves
    if isinstance(value, list):
        if not value:
            return [(base_path, value)]
        for index, child in enumerate(value):
            child_path = f"{base_path}[{index}]"
            leaves.extend(_iter_leaf_nodes(child, base_path=child_path))
        return leaves
    return [(base_path, value)]


def _looks_sensitive_key(key_name: str) -> bool:
    normalized = str(key_name or "").strip().lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _value_looks_redacted_or_encrypted(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        encrypted_flag = value.get("encrypted")
        if isinstance(encrypted_flag, bool) and encrypted_flag:
            return True
        ciphertext = str(value.get("ciphertext") or "").strip()
        if ciphertext:
            return True
        marker = str(value.get("value") or "").strip().lower()
        if marker in _REDACTED_MARKERS:
            return True
        return False

    normalized = str(value).strip().lower()
    if not normalized:
        return True
    if normalized in _REDACTED_MARKERS:
        return True
    if "redacted" in normalized:
        return True
    if any(normalized.startswith(prefix) for prefix in _ENCRYPTED_PREFIXES):
        return True
    return False


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
