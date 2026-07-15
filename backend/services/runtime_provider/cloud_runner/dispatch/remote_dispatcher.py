"""Remote operation dispatcher for cloud runner runtime jobs.

This module creates and assigns runtime jobs, writes outbound runner-control
messages, and resolves dispatch metadata. It does not poll for operation
results or own lifecycle, terminal, artifact, environment, or tool-command
operation orchestration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobCreateRequest,
    RuntimeJobService,
    RuntimeJobServiceError,
)
from runtime_shared.runner_protocol import RunnerMessageType

from ..constants import _DEFAULT_RUNTIME_IMAGE
from ..error_codes import (
    _RUNNER_ASSIGNMENT_REQUIRED,
    _RUNNER_DISPATCH_FAILED,
    _RUNNER_IDENTITY_INVALID,
)
from ..jobs.identity import RuntimeJobIdentityResolver
from ..normalization import (
    _normalize_optional_uuid,
    _normalize_tenant_id,
    _resolve_optional_text,
)
from ..payload_codec import _prepare_transport_params, _sanitize_params
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)


class CloudRunnerRemoteDispatcher:
    """Creates runtime jobs and enqueues outbound cloud-runner messages."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        runtime_job_service_factory: Callable[[Session], RuntimeJobService],
        coordination_store_factory: Callable[[Session], DBRunnerCoordinationStore],
        runtime_job_identity: RuntimeJobIdentityResolver,
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_job_service_factory = runtime_job_service_factory
        self._coordination_store_factory = coordination_store_factory
        self._runtime_job_identity = runtime_job_identity
        self._provider_name = provider_name

    def _dispatch_remote_operation(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        message_type: RunnerMessageType,
        params: Mapping[str, Any],
    ) -> RuntimeOperationResult:
        try:
            tenant_id = _normalize_tenant_id(request.tenant_id)
            runner_id = _normalize_optional_uuid(request.runner_id)
            execution_site_id = _normalize_optional_uuid(request.execution_site_id)
        except ValueError as exc:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_IDENTITY_INVALID,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        if runner_id is None:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_ASSIGNMENT_REQUIRED,
                error_message=(
                    "Runner-placement runtime operation requires an assigned runner_id."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        idempotency_key = _resolve_idempotency_key(request=request, operation_name=operation_name)
        correlation_id = _resolve_optional_text(
            request.metadata.get("correlation_id") or request.payload.get("correlation_id")
        )
        operation_id = _resolve_operation_id(request=request, operation_name=operation_name)
        runtime_image = _resolve_runtime_image(request=request, params=params)
        transport_params = dict(_prepare_transport_params(params))
        safe_params = dict(_sanitize_params(transport_params))
        delivery_policy = _resolve_delivery_policy(request)

        job_payload: dict[str, Any] = {
            "operation_name": operation_name,
            "message_type": message_type.value,
            "workspace_id": request.workspace_id,
            "operation_id": operation_id,
            "runtime_image": runtime_image,
            "params": safe_params,
            "runtime_placement_mode": request.runtime_placement_mode.value,
        }
        if execution_site_id is not None:
            job_payload["execution_site_id"] = str(execution_site_id)

        try:
            with self._session_factory() as db:
                runtime_job_service = self._runtime_job_service_factory(db)
                runtime_job = runtime_job_service.create_runtime_job(
                    RuntimeJobCreateRequest(
                        tenant_id=tenant_id,
                        task_id=request.task_id,
                        job_type=message_type.value,
                        idempotency_key=idempotency_key,
                        payload_json=job_payload,
                        correlation_id=correlation_id,
                    )
                )

                assigned = runtime_job_service.assign_runtime_job(
                    tenant_id=tenant_id,
                    runtime_job_id=runtime_job.id,
                    runner_id=runner_id,
                )
                assigned_runner_id = str(assigned.runner_id) if assigned.runner_id is not None else None
                runtime_job_id = str(runtime_job.id)
                outbound_runtime_job_id = self._runtime_job_identity._resolve_outbound_runtime_job_id(
                    db=db,
                    request=request,
                    message_type=message_type,
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                    control_runtime_job_id=runtime_job.id,
                )
                job_payload["runner_runtime_job_id"] = outbound_runtime_job_id

                outbound_params = dict(
                    transport_params
                    if message_type is RunnerMessageType.RUNTIME_VPN_CONFIG
                    else safe_params
                )
                outbound_params.setdefault("runtime_job_id", outbound_runtime_job_id)
                outbound_payload: dict[str, Any] = {
                    "runtime_job_id": outbound_runtime_job_id,
                    "operation_id": operation_id,
                    "workspace_id": request.workspace_id,
                    "runtime_image": runtime_image,
                    "operation": message_type.value,
                    "params": outbound_params,
                }
                if delivery_policy:
                    outbound_payload["delivery_policy"] = delivery_policy

                queued = self._coordination_store_factory(db).enqueue_outbound_message(
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                    message_id=f"remote-runtime-{message_type.value.replace('.', '-')}-{uuid4().hex}",
                    message_type=message_type.value,
                    payload_json=outbound_payload,
                    idempotency_key=f"remote_runtime:{message_type.value}:{runtime_job.id}",
                    runtime_job_id=runtime_job.id,
                    task_id=request.task_id,
                    correlation_id=correlation_id,
                )
                db.commit()

                return build_runtime_result(
                    request,
                    accepted=True,
                    provider=self._provider_name,
                    status=RuntimeOperationStatus.ACCEPTED,
                    metadata={
                        "protocol_domain": "remote_runtime",
                        "operation_name": operation_name,
                        "runtime_job_id": runtime_job_id,
                        "runner_runtime_job_id": outbound_runtime_job_id,
                        "runtime_job_status": str(runtime_job.status),
                        "runner_id_assigned": assigned_runner_id,
                        "control_message_id": queued.message_id,
                        "control_message_status": queued.status,
                        "control_message_type": message_type.value,
                        "operation_id": operation_id,
                        "runtime_image": runtime_image,
                    },
                )
        except RuntimeJobServiceError as exc:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=exc.error_code,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary fallback
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code=_RUNNER_DISPATCH_FAILED,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )


def _resolve_operation_id(*, request: RuntimeOperationRequest, operation_name: str) -> str:
    candidate = request.metadata.get("operation_id") or request.payload.get("operation_id")
    if candidate is not None and str(candidate).strip():
        return str(candidate).strip()
    return f"{operation_name}:{uuid4().hex}"


def _resolve_runtime_image(
    *,
    request: RuntimeOperationRequest,
    params: Mapping[str, Any],
) -> str:
    candidates = (
        request.payload.get("runtime_image"),
        request.payload.get("runtime_image_tag"),
        request.payload.get("image"),
        params.get("runtime_image"),
        params.get("runtime_image_tag"),
        params.get("image"),
        os.getenv("DROWAI_RUNTIME_IMAGE"),
        _DEFAULT_RUNTIME_IMAGE,
    )
    for candidate in candidates:
        text = _resolve_optional_text(candidate)
        if text is not None:
            return text
    return _DEFAULT_RUNTIME_IMAGE


def _resolve_delivery_policy(request: RuntimeOperationRequest) -> dict[str, Any]:
    raw_policy = request.metadata.get("delivery_policy")
    if not isinstance(raw_policy, Mapping):
        raw_policy = request.payload.get("delivery_policy")
    if not isinstance(raw_policy, Mapping):
        return {}

    policy: dict[str, Any] = {}
    max_attempts = raw_policy.get("max_attempts")
    if isinstance(max_attempts, int) and max_attempts > 0:
        policy["max_attempts"] = max_attempts
    timeout_seconds = raw_policy.get("timeout_seconds")
    if isinstance(timeout_seconds, (float, int)) and timeout_seconds > 0:
        policy["timeout_seconds"] = float(timeout_seconds)
    offline_mode = _resolve_optional_text(raw_policy.get("offline"))
    if offline_mode is not None and offline_mode.lower() in {"queue", "fail"}:
        policy["offline"] = offline_mode.lower()
    return policy


def _resolve_idempotency_key(
    *,
    request: RuntimeOperationRequest,
    operation_name: str,
) -> str:
    candidate = request.metadata.get("idempotency_key") or request.payload.get("idempotency_key")
    if candidate is not None and str(candidate).strip():
        return str(candidate).strip()
    return (
        f"remote_runtime:{operation_name}:tenant:{request.tenant_id}:task:{request.task_id}:"
        f"runner:{request.runner_id or 'none'}:{uuid4().hex}"
    )
