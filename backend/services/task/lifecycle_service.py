"""Task lifecycle orchestration service.

Responsibilities:
- Create task records with validation and normalization.
- Bootstrap provider-owned workspace/config/scope/vpn setup.
- Queue tasks and trigger background container/agent initialization.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.config import E2E_DETERMINISTIC_MODE, E2E_RUNTIME_LOCAL_MODE
from backend.models import Task, TaskCreateVPN, TaskStatus
from backend.schemas.vpn import VPNConfigCreate
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationResult,
    RuntimePlacementMode,
    is_pending_runner_operation_result,
    is_runner_assignment_probe_result,
)
from backend.services.runtime_provider.operations import RuntimeOperationService
from backend.services.runtime_provider.product_policy import (
    ProductRuntimePolicyError,
    decide_runtime_placement,
    resolve_product_runtime_policy,
    validate_product_runtime_policy,
)
from backend.services.engagement.service import EngagementService
from backend.services.tenant.context import TenantContextService, TenantRequestContext
from backend.services.runner_control.assignment_service import RunnerSelection
from .admission_service import AdmissionControlService
from .state_service import TaskStateService

logger = logging.getLogger(__name__)

E2E_FAILURE_RETRY_SCOPE = "e2e://failure-retry"
E2E_COMPLETION_SCOPE = "e2e://completion"
_VPN_LOCK_NAMESPACE = 0x44524F57
_VPN_TASK_LOCKS: dict[int, threading.Lock] = {}
_VPN_TASK_LOCKS_GUARD = threading.Lock()


def _vpn_process_lock(task_id: int) -> threading.Lock:
    """Return the process-wide lock shared by every loop/thread for a task."""
    with _VPN_TASK_LOCKS_GUARD:
        return _VPN_TASK_LOCKS.setdefault(int(task_id), threading.Lock())


@asynccontextmanager
async def _vpn_task_execution_lock(db: Session, *, task_id: int):
    """Serialize VPN materialize/reconnect sequences locally and across PostgreSQL replicas."""
    process_lock = _vpn_process_lock(task_id)
    process_acquire_task = asyncio.create_task(asyncio.to_thread(process_lock.acquire))
    try:
        await asyncio.shield(process_acquire_task)
    except asyncio.CancelledError:
        await process_acquire_task
        process_lock.release()
        raise
    advisory_connection = None
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            def acquire_advisory_lock():
                engine = getattr(bind, "engine", bind)
                connection = engine.connect()
                connection.exec_driver_sql(
                    "SELECT pg_advisory_lock(%s, %s)",
                    (_VPN_LOCK_NAMESPACE, int(task_id)),
                )
                return connection

            acquire_task = asyncio.create_task(asyncio.to_thread(acquire_advisory_lock))
            try:
                advisory_connection = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                advisory_connection = await acquire_task
                raise
        yield
    finally:
        try:
            if advisory_connection is not None:
                def release_advisory_lock() -> None:
                    try:
                        advisory_connection.exec_driver_sql(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (_VPN_LOCK_NAMESPACE, int(task_id)),
                        )
                    finally:
                        advisory_connection.close()

                await asyncio.shield(asyncio.to_thread(release_advisory_lock))
        finally:
            process_lock.release()


def deterministic_e2e_bootstrap_statuses(scope: str | None) -> tuple[str, ...]:
    """Resolve the process-gated bootstrap scenario from a UI-entered task scope."""
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope == E2E_FAILURE_RETRY_SCOPE:
        return (TaskStatus.QUEUED.value, TaskStatus.STARTING.value, TaskStatus.FAILED.value)
    if normalized_scope == E2E_COMPLETION_SCOPE:
        return (
            TaskStatus.QUEUED.value,
            TaskStatus.STARTING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.COMPLETED.value,
        )
    return (TaskStatus.QUEUED.value, TaskStatus.STARTING.value, TaskStatus.RUNNING.value)


class TaskLifecycleService:
    """Service for task creation and startup orchestration."""

    def __init__(
        self,
        db: Session,
        *,
        runtime_provider_registry: Any | None = None,
    ):
        self.db = db
        self._runtime_provider_registry = runtime_provider_registry

    def create_task(
        self,
        task_data: TaskCreateVPN,
        user_id: int,
        *,
        tenant_context: TenantRequestContext | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
        requested_runtime_placement_mode: RuntimePlacementMode | str | None = None,
    ) -> Task:
        """Create a task, bootstrap workspace, and start async initialization."""
        try:
            if not task_data.name or len(task_data.name.strip()) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Task name cannot be empty",
                )

            resolved_tenant_context = tenant_context or TenantContextService(self.db).resolve_for_user(user_id=user_id)
            existing_task = self.db.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.tenant_id == int(resolved_tenant_context.tenant_id),
                    Task.name == task_data.name.strip(),
                    Task.status.in_(TaskStatus.create_name_reservation_statuses()),
                )
            ).scalar_one_or_none()

            if existing_task:
                logger.warning(
                    "Duplicate task creation attempt: %s for user %s",
                    task_data.name,
                    user_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "A task with this name is already active. Please use a different name "
                        "or wait for the existing task to complete."
                    ),
                )

            engagement_service = EngagementService(self.db)
            resolved_engagement = engagement_service.resolve_for_task_creation(
                user_id=user_id,
                task_name=task_data.name,
                task_description=task_data.description,
                requested_engagement_id=getattr(task_data, "engagement_id", None),
                expected_tenant_id=resolved_tenant_context.tenant_id,
            )
            is_e2e_local_runtime = E2E_DETERMINISTIC_MODE or E2E_RUNTIME_LOCAL_MODE
            effective_runtime_call_scope = (
                RuntimeCallScope.TEST if is_e2e_local_runtime else runtime_call_scope
            )
            effective_requested_placement = (
                RuntimePlacementMode.LOCAL
                if is_e2e_local_runtime
                else requested_runtime_placement_mode
            )
            runtime_placement_mode = self._resolve_task_create_runtime_placement_mode(
                runtime_call_scope=effective_runtime_call_scope,
                requested_placement=effective_requested_placement,
            )

            admission_service = AdmissionControlService(self.db)

            def _write_created_task(runner_selection: RunnerSelection | None) -> Task:
                db_task = Task(
                    user_id=user_id,
                    tenant_id=resolved_tenant_context.tenant_id,
                    engagement=resolved_engagement,
                    name=task_data.name.strip(),
                    description=task_data.description.strip() if task_data.description else None,
                    scope=getattr(task_data, "scope", "network") or "network",
                    status=TaskStatus.CREATED.value,
                    runtime_placement_mode=runtime_placement_mode,
                    timeout_seconds=getattr(task_data, "timeout_seconds", None) or 3600,
                    max_retries=getattr(task_data, "max_retries", None) or 3,
                    priority=getattr(task_data, "priority", None) or 1,
                )
                if runner_selection is not None:
                    db_task.runner_id = str(runner_selection.runner_id)
                    db_task.execution_site_id = str(runner_selection.execution_site_id)
                self.db.add(db_task)
                self.db.flush()
                return db_task

            admission_result = admission_service.admit_task(
                tenant_id=int(resolved_tenant_context.tenant_id),
                user_id=int(user_id),
                placement=runtime_placement_mode,
                write_task=_write_created_task,
            )
            if not admission_result.decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "reason_code": admission_result.decision.reason_code,
                        "reason_codes": list(
                            getattr(
                                admission_result.decision,
                                "reason_codes",
                                (admission_result.decision.reason_code,),
                            )
                        ),
                        "message": admission_result.decision.message,
                    },
                )

            db_task = admission_result.task
            if db_task is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Admission succeeded but task was not persisted.",
                )
            self.db.refresh(db_task)

            task_id = db_task.id
            if not getattr(db_task, "workspace_id", None):
                db_task.workspace_id = f"task-{task_id}"
                self.db.commit()
                self.db.refresh(db_task)
            if E2E_DETERMINISTIC_MODE:
                self._complete_deterministic_e2e_bootstrap(task_id=task_id, user_id=user_id)
                try:
                    self.db.refresh(db_task)
                except Exception:
                    pass
                logger.info("Created deterministic E2E task %s for user %s", db_task.id, user_id)
                return db_task
            try:
                self.materialize_runtime_workspace_for_task(
                    task=db_task,
                    user_id=user_id,
                    task_data=task_data,
                    actor_type=RuntimeActorType.USER,
                    runtime_call_scope=effective_runtime_call_scope,
                )
            except Exception as materialize_error:
                failure_detail = str(materialize_error)
                logger.exception(
                    "Runtime workspace materialization failed for task %s",
                    task_id,
                )
                self._mark_task_failed_after_materialization_error(
                    task_id=task_id,
                    user_id=user_id,
                    reason=f"Runtime workspace materialization failed: {failure_detail}",
                )
                try:
                    self.db.refresh(db_task)
                except Exception:
                    pass
                return db_task
            self._queue_and_start_background_init(
                task_id,
                user_id,
                db_task.id,
                runtime_call_scope=effective_runtime_call_scope,
            )
            try:
                self.db.refresh(db_task)
            except Exception:
                pass

            logger.info("Created task %s for user %s", db_task.id, user_id)
            return db_task
        except HTTPException:
            raise
        except SQLAlchemyError as e:
            logger.error("Database error creating task: %s", e)
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create task",
            )
        except Exception as e:
            logger.error("Unexpected error creating task: %s", e)
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred",
            )

    def _resolve_task_create_runtime_placement_mode(
        self,
        *,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
        requested_placement: RuntimePlacementMode | str | None = None,
    ) -> str:
        """Resolve task-create placement mode through the product policy source."""
        try:
            policy = resolve_product_runtime_policy()
            decision = decide_runtime_placement(
                policy=policy,
                scope=runtime_call_scope,
                requested_placement=requested_placement,
            )
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "reason_code": decision.reason_code,
                        "message": decision.message,
                        "scope": decision.scope,
                    },
                )
            if decision.scope in {
                RuntimeCallScope.PRODUCT.value,
                RuntimeCallScope.PRODUCT_TASK.value,
            }:
                validate_product_runtime_policy(policy)
            if decision.placement is None:
                raise ProductRuntimePolicyError("Runtime placement decision did not include a placement.")
        except HTTPException:
            raise
        except (ProductRuntimePolicyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "reason_code": exc.__class__.__name__,
                    "message": f"Invalid runtime configuration for task creation: {exc}",
                },
            ) from exc
        return decision.placement

    def materialize_runtime_workspace_for_task(
        self,
        *,
        task: Task,
        user_id: int,
        task_data: TaskCreateVPN | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeOperationResult:
        """Materialize provider-owned workspace state for a task."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            raise RuntimeError(
                "materialize_runtime_workspace_for_task() cannot run inside an active "
                "event loop; use materialize_runtime_workspace_for_task_async()."
            )
        return self._run_coroutine_sync(
            self.materialize_runtime_workspace_for_task_async(
                task=task,
                user_id=user_id,
                task_data=task_data,
                actor_type=actor_type,
                runtime_call_scope=runtime_call_scope,
            )
        )

    async def materialize_runtime_workspace_for_task_async(
        self,
        *,
        task: Task,
        user_id: int,
        task_data: TaskCreateVPN | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeOperationResult:
        """Materialize provider-owned workspace state for a task asynchronously."""
        try:
            result = await self._run_task_runtime_operation(
                task=task,
                user_id=user_id,
                operation="materialize_runtime_workspace",
                actor_type=actor_type,
                payload=self._build_workspace_materialization_payload(
                    task=task,
                    user_id=user_id,
                    task_data=task_data,
                ),
                call=lambda provider, request: provider.materialize_runtime_workspace(
                    request
                ),
                runtime_call_scope=runtime_call_scope,
            )
            if not result.ok:
                raise RuntimeError(self._format_provider_failure("Workspace bootstrap failed", result))
            logger.info(
                "Materialized runtime workspace for task %s via provider %s",
                task.id,
                result.provider,
            )
            self._persist_task_vpn(task=task, task_data=task_data)
            return result
        except Exception as e:
            logger.error("Failed to materialize runtime workspace for task %s: %s", task.id, e)
            raise

    def _build_workspace_materialization_payload(
        self,
        *,
        task: Task,
        user_id: int,
        task_data: TaskCreateVPN | None,
    ) -> dict[str, Any]:
        return {
            "config_data": {
                "task_name": task.name,
                "description": task.description,
                "scope": task.scope,
                "user_id": user_id,
                "timeout_seconds": task.timeout_seconds,
                "max_retries": task.max_retries,
                "priority": task.priority,
            },
            "scope_content": str(task.scope or ""),
        }

    def _persist_task_vpn(
        self,
        *,
        task: Task,
        task_data: TaskCreateVPN | None,
    ) -> None:
        """Persist freshly-submitted VPN config to task state (no runner dispatch).

        VPN is task state, so the OVPN config submitted at task creation is
        persisted to the DB here. The actual ``runtime.vpn.config`` dispatch is
        deferred to ``materialize_task_vpn_config_async`` because runner
        placement cannot bind a VPN config until the task runtime (TASK_START)
        exists. No-op when no fresh config is provided (e.g. on task start, where
        config is already persisted from creation).
        """
        if task_data is None:
            return
        if not bool(getattr(task_data, "vpn_enabled", False)):
            return
        vpn_config = getattr(task_data, "vpn_config", None)
        if vpn_config is None:
            return

        from backend.services.vpn_service import VPNService

        ok, message = VPNService(self.db).configure_task_vpn(int(task.id), vpn_config)
        if not ok:
            raise RuntimeError(f"VPN configuration failed: {message}")
        self.db.refresh(task)

    async def materialize_task_vpn_config_async(
        self,
        *,
        task: Task,
        user_id: int,
        db: Session | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.SYSTEM,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
        only_if_configured: bool = False,
    ) -> RuntimeOperationResult | None:
        """Serialize and dispatch the existing provider VPN startup sequence."""
        async with _vpn_task_execution_lock(db or self.db, task_id=int(task.id)):
            return await self._materialize_task_vpn_config_locked(
                task=task,
                user_id=user_id,
                db=db,
                actor_type=actor_type,
                runtime_call_scope=runtime_call_scope,
                only_if_configured=only_if_configured,
            )

    async def _materialize_task_vpn_config_locked(
        self,
        *,
        task: Task,
        user_id: int,
        db: Session | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.SYSTEM,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
        only_if_configured: bool = False,
    ) -> RuntimeOperationResult | None:
        """Dispatch persisted VPN config to the runtime provider after provisioning.

        Must run after ``provision_task_runtime`` so runner placement can bind the
        ``runtime.vpn.config`` message to the assigned TASK_START runtime job
        (local placement writes the OVPN file into the workspace). Reads VPN
        state persisted by ``_persist_task_vpn``; no-op when VPN is not enabled.
        """
        target_db = db or self.db
        try:
            target_db.refresh(task)
        except Exception:
            pass
        if only_if_configured and str(getattr(task, "vpn_connection_status", "") or "") != "configured":
            return None
        if not bool(getattr(task, "vpn_enabled", False)):
            return None

        try:
            vpn_config = self._build_persisted_vpn_config(task)
        except Exception as exc:
            self.record_vpn_startup_failure(
                task=task,
                db=target_db,
                error_message=f"VPN startup skipped: {exc}",
                provider_name=None,
            )
            return None
        vpn_result = await self._run_task_runtime_operation(
            task=task,
            user_id=user_id,
            operation="materialize_vpn_config",
            actor_type=actor_type,
            payload={"vpn_config": vpn_config},
            metadata={
                "wait_for_result": True,
                "wait_timeout_seconds": 15.0,
            },
            db=target_db,
            call=lambda provider, request: provider.materialize_vpn_config(request),
            runtime_call_scope=runtime_call_scope,
        )
        if not vpn_result.ok:
            self.record_vpn_startup_failure(
                task=task,
                db=target_db,
                error_message=self._format_provider_failure("VPN materialization failed", vpn_result),
                provider_name=vpn_result.provider,
            )
            return vpn_result
        retry_result = await self._run_task_runtime_operation(
            task=task,
            user_id=user_id,
            operation="retry_vpn_connection",
            actor_type=actor_type,
            payload={"reason": "vpn_config_materialized"},
            metadata={
                "wait_for_result": True,
                "wait_timeout_seconds": 30.0,
            },
            db=target_db,
            call=lambda provider, request: provider.retry_vpn_connection(request),
            runtime_call_scope=runtime_call_scope,
        )
        if not retry_result.ok:
            self.record_vpn_startup_failure(
                task=task,
                db=target_db,
                error_message=self._format_provider_failure("VPN connection retry failed", retry_result),
                provider_name=retry_result.provider,
            )
            return retry_result
        from backend.services.vpn_service import VPNService

        await VPNService(target_db).update_vpn_status(
            task_id=int(task.id),
            status="connecting",
            ip_address=None,
            error_message=None,
        )
        logger.info(
            "Materialized VPN config and requested connection retry for task %s via provider %s",
            task.id,
            getattr(retry_result, "provider", "unknown"),
        )
        return retry_result

    async def retry_task_vpn_connection_async(
        self,
        *,
        task: Task,
        user_id: int,
        db: Session | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeOperationResult:
        """Serialize a manual reconnect with config materialization for the same task."""
        target_db = db or self.db
        async with _vpn_task_execution_lock(target_db, task_id=int(task.id)):
            return await self._run_task_runtime_operation(
                task=task,
                user_id=user_id,
                operation="retry_vpn_connection",
                actor_type=actor_type,
                metadata={"wait_for_result": True, "wait_timeout_seconds": 15.0},
                db=target_db,
                call=lambda provider, request: provider.retry_vpn_connection(request),
                runtime_call_scope=runtime_call_scope,
            )

    @staticmethod
    def record_vpn_startup_failure(
        *,
        task: Task,
        db: Session,
        error_message: str,
        provider_name: str | None,
    ) -> None:
        """Persist post-provision VPN startup failure without failing the task."""
        sanitized_message = str(error_message or "VPN startup failed.").strip()
        if len(sanitized_message) > 2048:
            sanitized_message = f"{sanitized_message[:2045]}..."
        task.vpn_connection_status = "failed"
        task.vpn_error_message = sanitized_message
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.warning(
                "VPN startup failed for task %s but status persistence failed.",
                getattr(task, "id", None),
                exc_info=True,
            )
            return
        logger.warning(
            "VPN startup failed for task %s; task will continue without VPN. provider=%s error=%s",
            getattr(task, "id", None),
            provider_name or "unknown",
            sanitized_message,
        )

    @staticmethod
    def _build_persisted_vpn_config(task: Task) -> VPNConfigCreate:
        encoded_config = getattr(task, "vpn_config_data", None)
        if not encoded_config:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task has VPN enabled but no persisted VPN config is available.",
            )
        try:
            config_data = base64.b64decode(str(encoded_config)).decode("utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task has VPN enabled but persisted VPN config cannot be decoded.",
            ) from exc
        provider = str(getattr(task, "vpn_provider", None) or "custom")
        if provider not in {"htb", "tryhackme", "custom"}:
            provider = "custom"
        return VPNConfigCreate(provider=provider, config_data=config_data)

    def _mark_task_failed_after_materialization_error(
        self,
        *,
        task_id: int,
        user_id: int,
        reason: str,
    ) -> None:
        self._mark_task_failed_with_metadata(
            task_id=task_id,
            user_id=user_id,
            reason=reason,
            failure_reason="runtime_workspace_materialization_failed",
        )

    def _mark_task_failed_with_metadata(
        self,
        *,
        task_id: int,
        user_id: int,
        reason: str,
        failure_reason: str,
        state_service: TaskStateService | None = None,
        db: Session | None = None,
    ) -> None:
        target_db = db or self.db
        target_state_service = state_service or TaskStateService(target_db)
        target_state_service.change_task_status(
            task_id=task_id,
            new_status=TaskStatus.FAILED.value,
            user_id=user_id,
            reason=reason,
            change_source="system",
        )
        task = target_db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
        if task is not None:
            task.error_message = reason
            task.failure_reason = failure_reason
            target_db.commit()

    def _queue_and_start_background_init(
        self,
        task_id: int,
        user_id: int,
        task_log_id: int,
        *,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> bool:
        try:
            state_service = TaskStateService(self.db)
            success, message, _ = state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.QUEUED.value,
                user_id=user_id,
                reason="Task created and queued for execution",
                change_source="system",
            )

            if success:
                logger.info("Task %s moved to QUEUED status", task_log_id)

                try:
                    def run_async_init() -> None:
                        try:
                            from backend.database import SessionLocal
                            from .state_service import TaskStateService

                            db_session = SessionLocal()
                            thread_state_service = TaskStateService(db_session)
                            try:
                                async def run_initialization() -> None:
                                    await self._start_unified_container_initialization(
                                        task_id,
                                        user_id,
                                        thread_state_service,
                                        db_session,
                                        runtime_call_scope=runtime_call_scope,
                                    )

                                asyncio.run(run_initialization())
                            except Exception as e:
                                logger.error("Container initialization error for task %s: %s", task_id, e)
                                try:
                                    thread_state_service.change_task_status(
                                        task_id=task_id,
                                        new_status=TaskStatus.FAILED.value,
                                        user_id=user_id,
                                        reason=f"Container initialization failed: {str(e)}",
                                        change_source="system",
                                    )
                                    db_session.commit()
                                except Exception as status_error:
                                    logger.error("Failed to update task status: %s", status_error)
                            finally:
                                try:
                                    from backend.services.tenant.rls import clear_rls_session_context

                                    clear_rls_session_context(db_session)
                                except Exception:
                                    pass
                                db_session.close()
                        except Exception as e:
                            logger.error("Background thread setup failed for task %s: %s", task_id, e)

                    init_thread = threading.Thread(target=run_async_init, daemon=True)
                    init_thread.start()
                    logger.info("Started background container initialization for task %s", task_log_id)
                    return True
                except Exception as init_error:
                    logger.exception(
                        "Failed to schedule container initialization for task %s",
                        task_log_id,
                    )
                    self._mark_task_failed_with_metadata(
                        task_id=task_id,
                        user_id=user_id,
                        reason=f"Container initialization scheduling failed: {str(init_error)}",
                        failure_reason="runtime_initialization_schedule_failed",
                    )
                    return False

            logger.warning("Failed to auto-queue task %s: %s", task_log_id, message)
            self._mark_task_failed_with_metadata(
                task_id=task_id,
                user_id=user_id,
                reason=f"Task queueing failed: {message}",
                failure_reason="task_queueing_failed",
            )
            return False
        except Exception as e:
            logger.exception("Failed to initialize task %s", task_log_id)
            try:
                self._mark_task_failed_with_metadata(
                    task_id=task_id,
                    user_id=user_id,
                    reason=f"Task initialization scheduling failed: {str(e)}",
                    failure_reason="task_queueing_exception",
                )
            except Exception:
                logger.exception("Failed to mark task %s failed after queueing exception", task_log_id)
            return False

    def _complete_deterministic_e2e_bootstrap(self, *, task_id: int, user_id: int) -> None:
        """Move deterministic E2E tasks to running without runtime side effects."""
        state_service = TaskStateService(self.db)
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            raise RuntimeError(f"Deterministic E2E task {task_id} disappeared during bootstrap")
        for next_status in deterministic_e2e_bootstrap_statuses(task.scope):
            success, message, _ = state_service.change_task_status(
                task_id=task_id,
                new_status=next_status,
                user_id=user_id,
                reason=f"Deterministic E2E lifecycle scenario moved task to {next_status}",
                change_source="system",
                metadata={"deterministic_e2e": True},
            )
            if not success:
                raise RuntimeError(
                    f"Deterministic E2E task status transition to {next_status} failed: {message}"
                )

    async def _start_unified_container_initialization(
        self,
        task_id: int,
        user_id: int,
        state_service: TaskStateService,
        db: Session,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> bool:
        """Initialize runtime container using task execution runtime provider."""
        try:
            from backend.services.tenant.rls import set_task_worker_rls_context

            set_task_worker_rls_context(
                db,
                task_id=int(task_id),
                actor_type="system",
                user_id=int(user_id),
            )

            success, message, _ = state_service.change_task_status(
                task_id=task_id,
                new_status=TaskStatus.STARTING.value,
                user_id=user_id,
                reason="Starting task runtime via runtime provider",
                change_source="system",
            )

            if not success:
                logger.error("Failed to move task %s to STARTING: %s", task_id, message)
                return False

            logger.info("Task %s moved to STARTING status - beginning provider runtime provisioning", task_id)

            try:
                task = db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
                if task is None:
                    raise RuntimeError("Task not found in database")
                provision_result = await self._run_task_runtime_operation(
                    task=task,
                    user_id=user_id,
                    operation="provision_task_runtime",
                    actor_type=RuntimeActorType.SYSTEM,
                    payload=self.build_provision_payload(task),
                    db=db,
                    call=lambda provider, request: provider.provision_task_runtime(request),
                    runtime_call_scope=runtime_call_scope,
                )

                if self._is_runner_assignment_probe_result(
                    task=task,
                    provision_result=provision_result,
                ):
                    reason = (
                        "Managed runner provisioning is deferred (runner_control); provider created only a "
                        "control-plane assignment probe. Remote runtime start is not available yet."
                    )
                    self._mark_task_failed_with_metadata(
                        task_id=task_id,
                        user_id=user_id,
                        reason=reason,
                        failure_reason="RUNNER_REMOTE_OPERATION_DEFERRED",
                        state_service=state_service,
                        db=db,
                    )
                    logger.info(
                        "Task %s kept out of RUNNING because managed runner provisioning is deferred (runner_control)",
                        task_id,
                    )
                    return False

                if provision_result.ok:
                    if self._is_runner_pending_result(
                        task=task,
                        provision_result=provision_result,
                    ):
                        logger.info(
                            "Task %s runtime provisioning accepted by runner control; awaiting runtime.started event before VPN materialization",
                            task_id,
                        )
                        db.commit()
                        return True
                    try:
                        success, message, _ = state_service.change_task_status(
                            task_id=task_id,
                            new_status=TaskStatus.RUNNING.value,
                            user_id=user_id,
                            reason=(
                                "Runtime provisioned successfully "
                                f"via provider `{provision_result.provider}`"
                            ),
                            change_source="system",
                        )
                        if not success:
                            raise RuntimeError(message)
                        logger.info(
                            "Task %s runtime started successfully, moved to RUNNING",
                            task_id,
                        )
                        db.commit()
                    except Exception as status_error:
                        logger.error(
                            "Failed finalizing startup status for task %s: %s",
                            task_id,
                            status_error,
                        )
                        state_service.change_task_status(
                            task_id=task_id,
                            new_status=TaskStatus.FAILED.value,
                            user_id=user_id,
                            reason=f"Startup finalization failed: {str(status_error)}",
                            change_source="system",
                        )
                        db.commit()
                        return False

                    try:
                        await self.materialize_task_vpn_config_async(
                            task=task,
                            user_id=user_id,
                            db=db,
                        )
                    except Exception as vpn_error:
                        self.record_vpn_startup_failure(
                            task=task,
                            db=db,
                            error_message=f"VPN materialization failed: {vpn_error}",
                            provider_name=str(provision_result.provider or "") or None,
                        )
                        logger.exception(
                            "Task %s VPN materialization failed after provisioning; runtime remains available",
                            task_id,
                        )
                    return True

                error_msg = self._format_provider_failure(
                    "Runtime provisioning failed",
                    provision_result,
                )
                self._mark_task_failed_with_metadata(
                    task_id=task_id,
                    user_id=user_id,
                    reason=error_msg,
                    failure_reason=str(provision_result.error_code or "runtime_provisioning_failed"),
                    state_service=state_service,
                    db=db,
                )
                logger.error("Task %s runtime provisioning failed, moved to FAILED", task_id)
                return False
            except Exception as e:
                logger.error("Runtime provider provisioning failed for task %s: %s", task_id, e)
                self._mark_task_failed_with_metadata(
                    task_id=task_id,
                    user_id=user_id,
                    reason=f"Runtime initialization error: {str(e)}",
                    failure_reason="runtime_provisioning_exception",
                    state_service=state_service,
                    db=db,
                )
                return False
        except Exception as e:
            logger.error("Container initialization failed for task %s: %s", task_id, e)
            try:
                self._mark_task_failed_with_metadata(
                    task_id=task_id,
                    user_id=user_id,
                    reason=f"Initialization error: {str(e)}",
                    failure_reason="runtime_initialization_exception",
                    state_service=state_service,
                    db=db,
                )
            except Exception as commit_error:
                logger.error("Failed to update task status after error: %s", commit_error)
            return False

    @staticmethod
    def _is_runner_assignment_probe_result(
        *,
        task: Task,
        provision_result: RuntimeOperationResult,
    ) -> bool:
        return is_runner_assignment_probe_result(
            provision_result,
            runtime_placement_mode=getattr(task, "runtime_placement_mode", None),
        )

    @staticmethod
    def _is_runner_pending_result(
        *,
        task: Task,
        provision_result: RuntimeOperationResult,
    ) -> bool:
        return is_pending_runner_operation_result(
            provision_result,
            runtime_placement_mode=getattr(task, "runtime_placement_mode", None),
        )

    async def _run_task_runtime_operation(
        self,
        *,
        task: Task,
        user_id: int | None,
        operation: str,
        actor_type: RuntimeActorType,
        call: Callable[[Any, Any], Awaitable[RuntimeOperationResult]],
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        db: Session | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeOperationResult:
        """Dispatch a task runtime operation through RuntimeOperationService."""
        if self._runtime_provider_registry is None:
            runtime_operations = RuntimeOperationService(db or self.db)
        else:
            runtime_operations = RuntimeOperationService(
                db or self.db,
                registry=self._runtime_provider_registry,
            )
        return await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=int(user_id) if user_id is not None else None,
            actor_type=actor_type,
            actor_id=actor_type.value if actor_type is not RuntimeActorType.USER else None,
            operation=operation,
            call=call,
            payload=payload,
            metadata=metadata,
            runtime_call_scope=runtime_call_scope,
        )

    @staticmethod
    def build_provision_payload(task: Task, *, target: str = "127.0.0.1") -> dict[str, Any]:
        """Build the provision/TASK_START payload carrying task runtime intent.

        VPN intent rides the provision contract as a boolean only. The OVPN
        config itself is materialized into the runtime workspace via
        ``materialize_vpn_config`` so the secret never travels in provision
        params (and is never persisted into runtime job records).
        """
        return {
            "target": target,
            "vpn_enabled": bool(getattr(task, "vpn_enabled", False)),
        }

    @staticmethod
    def _run_coroutine_sync(coro: Awaitable[RuntimeOperationResult]) -> RuntimeOperationResult:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        if loop.is_running():
            raise RuntimeError(
                "Cannot synchronously run runtime provider operation inside an active event loop."
            )
        return loop.run_until_complete(coro)

    @staticmethod
    def _format_provider_failure(prefix: str, result: RuntimeOperationResult) -> str:
        detail_parts = [prefix]
        detail_parts.append(f"provider={result.provider}")
        detail_parts.append(f"status={result.status.value}")
        if result.error_code:
            detail_parts.append(f"code={result.error_code}")
        if result.error_message:
            detail_parts.append(f"error={result.error_message}")
        return " | ".join(detail_parts)
