"""Task admission orchestration service for quota and capacity gates.

Responsibilities:
- Enforce Gate A quota checks (user, then tenant) using Postgres-backed counts.
- Enforce Gate B runner-capacity admission for runner placement.
- Hold one transaction/admission boundary and advisory lock during count+admit.

Boundaries:
- Does not create/transition tasks directly; callers provide a write callback.
- Does not own runner eligibility/capacity math; delegates to RunnerAssignmentService.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.config.feature_flags import (
    get_local_max_active_tasks_default,
    get_task_max_concurrent_per_tenant_default,
    get_task_max_concurrent_per_user_default,
    resolve_task_concurrency_limit,
)
from backend.domain.task_admission import (
    GLOBAL_CAPACITY_EXHAUSTED,
    NO_RUNNERS_REGISTERED,
    RUNNER_CAPACITY_EXHAUSTED,
    TENANT_QUOTA_EXCEEDED,
    USER_QUOTA_EXCEEDED,
)
from backend.models import Tenant, User
from backend.services.runner_control.assignment_service import (
    RunnerAssignmentRequest,
    RunnerAssignmentResult,
    RunnerAssignmentService,
    RunnerSelection,
)
from backend.services.runtime_provider.contracts import RuntimePlacementMode

from .quota_service import TaskQuotaService

_ADVISORY_LOCK_NAMESPACE_TASK_ADMISSION = 1729
_ADVISORY_LOCK_KEY_GLOBAL_CAPACITY = 0

T = TypeVar("T")


def _runner_assignment_message(*, reason_code: str) -> str:
    if reason_code == NO_RUNNERS_REGISTERED:
        return "No Runner is registered for this tenant."
    if reason_code == RUNNER_CAPACITY_EXHAUSTED:
        return "Runner active-task capacity is exhausted for this tenant."
    return f"Runner placement admission failed: {reason_code}."


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Structured task-admission decision payload."""

    allowed: bool
    reason_code: str | None = None
    reason_codes: tuple[str, ...] = ()
    message: str | None = None

    def __post_init__(self) -> None:
        normalized_reason_codes = tuple(
            str(reason_code).strip()
            for reason_code in self.reason_codes
            if str(reason_code or "").strip()
        )
        normalized_reason_code = str(self.reason_code).strip() if self.reason_code else None
        if not normalized_reason_codes and normalized_reason_code:
            normalized_reason_codes = (normalized_reason_code,)
        if normalized_reason_code is None and normalized_reason_codes:
            normalized_reason_code = normalized_reason_codes[0]
        object.__setattr__(self, "reason_code", normalized_reason_code)
        object.__setattr__(self, "reason_codes", normalized_reason_codes)


@dataclass(frozen=True, slots=True)
class AdmissionResult(Generic[T]):
    """Admission decision + optional task payload from write callback."""

    decision: AdmissionDecision
    task: T | None = None


@dataclass(frozen=True, slots=True)
class _TenantQuotaLimits:
    tenant_limit: int | None
    per_user_default_limit: int | None


class AdmissionControlService:
    """Admit a task against user quota, tenant quota, and runner capacity.

    The admission transaction boundary includes:
    1) advisory lock acquisition (PostgreSQL only, no-op for sqlite/tests) —
       a global-capacity lock (local placement with a configured ceiling) is
       always taken before the tenant lock to keep a deadlock-free order,
    2) user and tenant quota checks from task counts,
    3) Gate B physical capacity: runner selection for `runner` placement, or a
       deployment-wide active-task ceiling for `local` placement,
    4) callback write/flush for first counted task state.
    """

    def __init__(
        self,
        db: Session,
        *,
        quota_service: TaskQuotaService | None = None,
        assignment_service_factory: Callable[[Session], RunnerAssignmentService] | None = None,
    ) -> None:
        self._db = db
        self._quota_service = quota_service or TaskQuotaService(db)
        self._assignment_service_factory = assignment_service_factory or RunnerAssignmentService

    def admit_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        placement: str,
        write_task: Callable[[RunnerSelection | None], T],
    ) -> AdmissionResult[T]:
        """Admit a task and commit exactly once when allowed.

        Args:
            tenant_id: Tenant identity for admission scope.
            user_id: User identity for user-level quota scope.
            placement: Runtime placement (`local` or `runner`).
            write_task: Callback that uses this same Session to insert/transition
                a task into its first counted status and calls `flush()`.

        Returns:
            Structured admission result with optional task payload.

        Raises:
            Any exception raised by `write_task`.
        """
        normalized_tenant_id = int(tenant_id)
        normalized_user_id = int(user_id)
        normalized_placement = str(placement or "").strip().lower()
        is_local_placement = normalized_placement == RuntimePlacementMode.LOCAL.value
        global_capacity_limit = self._resolve_global_capacity_limit() if is_local_placement else None

        try:
            # Lock order is global-before-tenant; runner/unlimited paths take only
            # the tenant lock and never hold the global lock, so no cycle forms.
            if global_capacity_limit is not None:
                self._acquire_global_capacity_lock()
            self._acquire_advisory_xact_lock(tenant_id=normalized_tenant_id)

            user_decision = self._evaluate_user_quota(
                tenant_id=normalized_tenant_id,
                user_id=normalized_user_id,
            )
            if not user_decision.allowed:
                self._db.rollback()
                return AdmissionResult(decision=user_decision)

            tenant_decision = self._evaluate_tenant_quota(tenant_id=normalized_tenant_id)
            if not tenant_decision.allowed:
                self._db.rollback()
                return AdmissionResult(decision=tenant_decision)

            runner_selection: RunnerSelection | None = None
            if normalized_placement == RuntimePlacementMode.RUNNER.value:
                runner_result = self._select_runner_for_admission(tenant_id=normalized_tenant_id)
                if runner_result.selection is None:
                    reason_codes = runner_result.reason_codes or (NO_RUNNERS_REGISTERED,)
                    reason_code = reason_codes[0]
                    self._db.rollback()
                    return AdmissionResult(
                        decision=AdmissionDecision(
                            allowed=False,
                            reason_code=reason_code,
                            reason_codes=reason_codes,
                            message=_runner_assignment_message(reason_code=reason_code),
                        )
                    )
                runner_selection = runner_result.selection
            elif global_capacity_limit is not None:
                global_decision = self._evaluate_global_capacity(limit=global_capacity_limit)
                if not global_decision.allowed:
                    self._db.rollback()
                    return AdmissionResult(decision=global_decision)

            task = write_task(runner_selection)
            self._db.flush()
            self._db.commit()
            return AdmissionResult(decision=AdmissionDecision(allowed=True), task=task)
        except Exception:
            self._db.rollback()
            raise

    def _evaluate_user_quota(self, *, tenant_id: int, user_id: int) -> AdmissionDecision:
        user_limit = self._resolve_user_limit(tenant_id=tenant_id, user_id=user_id)
        if user_limit is None:
            return AdmissionDecision(allowed=True)

        active_user_tasks = self._quota_service.count_active_for_user(tenant_id=tenant_id, user_id=user_id)
        if active_user_tasks < user_limit:
            return AdmissionDecision(allowed=True)
        return AdmissionDecision(
            allowed=False,
            reason_code=USER_QUOTA_EXCEEDED,
            message=f"User active-task quota exceeded ({active_user_tasks}/{user_limit}).",
        )

    def _evaluate_tenant_quota(self, *, tenant_id: int) -> AdmissionDecision:
        tenant_limit = self._resolve_tenant_limit(tenant_id=tenant_id)
        if tenant_limit is None:
            return AdmissionDecision(allowed=True)

        active_tenant_tasks = self._quota_service.count_active_for_tenant(tenant_id=tenant_id)
        if active_tenant_tasks < tenant_limit:
            return AdmissionDecision(allowed=True)
        return AdmissionDecision(
            allowed=False,
            reason_code=TENANT_QUOTA_EXCEEDED,
            message=f"Tenant active-task quota exceeded ({active_tenant_tasks}/{tenant_limit}).",
        )

    def _evaluate_global_capacity(self, *, limit: int) -> AdmissionDecision:
        active_global_tasks = self._quota_service.count_active_global()
        if active_global_tasks < limit:
            return AdmissionDecision(allowed=True)
        return AdmissionDecision(
            allowed=False,
            reason_code=GLOBAL_CAPACITY_EXHAUSTED,
            message=f"Global active-task capacity exceeded ({active_global_tasks}/{limit}).",
        )

    def _resolve_global_capacity_limit(self) -> int | None:
        return resolve_task_concurrency_limit(
            row_limit=None,
            global_default_limit=get_local_max_active_tasks_default(),
        )

    def _resolve_user_limit(self, *, tenant_id: int, user_id: int) -> int | None:
        tenant_limits = self._fetch_tenant_limits(tenant_id=tenant_id)
        user_row_limit = self._db.execute(select(User.max_concurrent_tasks).where(User.id == user_id)).scalar_one_or_none()
        return resolve_task_concurrency_limit(
            row_limit=user_row_limit,
            tenant_default_limit=tenant_limits.per_user_default_limit,
            global_default_limit=get_task_max_concurrent_per_user_default(),
        )

    def _resolve_tenant_limit(self, *, tenant_id: int) -> int | None:
        tenant_limits = self._fetch_tenant_limits(tenant_id=tenant_id)
        return resolve_task_concurrency_limit(
            row_limit=tenant_limits.tenant_limit,
            global_default_limit=get_task_max_concurrent_per_tenant_default(),
        )

    def _fetch_tenant_limits(self, *, tenant_id: int) -> _TenantQuotaLimits:
        row = self._db.execute(
            select(
                Tenant.max_concurrent_tasks,
                Tenant.max_concurrent_tasks_per_user,
            ).where(Tenant.id == tenant_id)
        ).one_or_none()
        if row is None:
            return _TenantQuotaLimits(tenant_limit=None, per_user_default_limit=None)
        return _TenantQuotaLimits(
            tenant_limit=row.max_concurrent_tasks,
            per_user_default_limit=row.max_concurrent_tasks_per_user,
        )

    def _select_runner_for_admission(self, *, tenant_id: int) -> RunnerAssignmentResult:
        assignment_service = self._assignment_service_factory(self._db)
        return assignment_service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_id))

    def _acquire_advisory_xact_lock(self, *, tenant_id: int) -> None:
        if not self._is_postgres_bind():
            return
        self._db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace_key, :tenant_key)"),
            {
                "namespace_key": _ADVISORY_LOCK_NAMESPACE_TASK_ADMISSION,
                "tenant_key": _to_postgres_int4(tenant_id),
            },
        )

    def _acquire_global_capacity_lock(self) -> None:
        if not self._is_postgres_bind():
            return
        self._db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace_key, :global_key)"),
            {
                "namespace_key": _ADVISORY_LOCK_NAMESPACE_TASK_ADMISSION,
                "global_key": _ADVISORY_LOCK_KEY_GLOBAL_CAPACITY,
            },
        )

    def _is_postgres_bind(self) -> bool:
        bind = self._db.get_bind()
        return bind is not None and bind.dialect.name == "postgresql"


def _to_postgres_int4(value: int) -> int:
    """Normalize Python int to PostgreSQL signed int4 range for advisory-lock keys."""
    normalized = int(value) & 0xFFFFFFFF
    return normalized if normalized < (1 << 31) else normalized - (1 << 32)
