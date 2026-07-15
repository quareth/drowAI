"""Task runtime transition orchestration service.

Responsibilities:
- Execute start/pause/resume/stop operations with validated state transitions.
- Coordinate container runtime side effects with task state updates.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.config import E2E_DETERMINISTIC_MODE, E2E_RUNTIME_LOCAL_MODE
from ...models import Task, TaskStatus
from ..runner_control.assignment_service import RunnerSelection
from ..runtime_provider.contracts import (
    RuntimeCallScope,
    RuntimeOperationResult,
    RuntimePlacementMode,
    is_pending_runner_operation_result,
    is_runner_assignment_probe_result,
    normalize_runtime_call_scope,
)
from ..runtime_provider import RuntimeOperationService, provider_result_detail
from ..runtime_provider.product_policy import (
    ProductRuntimePolicyError,
    decide_runtime_placement,
    resolve_product_runtime_policy,
)
from .access_service import (
    get_owned_task_or_404,
    get_owned_task_with_engagement_or_404,
)
from .lifecycle_service import TaskLifecycleService
from .retirement_service import TaskRetirementService
from .state_service import TaskStateService
from .admission_service import AdmissionControlService

logger = logging.getLogger(__name__)

PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED = "PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED"


def deterministic_e2e_transition_targets(action: str, current_status: str) -> tuple[str, ...]:
    """Return domain-valid status targets for one offline lifecycle operation."""
    if action == "start" and current_status in {"created", "stopped", "failed", "timeout"}:
        return ("queued", "starting", "running")
    if action == "pause" and current_status == "running":
        return ("pausing", "paused")
    if action == "resume" and current_status == "paused":
        return ("resuming", "running")
    if action == "stop" and current_status in {"running", "paused", "pausing", "resuming"}:
        return ("stopping", "stopped")
    if action == "stop" and current_status in {"queued", "starting"}:
        return ("stopped",)
    return ()


def resolve_e2e_runtime_call_scope(
    runtime_call_scope: RuntimeCallScope | str,
    *,
    runtime_local_mode: bool = E2E_RUNTIME_LOCAL_MODE,
) -> RuntimeCallScope | str:
    """Grant test scope only to the explicit real-Docker browser certification process."""
    return RuntimeCallScope.TEST if runtime_local_mode else runtime_call_scope


class TaskRuntimeService:
    """Service for start/pause/resume/stop operations."""

    def __init__(self, db: Session):
        self.db = db
        self._runtime_operations = RuntimeOperationService(db)

    def _get_task_or_404(
        self,
        *,
        task_id: int,
        user_id: int,
        tenant_id: int,
        with_engagement: bool = False,
    ) -> Task:
        if with_engagement:
            return get_owned_task_with_engagement_or_404(
                db=self.db,
                task_id=task_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        return get_owned_task_or_404(
            db=self.db,
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )

    def _complete_deterministic_e2e_operation(
        self,
        *,
        task: Task,
        user_id: int,
        action: str,
    ) -> Task:
        """Persist an offline lifecycle operation without runtime-provider side effects."""
        targets = deterministic_e2e_transition_targets(action, str(task.status))
        if not targets:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot {action} task in {task.status} status",
            )
        state_service = TaskStateService(self.db)
        for target in targets:
            self._change_status_or_raise(
                state_service=state_service,
                task_id=int(task.id),
                new_status=target,
                user_id=user_id,
                reason=f"Deterministic E2E {action} scenario moved task to {target}",
                change_source="system",
                error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        self.db.refresh(task)
        return task

    @staticmethod
    def _change_status_or_raise(
        *,
        state_service: TaskStateService,
        task_id: int,
        new_status: str,
        user_id: int,
        reason: str,
        change_source: str,
        error_status: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        """Run a validated state transition or raise an HTTP error."""
        success, message, _ = state_service.change_task_status(
            task_id=task_id,
            new_status=new_status,
            user_id=user_id,
            reason=reason,
            change_source=change_source,
        )
        if not success:
            raise HTTPException(status_code=error_status, detail=message)

    async def start_task(
        self,
        task_id: int,
        user_id: int,
        *,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> Task:
        task = self._get_task_or_404(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            with_engagement=True,
        )
        self._ensure_engagement_allows_runtime_activation(task=task, action="start")
        if E2E_DETERMINISTIC_MODE:
            return self._complete_deterministic_e2e_operation(
                task=task,
                user_id=user_id,
                action="start",
            )
        normalized_scope = self._ensure_product_runtime_placement_allowed(
            task=task,
            task_id=task_id,
            action="start",
            runtime_call_scope=resolve_e2e_runtime_call_scope(runtime_call_scope),
        )
        state_service = TaskStateService(self.db)

        if task.status in {
            TaskStatus.QUEUED.value,
            TaskStatus.STARTING.value,
            TaskStatus.RUNNING.value,
        }:
            self.db.refresh(task)
            return task

        can_start, start_message = state_service.validate_operation(task_id, "start")
        if not can_start:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=start_message)

        admission_result = AdmissionControlService(self.db).admit_task(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            placement=str(getattr(task, "runtime_placement_mode", RuntimePlacementMode.LOCAL.value) or ""),
            write_task=lambda runner_selection: self._admit_task_start(
                task=task,
                task_id=task_id,
                user_id=user_id,
                state_service=state_service,
                runner_selection=runner_selection,
            ),
        )
        if not admission_result.decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": admission_result.decision.reason_code,
                    "reason_codes": list(admission_result.decision.reason_codes),
                    "message": admission_result.decision.message,
                },
            )

        try:
            await TaskLifecycleService(self.db).materialize_runtime_workspace_for_task_async(
                task=task,
                user_id=user_id,
                runtime_call_scope=normalized_scope,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Runtime workspace materialization failed: {exc}",
            ) from exc

        self._change_status_or_raise(
            state_service=state_service,
            task_id=task_id,
            new_status=TaskStatus.STARTING.value,
            user_id=user_id,
            reason="Starting task runtime via runtime provider",
            change_source="system",
        )

        result = await self._runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=user_id,
            operation="provision_task_runtime",
            call=lambda provider, request: provider.provision_task_runtime(request),
            payload=TaskLifecycleService.build_provision_payload(task),
            runtime_call_scope=normalized_scope,
        )
        if self._is_runner_assignment_probe_result(task=task, result=result):
            reason = (
                "Managed runner runtime start is deferred (runner_control); only assignment probes are supported. "
                "Use runner-control assignment APIs for validation flows."
            )
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.FAILED.value,
                user_id=user_id,
                reason=reason,
                change_source="system",
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)

        if not result.ok:
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.FAILED.value,
                user_id=user_id,
                reason=provider_result_detail("Runtime provisioning failed", result),
                change_source="system",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=provider_result_detail("Runtime provisioning failed", result),
            )

        if self._is_runner_pending_result(task=task, result=result):
            self.db.refresh(task)
            return task

        self._change_status_or_raise(
            state_service=state_service,
            task_id=task_id,
            new_status=TaskStatus.RUNNING.value,
            user_id=user_id,
            reason="Runtime provider confirmed task running",
            change_source="system",
            error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

        lifecycle_service = TaskLifecycleService(self.db)
        try:
            await lifecycle_service.materialize_task_vpn_config_async(
                task=task,
                user_id=user_id,
                db=self.db,
                runtime_call_scope=normalized_scope,
            )
        except Exception as exc:
            lifecycle_service.record_vpn_startup_failure(
                task=task,
                db=self.db,
                error_message=f"VPN materialization failed: {exc}",
                provider_name=str(result.provider or "") or None,
            )
            logger.exception(
                "Task %s VPN materialization failed after provisioning; runtime remains available",
                task_id,
            )

        self.db.refresh(task)
        return task

    @staticmethod
    def _admit_task_start(
        *,
        task: Task,
        task_id: int,
        user_id: int,
        state_service: TaskStateService,
        runner_selection: RunnerSelection | None,
    ) -> Task:
        runtime_mode = str(getattr(task, "runtime_placement_mode", "") or "").strip().lower()
        if runtime_mode == RuntimePlacementMode.RUNNER.value:
            if runner_selection is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Runner-placement admission succeeded without runner selection; "
                        "cannot queue task start."
                    ),
                )
            task.runner_id = str(runner_selection.runner_id)
            task.execution_site_id = str(runner_selection.execution_site_id)

        success, message, _ = state_service.stage_task_status_change(
            task_id=task_id,
            new_status=TaskStatus.QUEUED.value,
            user_id=user_id,
            reason="User queued task runtime start",
            change_source="manual",
        )
        if not success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)
        return task

    @staticmethod
    def _is_runner_assignment_probe_result(
        *,
        task: Task,
        result: RuntimeOperationResult,
    ) -> bool:
        return is_runner_assignment_probe_result(
            result,
            runtime_placement_mode=getattr(task, "runtime_placement_mode", None),
        )

    @staticmethod
    def _is_runner_pending_result(
        *,
        task: Task,
        result: RuntimeOperationResult,
    ) -> bool:
        return is_pending_runner_operation_result(
            result,
            runtime_placement_mode=getattr(task, "runtime_placement_mode", None),
        )

    @staticmethod
    def _is_runner_placement_task(task: Task) -> bool:
        return str(getattr(task, "runtime_placement_mode", "") or "").strip().lower() == RuntimePlacementMode.RUNNER.value

    @staticmethod
    def _normalize_runtime_call_scope_or_raise(
        runtime_call_scope: RuntimeCallScope | str,
    ) -> RuntimeCallScope:
        try:
            return normalize_runtime_call_scope(runtime_call_scope)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc

    @classmethod
    def _ensure_product_runtime_placement_allowed(
        cls,
        *,
        task: Task,
        task_id: int,
        action: str,
        runtime_call_scope: RuntimeCallScope | str,
    ) -> RuntimeCallScope:
        normalized_scope = cls._normalize_runtime_call_scope_or_raise(runtime_call_scope)
        requested_placement = getattr(task, "runtime_placement_mode", None)
        if not str(requested_placement or "").strip():
            return normalized_scope
        try:
            decision = decide_runtime_placement(
                policy=resolve_product_runtime_policy(),
                scope=normalized_scope,
                requested_placement=requested_placement,
            )
        except ProductRuntimePolicyError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "reason_code": "PRODUCT_RUNTIME_POLICY_INVALID",
                    "task_id": int(task_id),
                    "message": str(exc),
                },
            ) from exc
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED,
                    "task_id": int(task_id),
                    "message": (
                        f"Cannot {action} task {task_id} with local runtime placement "
                        "in product scope. Recreate or migrate the task to runner placement."
                    ),
                },
            )
        return normalized_scope

    @staticmethod
    def _ensure_engagement_allows_runtime_activation(*, task: Task, action: str) -> None:
        engagement = getattr(task, "engagement", None)
        engagement_status = str(getattr(engagement, "status", "")).strip().lower()
        if engagement_status == "archived":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot {action} task in archived engagement. "
                    "Restore the engagement first."
                ),
            )

    async def pause_task(
        self,
        task_id: int,
        user_id: int,
        *,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> Task | dict[str, str]:
        task = self._get_task_or_404(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            with_engagement=False,
        )
        if E2E_DETERMINISTIC_MODE:
            return self._complete_deterministic_e2e_operation(
                task=task,
                user_id=user_id,
                action="pause",
            )
        normalized_scope = self._ensure_product_runtime_placement_allowed(
            task=task,
            task_id=task_id,
            action="pause",
            runtime_call_scope=resolve_e2e_runtime_call_scope(runtime_call_scope),
        )
        state_service = TaskStateService(self.db)
        current_status = task.status

        if current_status == TaskStatus.CREATED.value:
            return {"message": "Task is not running yet. It's still in created state and hasn't been started."}
        if current_status == TaskStatus.PAUSED.value:
            return {"message": "Task is already paused"}
        if current_status != TaskStatus.RUNNING.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot pause task in {current_status} status. Only running tasks can be paused.",
            )

        self._change_status_or_raise(
            state_service=state_service,
            task_id=task_id,
            new_status=TaskStatus.PAUSING.value,
            user_id=user_id,
            reason="User requested pause",
            change_source="manual",
        )

        result = await self._runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=user_id,
            operation="pause_task_runtime",
            call=lambda provider, request: provider.pause_task_runtime(request),
            runtime_call_scope=normalized_scope,
        )
        if not result.ok:
            cont_msg = provider_result_detail("Pause failed", result)
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.RUNNING.value,
                user_id=user_id,
                reason=f"Pause failed: {cont_msg}",
                change_source="system",
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=cont_msg)

        if self._is_runner_pending_result(task=task, result=result):
            self.db.refresh(task)
            return task

        try:
            self._change_status_or_raise(
                state_service=state_service,
                task_id=task_id,
                new_status=TaskStatus.PAUSED.value,
                user_id=user_id,
                reason="Container paused successfully",
                change_source="system",
                error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except HTTPException as exc:
            message = str(exc.detail)
            await self._runtime_operations.run_authorized_task_operation(
                task=task,
                user_id=user_id,
                operation="resume_task_runtime",
                call=lambda provider, request: provider.resume_task_runtime(request),
                runtime_call_scope=normalized_scope,
            )
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.RUNNING.value,
                user_id=user_id,
                reason="Database update failed after pause",
                change_source="system",
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message)

        self.db.refresh(task)
        return task

    async def resume_task(
        self,
        task_id: int,
        user_id: int,
        *,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> Task | dict[str, str]:
        task = self._get_task_or_404(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            with_engagement=True,
        )
        self._ensure_engagement_allows_runtime_activation(task=task, action="resume")
        if E2E_DETERMINISTIC_MODE:
            return self._complete_deterministic_e2e_operation(
                task=task,
                user_id=user_id,
                action="resume",
            )
        normalized_scope = self._ensure_product_runtime_placement_allowed(
            task=task,
            task_id=task_id,
            action="resume",
            runtime_call_scope=resolve_e2e_runtime_call_scope(runtime_call_scope),
        )
        state_service = TaskStateService(self.db)

        can_resume, resume_message = state_service.validate_operation(task_id, "resume")
        if not can_resume:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=resume_message)

        if task.status == TaskStatus.RUNNING.value:
            return {"message": "Task is already running"}

        self._change_status_or_raise(
            state_service=state_service,
            task_id=task_id,
            new_status=TaskStatus.RESUMING.value,
            user_id=user_id,
            reason="User requested resume",
            change_source="manual",
        )

        result = await self._runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=user_id,
            operation="resume_task_runtime",
            call=lambda provider, request: provider.resume_task_runtime(request),
            runtime_call_scope=normalized_scope,
        )
        if not result.ok:
            cont_msg = provider_result_detail("Resume failed", result)
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.PAUSED.value,
                user_id=user_id,
                reason=f"Resume failed: {cont_msg}",
                change_source="system",
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=cont_msg)

        if self._is_runner_pending_result(task=task, result=result):
            self.db.refresh(task)
            return task

        try:
            self._change_status_or_raise(
                state_service=state_service,
                task_id=task_id,
                new_status=TaskStatus.RUNNING.value,
                user_id=user_id,
                reason="Container resumed successfully",
                change_source="system",
                error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except HTTPException as exc:
            message = str(exc.detail)
            await self._runtime_operations.run_authorized_task_operation(
                task=task,
                user_id=user_id,
                operation="pause_task_runtime",
                call=lambda provider, request: provider.pause_task_runtime(request),
                runtime_call_scope=normalized_scope,
            )
            state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.PAUSED.value,
                user_id=user_id,
                reason="Database update failed after resume",
                change_source="system",
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message)

        self.db.refresh(task)
        return task

    async def stop_task(
        self,
        task_id: int,
        user_id: int,
        *,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> Task:
        task = self._get_task_or_404(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            with_engagement=False,
        )
        if E2E_DETERMINISTIC_MODE:
            return self._complete_deterministic_e2e_operation(
                task=task,
                user_id=user_id,
                action="stop",
            )
        normalized_scope = self._ensure_product_runtime_placement_allowed(
            task=task,
            task_id=task_id,
            action="stop",
            runtime_call_scope=resolve_e2e_runtime_call_scope(runtime_call_scope),
        )
        state_service = TaskStateService(self.db)
        can_stop, stop_message = state_service.validate_operation(task_id=task_id, operation="stop")
        if not can_stop:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stop_message)

        # Only statuses that support STOPPING should transition through STOPPING.
        should_transition_through_stopping = task.status in {
            TaskStatus.RUNNING.value,
            TaskStatus.PAUSED.value,
            TaskStatus.PAUSING.value,
            TaskStatus.RESUMING.value,
        }
        if should_transition_through_stopping:
            self._change_status_or_raise(
                state_service=state_service,
                task_id=task_id,
                new_status=TaskStatus.STOPPING.value,
                user_id=user_id,
                reason="User requested runtime retirement",
                change_source="manual",
            )

        if self._is_runner_placement_task(task):
            result = await self._runtime_operations.run_authorized_task_operation(
                task=task,
                user_id=user_id,
                operation="stop_task_runtime",
                call=lambda provider, request: provider.stop_task_runtime(request),
                payload={"lifecycle_intent": "stop"},
                runtime_call_scope=normalized_scope,
            )
            if not result.ok:
                failure_reason = provider_result_detail("Stop failed", result)
                self._change_status_or_raise(
                    state_service=state_service,
                    task_id=task_id,
                    new_status=TaskStatus.FAILED.value,
                    user_id=user_id,
                    reason=failure_reason,
                    change_source="system",
                    error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=failure_reason,
                )

            if self._is_runner_pending_result(task=task, result=result):
                self.db.refresh(task)
                return task
        else:
            retirement_service = TaskRetirementService()
            try:
                try:
                    retirement_kwargs = {
                        "task_id": task_id,
                        "engagement_id": getattr(task, "engagement_id", None),
                        "user_id": user_id,
                    }
                    if normalized_scope is not RuntimeCallScope.PRODUCT_TASK:
                        retirement_kwargs["runtime_call_scope"] = normalized_scope
                    retirement_result = await retirement_service.retire_runtime(
                        **retirement_kwargs,
                    )
                except TypeError:
                    retirement_kwargs.pop("user_id", None)
                    retirement_result = await retirement_service.retire_runtime(
                        **retirement_kwargs,
                    )
            except TypeError:
                retirement_result = await retirement_service.retire_runtime(
                        task_id=task_id,
                        engagement_id=getattr(task, "engagement_id", None),
                )
            except Exception as exc:
                logger.exception("Unexpected runtime retirement exception for task %s", task_id)
                failure_reason = f"Runtime retirement raised an unexpected exception: {exc}"
                self._change_status_or_raise(
                    state_service=state_service,
                    task_id=task_id,
                    new_status=TaskStatus.FAILED.value,
                    user_id=user_id,
                    reason=failure_reason,
                    change_source="system",
                    error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Runtime retirement failed unexpectedly: {exc}",
                )

            if not retirement_result.success:
                failure_reason = f"Runtime retirement failed: {retirement_result.message}"
                self._change_status_or_raise(
                    state_service=state_service,
                    task_id=task_id,
                    new_status=TaskStatus.FAILED.value,
                    user_id=user_id,
                    reason=failure_reason,
                    change_source="system",
                    error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=retirement_result.message,
                )

        self._change_status_or_raise(
            state_service=state_service,
            task_id=task_id,
            new_status=TaskStatus.STOPPED.value,
            user_id=user_id,
            reason="Task runtime retired successfully",
            change_source="system",
            error_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.db.refresh(task)
        return task


__all__ = [
    "TaskRuntimeService",
    "deterministic_e2e_transition_targets",
    "resolve_e2e_runtime_call_scope",
]
