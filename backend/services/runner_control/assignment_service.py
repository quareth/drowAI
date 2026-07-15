"""Runner assignment eligibility and deterministic selection for Runner Control.

Scope:
- Selects tenant-bound eligible runners using status, credential, presence,
  version, capability, label, and capacity checks.
- Returns stable reason codes when no runner is eligible.

Boundaries:
- Selection logic only; does not mutate tasks/runtime jobs and does not perform
  control-channel delivery.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import sys
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config.feature_flags import get_local_max_active_tasks_default, resolve_task_concurrency_limit
from backend.domain.task_admission import (
    NO_RUNNERS_REGISTERED,
    RUNNER_CAPABILITY_MISMATCH,
    RUNNER_CAPACITY_EXHAUSTED,
    RUNNER_CREDENTIAL_NOT_ACTIVE,
    RUNNER_EXECUTION_SITE_MISMATCH,
    RUNNER_HEARTBEAT_STALE,
    RUNNER_LABEL_MISMATCH,
    RUNNER_MAINTENANCE_MODE,
    RUNNER_NOT_ONLINE,
    RUNNER_PROTOCOL_INCOMPATIBLE,
    RUNNER_REVOKED,
    RUNNER_RUNTIME_VERSION_INCOMPATIBLE,
    RUNNER_STALE_OR_OFFLINE,
)
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task
from backend.models.runner_control import Runner, RunnerConnection, RunnerCredential
from backend.services.runner_control.metrics import RunnerControlMetrics
from runtime_shared.runner_protocol import RUNNER_PROTOCOL_SCHEMA_VERSION

_ONLINE_STATUSES = frozenset({"active", "online"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunnerAssignmentRequest:
    """Input constraints for selecting one tenant-bound runner."""

    tenant_id: int
    execution_site_id: UUID | None = None
    required_protocol_version: str | None = None
    required_runtime_version: str | None = None
    required_capabilities: Sequence[str] = ()
    required_labels: Mapping[str, str] | None = None
    minimum_available_tasks: int = 1


@dataclass(frozen=True, slots=True)
class RunnerSelection:
    """Selected runner placement snapshot."""

    runner_id: UUID
    execution_site_id: UUID
    available_tasks: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RunnerAssignmentResult:
    """Outcome of assignment selection with optional no-eligible reasons."""

    selection: RunnerSelection | None
    reason_codes: tuple[str, ...]
    evaluated_runner_count: int


@dataclass(frozen=True, slots=True)
class _LeaseSnapshot:
    lease_expires_at: datetime


class RunnerAssignmentService:
    """Select a single eligible runner for a tenant-scoped assignment request."""

    def __init__(
        self,
        db: Session,
        *,
        heartbeat_stale_after: timedelta = timedelta(seconds=120),
        metrics: RunnerControlMetrics | None = None,
    ) -> None:
        self._db = db
        self._heartbeat_stale_after = heartbeat_stale_after
        self._metrics = metrics or RunnerControlMetrics(db)

    def select_runner(
        self,
        request: RunnerAssignmentRequest,
        *,
        now: datetime | None = None,
    ) -> RunnerAssignmentResult:
        """Return one deterministic eligible runner or stable reason codes."""

        now_utc = _ensure_utc(now or datetime.now(tz=UTC))
        required_labels = _normalize_labels(request.required_labels)
        required_capabilities = _normalize_required_capabilities(request.required_capabilities)

        runners = list(
            self._db.execute(
                select(Runner)
                .where(Runner.tenant_id == request.tenant_id)
                .order_by(Runner.created_at.asc(), Runner.id.asc())
            ).scalars()
        )
        if not runners:
            self._metrics.record_assignment_failure(reason_codes=(NO_RUNNERS_REGISTERED,))
            logger.info(
                "runner_control.assignment_outcome tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s success=%s reasons=%s",
                request.tenant_id,
                None,
                None,
                None,
                None,
                None,
                False,
                (NO_RUNNERS_REGISTERED,),
            )
            return RunnerAssignmentResult(
                selection=None,
                reason_codes=(NO_RUNNERS_REGISTERED,),
                evaluated_runner_count=0,
            )

        active_credential_runner_ids = self._active_credential_runner_ids(
            tenant_id=request.tenant_id,
            now=now_utc,
        )
        latest_active_leases = self._latest_active_leases(tenant_id=request.tenant_id)

        eligible: list[tuple[tuple[int, datetime, str], RunnerSelection]] = []
        failure_reasons: set[str] = set()

        for runner in runners:
            available_tasks = _resolve_available_tasks(self._db, runner)
            reasons = self._evaluate_runner(
                runner=runner,
                now=now_utc,
                request=request,
                required_labels=required_labels,
                required_capabilities=required_capabilities,
                active_credential_runner_ids=active_credential_runner_ids,
                latest_active_leases=latest_active_leases,
                available_tasks=available_tasks,
            )
            if reasons:
                failure_reasons.update(reasons)
                continue
            lease = latest_active_leases.get(runner.id)
            if available_tasks is None or lease is None:
                failure_reasons.add(RUNNER_CAPACITY_EXHAUSTED)
                continue

            selection = RunnerSelection(
                runner_id=runner.id,
                execution_site_id=runner.execution_site_id,
                available_tasks=max(0, available_tasks),
                lease_expires_at=lease.lease_expires_at,
            )
            # Prefer capacity, then freshest lease, then stable UUID tie-break.
            score = (selection.available_tasks, selection.lease_expires_at, str(selection.runner_id))
            eligible.append((score, selection))

        if not eligible:
            sorted_reasons = tuple(sorted(failure_reasons)) or (NO_RUNNERS_REGISTERED,)
            self._metrics.record_assignment_failure(reason_codes=sorted_reasons)
            logger.info(
                "runner_control.assignment_outcome tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s success=%s reasons=%s",
                request.tenant_id,
                None,
                None,
                None,
                None,
                None,
                False,
                sorted_reasons,
            )
            return RunnerAssignmentResult(
                selection=None,
                reason_codes=sorted_reasons,
                evaluated_runner_count=len(runners),
            )

        selected = max(eligible, key=lambda item: item[0])[1]
        self._metrics.record_assignment_success()
        logger.info(
            "runner_control.assignment_outcome tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s success=%s reasons=%s",
            request.tenant_id,
            selected.runner_id,
            None,
            None,
            None,
            None,
            True,
            (),
        )
        return RunnerAssignmentResult(
            selection=selected,
            reason_codes=(),
            evaluated_runner_count=len(runners),
        )

    def _evaluate_runner(
        self,
        *,
        runner: Runner,
        now: datetime,
        request: RunnerAssignmentRequest,
        required_labels: dict[str, str],
        required_capabilities: set[str],
        active_credential_runner_ids: set[UUID],
        latest_active_leases: dict[UUID, _LeaseSnapshot],
        available_tasks: int | None,
    ) -> set[str]:
        reasons: set[str] = set()

        status = _normalize_status(runner.status)
        if status == "revoked":
            reasons.add(RUNNER_REVOKED)
            return reasons
        if status == "maintenance":
            reasons.add(RUNNER_MAINTENANCE_MODE)
            return reasons
        if status not in _ONLINE_STATUSES:
            reasons.add(RUNNER_NOT_ONLINE)
            return reasons

        if request.execution_site_id is not None and runner.execution_site_id != request.execution_site_id:
            reasons.add(RUNNER_EXECUTION_SITE_MISMATCH)
            return reasons

        if runner.id not in active_credential_runner_ids:
            reasons.add(RUNNER_CREDENTIAL_NOT_ACTIVE)
            return reasons

        lease = latest_active_leases.get(runner.id)
        if lease is None or lease.lease_expires_at <= now:
            reasons.add(RUNNER_STALE_OR_OFFLINE)
            return reasons

        if _is_heartbeat_stale(
            last_seen_at=runner.last_seen_at,
            now=now,
            stale_after=self._heartbeat_stale_after,
        ):
            reasons.add(RUNNER_HEARTBEAT_STALE)
            return reasons

        if not _protocol_is_compatible(
            required_version=request.required_protocol_version,
            capacity_json=runner.capacity_json,
        ):
            reasons.add(RUNNER_PROTOCOL_INCOMPATIBLE)
            return reasons

        if not _runtime_version_is_compatible(
            required_version=request.required_runtime_version,
            runner_version=runner.version,
            capacity_json=runner.capacity_json,
        ):
            reasons.add(RUNNER_RUNTIME_VERSION_INCOMPATIBLE)
            return reasons

        if required_capabilities and not required_capabilities.issubset(_runner_capabilities(runner.capabilities_json)):
            reasons.add(RUNNER_CAPABILITY_MISMATCH)
            return reasons

        if required_labels and not _labels_match(required_labels=required_labels, runner_labels=runner.labels_json):
            reasons.add(RUNNER_LABEL_MISMATCH)
            return reasons

        if available_tasks is None or available_tasks < max(1, int(request.minimum_available_tasks)):
            reasons.add(RUNNER_CAPACITY_EXHAUSTED)

        return reasons

    def _active_credential_runner_ids(self, *, tenant_id: int, now: datetime) -> set[UUID]:
        rows = self._db.execute(
            select(RunnerCredential.runner_id)
            .where(
                RunnerCredential.tenant_id == tenant_id,
                RunnerCredential.status == "active",
                RunnerCredential.revoked_at.is_(None),
                (RunnerCredential.expires_at.is_(None) | (RunnerCredential.expires_at > now)),
            )
            .group_by(RunnerCredential.runner_id)
        ).all()
        return {row.runner_id for row in rows}

    def _latest_active_leases(self, *, tenant_id: int) -> dict[UUID, _LeaseSnapshot]:
        rows = self._db.execute(
            select(
                RunnerConnection.runner_id,
                func.max(RunnerConnection.lease_expires_at).label("lease_expires_at"),
            )
            .where(
                RunnerConnection.tenant_id == tenant_id,
                RunnerConnection.status == "active",
            )
            .group_by(RunnerConnection.runner_id)
        ).all()
        return {
            row.runner_id: _LeaseSnapshot(lease_expires_at=_ensure_utc(row.lease_expires_at))
            for row in rows
            if row.lease_expires_at is not None
        }


def _normalize_status(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    if labels is None:
        return {}
    normalized: dict[str, str] = {}
    for key, value in labels.items():
        normalized[str(key).strip()] = str(value).strip()
    return normalized


def _normalize_required_capabilities(capabilities: Sequence[str]) -> set[str]:
    return {str(capability).strip() for capability in capabilities if str(capability).strip()}


def _labels_match(*, required_labels: Mapping[str, str], runner_labels: object) -> bool:
    if not isinstance(runner_labels, Mapping):
        return False

    normalized_runner = {str(key).strip(): str(value).strip() for key, value in runner_labels.items()}
    for key, expected in required_labels.items():
        if normalized_runner.get(key) != expected:
            return False
    return True


def _runner_capabilities(raw_value: object) -> set[str]:
    if isinstance(raw_value, Mapping):
        return {str(key).strip() for key in raw_value.keys() if str(key).strip()}
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes, bytearray)):
        return {str(item).strip() for item in raw_value if str(item).strip()}
    return set()


def _protocol_is_compatible(*, required_version: str | None, capacity_json: object) -> bool:
    required = str(required_version or "").strip()
    if not required:
        return True

    reported = RUNNER_PROTOCOL_SCHEMA_VERSION
    if isinstance(capacity_json, Mapping):
        protocol_candidate = capacity_json.get("protocol_version") or capacity_json.get("schema_version")
        if protocol_candidate is not None and str(protocol_candidate).strip():
            reported = str(protocol_candidate).strip()

    return reported == required


def _runtime_version_is_compatible(*, required_version: str | None, runner_version: str | None, capacity_json: object) -> bool:
    required = str(required_version or "").strip()
    if not required:
        return True

    reported = str(runner_version or "").strip()
    if not reported and isinstance(capacity_json, Mapping):
        reported = str(capacity_json.get("version") or "").strip()
    if not reported:
        return False

    if required.startswith(">="):
        minimum = required[2:].strip()
        return _compare_version_strings(reported, minimum) >= 0
    return reported == required


def _compare_version_strings(left: str, right: str) -> int:
    left_parts = _parse_version_parts(left)
    right_parts = _parse_version_parts(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


def _parse_version_parts(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in value.strip().split("."):
        numeric = "".join(ch for ch in segment if ch.isdigit())
        if not numeric:
            break
        parts.append(int(numeric))
    return tuple(parts) if parts else (0,)


def _resolve_available_tasks(db: Session, runner: Runner) -> int | None:
    max_active_tasks = resolve_task_concurrency_limit(
        row_limit=runner.max_active_tasks,
        global_default_limit=get_local_max_active_tasks_default(),
    )
    if max_active_tasks is None:
        return sys.maxsize
    if max_active_tasks <= 0:
        return 0

    active_statuses = tuple(TaskStatus.active_task_statuses())
    active_task_count = db.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.runner_id == str(runner.id),
            Task.status.in_(active_statuses),
        )
    ).scalar_one()
    return max(0, int(max_active_tasks) - int(active_task_count))


def _is_heartbeat_stale(*, last_seen_at: datetime | None, now: datetime, stale_after: timedelta) -> bool:
    if last_seen_at is None:
        return True
    return _ensure_utc(last_seen_at) < (now - stale_after)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
