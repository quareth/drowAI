"""Runtime-provider operation helpers for management-plane callers.

Responsibilities:
- Resolve authorized task runtime context from API/user or internal callers.
- Build normalized provider request envelopes for common task runtime operations.
- Keep routers and task services from selecting local Docker/runtime internals.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.models import Task

from .context import RuntimeProviderContextResolver, RuntimeRequestContext
from .contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    normalize_runtime_call_scope,
)
from .provider import TaskExecutionRuntimeProvider
from .product_policy import (
    ProductRuntimePolicyError,
    decide_runtime_placement,
    resolve_product_runtime_policy,
)
from .registry import RuntimeProviderRegistry, UnsupportedRuntimePlacementError


ProviderCall = Callable[[TaskExecutionRuntimeProvider, RuntimeOperationRequest], Awaitable[RuntimeOperationResult]]
logger = logging.getLogger(__name__)


class RuntimeOperationService:
    """Resolve task runtime identity and dispatch provider operations."""

    def __init__(
        self,
        db: Session,
        *,
        registry: RuntimeProviderRegistry | None = None,
    ) -> None:
        self.db = db
        self._registry = registry or RuntimeProviderRegistry()
        self._resolver = RuntimeProviderContextResolver(db)

    def context_for_user_task(
        self,
        *,
        task_id: int,
        user_id: int,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Resolve authorized runtime context for a user-owned task."""
        return self._resolver.resolve_user_task_context(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            runtime_call_scope=runtime_call_scope,
        )

    def context_for_internal_task(
        self,
        *,
        task_id: int,
        actor_type: RuntimeActorType,
        actor_id: str | int | None = None,
        user_id: int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Resolve runtime context for internal system/agent operations."""
        return self._resolver.resolve_internal_task_context(
            task_id=task_id,
            actor_type=actor_type,
            actor_id=actor_id,
            user_id=user_id,
            runtime_call_scope=runtime_call_scope,
        )

    @staticmethod
    def context_from_authorized_task(
        *,
        task: Task,
        user_id: int | None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        actor_id: str | int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Build context from a task already authorized by caller."""
        return RuntimeProviderContextResolver.context_from_task(
            task=task,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            runtime_call_scope=runtime_call_scope,
        )

    def provider_for_context(
        self,
        context: RuntimeRequestContext,
        runtime_call_scope: RuntimeCallScope | str | None = None,
    ) -> TaskExecutionRuntimeProvider:
        """Return provider for the resolved runtime context."""
        normalized_scope = self._normalize_runtime_call_scope_or_403(
            context=context,
            runtime_call_scope=runtime_call_scope,
        )
        self._ensure_runtime_placement_allowed_or_409(
            context=context,
            runtime_call_scope=normalized_scope,
        )
        try:
            return self._registry.get_provider(runtime_placement_mode=context.runtime_placement_mode)
        except UnsupportedRuntimePlacementError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "UNSUPPORTED_RUNTIME_PLACEMENT",
                    "task_id": _context_task_id(context),
                    "placement": str(context.runtime_placement_mode),
                    "message": (
                        "Unsupported runtime placement mode: "
                        f"{context.runtime_placement_mode}"
                    ),
                },
            ) from exc

    @staticmethod
    def _normalize_runtime_call_scope_or_403(
        *,
        context: RuntimeRequestContext,
        runtime_call_scope: RuntimeCallScope | str | None = None,
    ) -> RuntimeCallScope:
        """Normalize runtime call scope before provider selection."""
        try:
            return normalize_runtime_call_scope(
                runtime_call_scope
                or getattr(context, "runtime_call_scope", RuntimeCallScope.PRODUCT_TASK)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc

    @staticmethod
    def _ensure_runtime_placement_allowed_or_409(
        *,
        context: RuntimeRequestContext,
        runtime_call_scope: RuntimeCallScope,
    ) -> RuntimePlacementMode:
        """Apply product runtime policy before provider lookup or request build."""
        try:
            policy = resolve_product_runtime_policy()
        except ProductRuntimePolicyError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "reason_code": "PRODUCT_RUNTIME_POLICY_INVALID",
                    "task_id": _context_task_id(context),
                    "message": str(exc),
                },
            ) from exc

        decision = decide_runtime_placement(
            policy=policy,
            scope=runtime_call_scope,
            requested_placement=getattr(context, "runtime_placement_mode", None),
        )
        if not decision.allowed or decision.placement is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": decision.reason_code or "RUNTIME_PLACEMENT_REJECTED",
                    "task_id": _context_task_id(context),
                    "placement": str(getattr(context, "runtime_placement_mode", "")),
                    "scope": decision.scope,
                    "message": decision.message,
                },
            )
        return RuntimePlacementMode(decision.placement)

    @staticmethod
    def build_request(
        *,
        context: RuntimeRequestContext,
        operation: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_call_scope: RuntimeCallScope | str | None = None,
    ) -> RuntimeOperationRequest:
        """Build a provider request from resolved runtime context."""
        normalized_scope = RuntimeOperationService._normalize_runtime_call_scope_or_403(
            context=context,
            runtime_call_scope=runtime_call_scope,
        )
        runtime_placement_mode = RuntimeOperationService._ensure_runtime_placement_allowed_or_409(
            context=context,
            runtime_call_scope=normalized_scope,
        )
        return RuntimeOperationRequest(
            tenant_id=context.tenant_id,
            task_id=context.task_id,
            actor_type=context.actor_type,
            actor_id=context.actor_id,
            user_id=context.user_id,
            runtime_placement_mode=runtime_placement_mode,
            workspace_id=context.workspace_id,
            runner_id=context.runner_id,
            execution_site_id=context.execution_site_id,
            operation=operation,
            runtime_call_scope=normalized_scope,
            payload=payload or {},
            metadata=metadata or {},
        )

    async def run_for_context(
        self,
        *,
        context: RuntimeRequestContext,
        operation: str,
        call: ProviderCall,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_call_scope: RuntimeCallScope | str | None = None,
    ) -> RuntimeOperationResult:
        """Dispatch a provider operation for a pre-resolved context."""
        normalized_scope = self._normalize_runtime_call_scope_or_403(
            context=context,
            runtime_call_scope=runtime_call_scope,
        )
        provider = self.provider_for_context(
            context,
            runtime_call_scope=normalized_scope,
        )
        request = self.build_request(
            context=context,
            operation=operation,
            payload=payload,
            metadata=metadata,
            runtime_call_scope=normalized_scope,
        )
        logger.info(
            "runtime_provider.operation.start tenant_id=%s task_id=%s operation=%s placement=%s provider=%s actor_type=%s runner_id=%s",
            request.tenant_id,
            request.task_id,
            request.operation,
            request.runtime_placement_mode.value,
            provider.provider_name,
            request.actor_type.value,
            request.runner_id,
        )
        try:
            result = await call(provider, request)
        except Exception:
            logger.exception(
                "runtime_provider.operation.exception tenant_id=%s task_id=%s operation=%s placement=%s provider=%s runner_id=%s",
                request.tenant_id,
                request.task_id,
                request.operation,
                request.runtime_placement_mode.value,
                provider.provider_name,
                request.runner_id,
            )
            raise
        log_method = logger.info if result.ok else logger.warning
        log_method(
            "runtime_provider.operation.end tenant_id=%s task_id=%s operation=%s placement=%s provider=%s status=%s accepted=%s error_code=%s runner_id=%s runtime_job_id=%s",
            result.tenant_id,
            result.task_id,
            result.operation,
            result.runtime_placement_mode.value,
            result.provider,
            result.status.value,
            result.accepted,
            result.error_code,
            result.runner_id,
            result.metadata.get("runtime_job_id") if isinstance(result.metadata, dict) else None,
        )
        return result

    async def run_authorized_task_operation(
        self,
        *,
        task: Task,
        user_id: int,
        operation: str,
        call: ProviderCall,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_call_scope: RuntimeCallScope | str | None = None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        actor_id: str | int | None = None,
    ) -> RuntimeOperationResult:
        """Dispatch a provider operation for a task already authorized by caller."""
        context = self.context_from_authorized_task(
            task=task,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            runtime_call_scope=(
                runtime_call_scope
                if runtime_call_scope is not None
                else RuntimeCallScope.PRODUCT_TASK
            ),
        )
        return await self.run_for_context(
            context=context,
            operation=operation,
            call=call,
            payload=payload,
            metadata=metadata,
            runtime_call_scope=runtime_call_scope,
        )

    async def run_user_task_operation(
        self,
        *,
        task_id: int,
        user_id: int,
        tenant_id: int,
        operation: str,
        call: ProviderCall,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_call_scope: RuntimeCallScope | str | None = None,
    ) -> RuntimeOperationResult:
        """Resolve user task context and dispatch provider operation."""
        context = self.context_for_user_task(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            runtime_call_scope=(
                runtime_call_scope
                if runtime_call_scope is not None
                else RuntimeCallScope.PRODUCT_TASK
            ),
        )
        return await self.run_for_context(
            context=context,
            operation=operation,
            call=call,
            payload=payload,
            metadata=metadata,
            runtime_call_scope=runtime_call_scope,
        )


def provider_result_detail(prefix: str, result: RuntimeOperationResult) -> str:
    """Return stable human-readable provider failure details."""
    parts = [prefix, f"provider={result.provider}", f"status={result.status.value}"]
    if result.error_code:
        parts.append(f"code={result.error_code}")
    if result.error_message:
        parts.append(f"error={result.error_message}")
    return " | ".join(parts)


def provider_result_success(result: RuntimeOperationResult) -> bool:
    """Return true when provider result represents an accepted non-failed operation."""
    return result.ok and result.status not in {
        RuntimeOperationStatus.FAILED,
        RuntimeOperationStatus.REJECTED,
    }


def _context_task_id(context: RuntimeRequestContext) -> int | str:
    """Return task id for error details without masking the original rejection."""
    task_id = getattr(context, "task_id", "")
    try:
        return int(task_id)
    except (TypeError, ValueError):
        return str(task_id)


__all__ = [
    "RuntimeOperationService",
    "provider_result_detail",
    "provider_result_success",
]
