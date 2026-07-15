"""Task VPN management routes.

Responsibilities:
- Expose VPN configure/upload/retry/status endpoints for tasks.
- Validate ownership and delegate VPN operations to VPN-related services.

Runtime path contract: manual VPN retry and post-start recovery use the same connect
shell contract `UnifiedDockerService.build_vpn_connect_exec_shell()` (resolver + explicit `bash`).
VPN paths are image-internal (``/opt/drowai/runtime/vpn/vpn-manager.sh``) across policies;
legacy ``/agent_src`` runtime paths are retired from active startup/retry/recovery behavior.
HTTP responses and error codes for retry are unchanged.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
import logging
from typing import Any

from ...auth import get_current_user
from ...database import get_db
from ...domain.task_lifecycle import TaskStatus
from ...models import User, VPNConfigCreate, VPNStatusResponse
from ...services.runtime_provider import RuntimeActorType, RuntimeOperationService, provider_result_detail
from ...services.task.lifecycle_service import TaskLifecycleService
from ...services.tenant.authorization import ACTION_TASK_CONTROL, ACTION_TASK_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.vpn_service import VPNService
from .deps import enforce_tenant_action, get_tenant_task_or_404
from runtime_shared.docker_contracts import IMAGE_INTERNAL_VPN_SCRIPT_PATH
from runtime_shared.vpn_observability import normalize_vpn_log_lines, parse_vpn_status_output

router = APIRouter()
logger = logging.getLogger(__name__)

_VPN_STATUS_COMMAND = f"bash {IMAGE_INTERNAL_VPN_SCRIPT_PATH} status"


def _raise_provider_failure(*, prefix: str, result) -> None:
    """Raise deterministic HTTP errors for runtime provider failures."""
    status_code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if str(result.error_code or "").strip() == "RUNNER_OPERATION_RESULT_TIMEOUT"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    raise HTTPException(status_code=status_code, detail=provider_result_detail(prefix, result))


def _runtime_is_ready_for_vpn_operations(task: Any) -> bool:
    """Return whether user-triggered VPN operations can safely enter the runtime."""
    task_status = str(getattr(task, "status", "") or "").strip().lower()
    return task_status == TaskStatus.RUNNING.value


@router.post("/{task_id}/vpn/configure")
async def configure_task_vpn(
    task_id: int,
    vpn_config: VPNConfigCreate,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Configure VPN for an existing task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    vpn_service = VPNService(db)
    success, message = vpn_service.configure_task_vpn(task_id, vpn_config)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    materialized = _runtime_is_ready_for_vpn_operations(task)
    if materialized:
        runtime_result = await TaskLifecycleService(db).materialize_task_vpn_config_async(
            task=task,
            user_id=current_user.id,
            db=db,
            actor_type=RuntimeActorType.USER,
        )
        if runtime_result is not None and not runtime_result.ok:
            _raise_provider_failure(
                prefix=f"Failed to configure VPN runtime for task {task_id}",
                result=runtime_result,
            )
        db.refresh(task)
        if task.vpn_connection_status == "failed":
            raise HTTPException(status_code=503, detail=task.vpn_error_message or "VPN startup failed")
    return {"message": message, "task_id": task_id, "runtime_materialized": materialized}


@router.post("/{task_id}/vpn/upload")
async def upload_vpn_config(
    task_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    try:
        content = await file.read()
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid file content")

    vpn_service = VPNService(db)
    vpn_config = VPNConfigCreate(provider="custom", config_data=text)
    ok, msg = vpn_service.configure_task_vpn(task_id, vpn_config)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    materialized = _runtime_is_ready_for_vpn_operations(task)
    if materialized:
        runtime_result = await TaskLifecycleService(db).materialize_task_vpn_config_async(
            task=task,
            user_id=current_user.id,
            db=db,
            actor_type=RuntimeActorType.USER,
        )
        if runtime_result is not None and not runtime_result.ok:
            _raise_provider_failure(
                prefix=f"Failed to upload VPN runtime config for task {task_id}",
                result=runtime_result,
            )
        db.refresh(task)
        if task.vpn_connection_status == "failed":
            raise HTTPException(status_code=503, detail=task.vpn_error_message or "VPN startup failed")
    return {
        "message": "VPN configuration uploaded",
        "task_id": task_id,
        "runtime_materialized": materialized,
    }


@router.post("/{task_id}/vpn/retry")
async def retry_vpn_connection(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Manually retry VPN connection inside the task runtime.

    Exec command is ``build_vpn_connect_exec_shell()`` (resolver + ``bash``; image-internal path).
    """
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    if not task.vpn_enabled:
        raise HTTPException(status_code=400, detail="VPN not configured for this task")
    if not _runtime_is_ready_for_vpn_operations(task):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task runtime must be running before VPN retry.",
        )

    try:
        result = await TaskLifecycleService(db).retry_task_vpn_connection_async(
            task=task,
            user_id=current_user.id,
            db=db,
            actor_type=RuntimeActorType.USER,
        )
        if not result.ok:
            await VPNService(db).update_vpn_status(
                task_id=task_id,
                status="failed",
                error_message=provider_result_detail("VPN reconnect failed", result),
            )
            _raise_provider_failure(prefix=f"Failed to retry VPN for task {task_id}", result=result)
        res = result.metadata.get("delegate_result")
        runtime_status = _extract_vpn_runtime_status(res)
        connection_status = runtime_status[0] if runtime_status is not None else "reconnecting"
        ip_address = runtime_status[1] if runtime_status is not None else None
        error_message = runtime_status[2] if runtime_status is not None else None
        await VPNService(db).update_vpn_status(
            task_id=task_id,
            status=connection_status,
            ip_address=ip_address,
            error_message=error_message,
        )
        exit_code, logs = _extract_vpn_operation_details(res)
        return {
            "message": "VPN reconnect initiated",
            "accepted": True,
            "connection_status": connection_status,
            "exit_code": exit_code,
            "logs": logs[-20:] if isinstance(logs, list) else logs,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retry VPN: {e}")


def _extract_vpn_operation_details(delegate_result: Any) -> tuple[int | None, list[dict[str, Any]]]:
    """Extract bounded reconnect diagnostics from local or runner result shapes."""
    if isinstance(delegate_result, dict):
        exit_code = delegate_result.get("exit_code")
        stdout = str(delegate_result.get("stdout") or "")
        stdout_logs = normalize_vpn_log_lines(
            line for line in stdout.splitlines() if not line.lstrip().startswith("{")
        )
        existing_logs = delegate_result.get("logs")
        logs = stdout_logs or (existing_logs if isinstance(existing_logs, list) else [])
        metadata = delegate_result.get("metadata")
        if isinstance(metadata, dict):
            nested_exit_code, nested_logs = _extract_vpn_operation_details(metadata)
            return (
                int(exit_code) if isinstance(exit_code, int) else nested_exit_code,
                logs or nested_logs,
            )
        return int(exit_code) if isinstance(exit_code, int) else None, logs
    if isinstance(delegate_result, list):
        return None, [item for item in delegate_result if isinstance(item, dict)]
    return None, []


def _extract_vpn_runtime_status(delegate_result: Any) -> tuple[str, str | None, str | None] | None:
    """Extract runtime VPN status tuple from provider delegate logs when available."""
    if isinstance(delegate_result, dict):
        parsed = parse_vpn_status_output(delegate_result.get("stdout"))
        if parsed is not None:
            return str(parsed["status"]), parsed.get("ip_address"), parsed.get("error_message")
        metadata = delegate_result.get("metadata")
        if isinstance(metadata, dict):
            parsed = parse_vpn_status_output(metadata.get("stdout"))
            if parsed is not None:
                return str(parsed["status"]), parsed.get("ip_address"), parsed.get("error_message")
        nested = delegate_result.get("delegate_result")
        if nested is not None:
            return _extract_vpn_runtime_status(nested)
        return None
    if isinstance(delegate_result, list):
        for item in reversed(delegate_result):
            if not isinstance(item, dict):
                continue
            parsed = parse_vpn_status_output(item.get("message"))
            if parsed is not None:
                return str(parsed["status"]), parsed.get("ip_address"), parsed.get("error_message")
    return None


@router.get("/{task_id}/vpn/status", response_model=VPNStatusResponse)
async def get_vpn_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    runtime_mode = str(getattr(task, "runtime_placement_mode", "local") or "").strip().lower()
    runner_task = runtime_mode == "runner"

    vpn_service = VPNService(db)
    status_obj = await vpn_service.get_vpn_status(task_id)
    if status_obj is None:
        raise HTTPException(status_code=404, detail="VPN not configured for this task")
    if not _runtime_is_ready_for_vpn_operations(task):
        return status_obj
    runtime_operations = RuntimeOperationService(db)
    provider_result = await runtime_operations.run_authorized_task_operation(
        task=task,
        user_id=current_user.id,
        operation="check_vpn_status",
        call=lambda provider, request: provider.check_vpn_status(request),
        payload={"command": _VPN_STATUS_COMMAND},
        metadata={"wait_for_result": True, "wait_timeout_seconds": 15.0},
    )
    if not provider_result.ok and runner_task:
        _raise_provider_failure(prefix=f"Failed to get VPN runtime status for task {task_id}", result=provider_result)
    runtime_status = _extract_vpn_runtime_status(provider_result.metadata.get("delegate_result"))
    if runtime_status is None and runner_task:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to get VPN runtime status: missing runtime status snapshot from provider result",
        )
    if provider_result.ok and runtime_status is not None:
        status_name, ip_address, error_message = runtime_status
        await vpn_service.update_vpn_status(
            task_id=task_id,
            status=status_name,
            ip_address=ip_address,
            error_message=error_message,
        )
        refreshed = await vpn_service.get_vpn_status(task_id)
        if refreshed is not None:
            return refreshed
    return status_obj
