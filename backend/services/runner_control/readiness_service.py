"""Product readiness facade for tenant-scoped Runner Control status.

Scope:
- Converts centralized runner assignment and registry connectivity results into
  product-friendly readiness states for Management UI.

Boundaries:
- Does not duplicate runner eligibility checks; delegates assignment decisions
  to RunnerAssignmentService and site connectivity to RunnerRegistryService.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

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
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService
from backend.services.runner_control.registry_service import RunnerRegistryService

RUNNER_READINESS_READY = "ready"
RUNNER_READINESS_WAITING_FOR_RUNNER = "waiting_for_runner"
RUNNER_READINESS_REGISTERED_OFFLINE = "runner_registered_offline"
RUNNER_READINESS_INCOMPATIBLE = "runner_incompatible"
RUNNER_READINESS_CAPACITY_EXHAUSTED = "runner_capacity_exhausted"

_INCOMPATIBLE_REASON_CODES = frozenset(
    {
        RUNNER_CAPABILITY_MISMATCH,
        RUNNER_EXECUTION_SITE_MISMATCH,
        RUNNER_LABEL_MISMATCH,
        RUNNER_PROTOCOL_INCOMPATIBLE,
        RUNNER_RUNTIME_VERSION_INCOMPATIBLE,
    }
)
_OFFLINE_REASON_CODES = frozenset(
    {
        RUNNER_CREDENTIAL_NOT_ACTIVE,
        RUNNER_HEARTBEAT_STALE,
        RUNNER_MAINTENANCE_MODE,
        RUNNER_NOT_ONLINE,
        RUNNER_REVOKED,
        RUNNER_STALE_OR_OFFLINE,
    }
)


@dataclass(frozen=True, slots=True)
class RunnerReadinessResult:
    """Product readiness snapshot plus assignment-level debug reason codes."""

    status: str
    ready: bool
    reason_codes: tuple[str, ...]
    runner_site_count: int
    connected_runner_count: int
    evaluated_runner_count: int
    selected_runner_id: UUID | None
    execution_site_id: UUID | None


class RunnerReadinessService:
    """Build tenant-scoped product readiness from existing runner-control services."""

    def __init__(
        self,
        db: Session,
        *,
        assignment_service: RunnerAssignmentService | None = None,
        registry_service: RunnerRegistryService | None = None,
    ) -> None:
        self._assignment_service = assignment_service or RunnerAssignmentService(db)
        self._registry_service = registry_service or RunnerRegistryService(db)

    def get_readiness(
        self,
        request: RunnerAssignmentRequest,
        *,
        now: datetime | None = None,
    ) -> RunnerReadinessResult:
        """Return product readiness for the request tenant and optional constraints."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        connectivity_by_site = self._registry_service.list_runner_site_connectivity(
            tenant_id=request.tenant_id,
            now=observed_at,
        )
        assignment_result = self._assignment_service.select_runner(request, now=observed_at)
        connected_runner_count = sum(
            int(summary.connected_runner_count or 0) for summary in connectivity_by_site.values()
        )

        if assignment_result.selection is not None:
            return RunnerReadinessResult(
                status=RUNNER_READINESS_READY,
                ready=True,
                reason_codes=(),
                runner_site_count=len(connectivity_by_site),
                connected_runner_count=connected_runner_count,
                evaluated_runner_count=assignment_result.evaluated_runner_count,
                selected_runner_id=assignment_result.selection.runner_id,
                execution_site_id=assignment_result.selection.execution_site_id,
            )

        reason_codes = assignment_result.reason_codes
        return RunnerReadinessResult(
            status=_product_status_for_unready(
                reason_codes=reason_codes,
            ),
            ready=False,
            reason_codes=reason_codes,
            runner_site_count=len(connectivity_by_site),
            connected_runner_count=connected_runner_count,
            evaluated_runner_count=assignment_result.evaluated_runner_count,
            selected_runner_id=None,
            execution_site_id=request.execution_site_id,
        )


def _product_status_for_unready(
    *,
    reason_codes: tuple[str, ...],
) -> str:
    reason_set = set(reason_codes)
    if NO_RUNNERS_REGISTERED in reason_set:
        return RUNNER_READINESS_WAITING_FOR_RUNNER
    if reason_set & _INCOMPATIBLE_REASON_CODES:
        return RUNNER_READINESS_INCOMPATIBLE
    if reason_set == {RUNNER_CAPACITY_EXHAUSTED}:
        return RUNNER_READINESS_CAPACITY_EXHAUSTED
    if reason_set & _OFFLINE_REASON_CODES:
        return RUNNER_READINESS_REGISTERED_OFFLINE
    if RUNNER_CAPACITY_EXHAUSTED in reason_set:
        return RUNNER_READINESS_CAPACITY_EXHAUSTED
    return RUNNER_READINESS_WAITING_FOR_RUNNER


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
