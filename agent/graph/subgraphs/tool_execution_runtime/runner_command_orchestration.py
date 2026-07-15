"""Orchestrate prepared runner tool-command execution for graph tool calls.

This module owns runner-specific command preparation and provider dispatch for
`GraphToolExecutor`, keeping the adapter focused on graph/executor bridging.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Dict, Mapping, Optional

from agent.tool_runtime.command_preparation import prepare_tool_command
from agent.tool_runtime.pty_transport import should_use_pty, tool_supports_pty
from agent.tool_runtime.timeout_policy import ToolTimeoutPlan
from agent.utils.truncation_config import STDERR_SNIPPET
from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimePlacementMode,
)
from runtime_shared.runner_protocol import is_completed_process_tool_result_status
from runtime_shared.tool_command_transport import (
    TRANSPORT_FILE_COMM,
    TRANSPORT_PTY,
    normalize_tool_command_transport,
)
from runtime_shared.workspace_files import (
    runtime_workspace_directories_to_payload,
    runtime_workspace_files_to_payload,
)

from ...infrastructure.state_models import GraphRuntimeContext
from .lane_dispatch import ToolLaneDispatchDecision
from .remote_result_adapter import adapt_remote_tool_result
from .runner_command_result_finalizer import finalize_runner_command_result


async def execute_runner_container_tool_via_provider(
    *,
    request: Dict[str, Any],
    parameters: Dict[str, Any],
    timeout_plan: ToolTimeoutPlan,
    context: Optional[GraphRuntimeContext],
    decision: ToolLaneDispatchDecision,
    workspace_path: Optional[str],
    parallel_pty_identity: Any,
    allow_pty: bool,
    get_executor: Callable[..., Any],
    resolve_runtime_actor_type: Callable[[Any], Any],
    get_provider: Callable[..., Any],
) -> Dict[str, Any]:
    """Execute a container-scoped graph tool call through the runner provider."""
    tool_id = str(request["tool"])
    required_fields = {
        "tenant_id": request.get("tenant_id")
        or (context.tenant_id if context else None),
        "workspace_id": request.get("workspace_id")
        or (context.workspace_id if context else None),
        "runtime_placement_mode": request.get("runtime_placement_mode")
        or (context.runtime_placement_mode if context else None),
        "task_id": request.get("task_id") or (context.task_id if context else None),
    }
    missing = [name for name, value in required_fields.items() if value in (None, "")]
    if missing:
        missing_text = ", ".join(missing)
        message = (
            "tool execution runtime identity is incomplete for runner lane dispatch: "
            f"missing {missing_text}."
        )
        return _graph_error_payload(
            tool_id=tool_id,
            message=message,
            error_code="missing_runtime_identity",
            status="missing_runtime_identity",
        )

    timeout_policy = {
        "deadline_seconds": float(timeout_plan.deadline_seconds),
        "grace_seconds": float(timeout_plan.grace_seconds),
    }
    requested_transport = _runner_transport_from_parameters(parameters)
    should_route_pty = (
        requested_transport == TRANSPORT_PTY
        or (
            requested_transport is None
            and _should_use_runner_pty(tool_id, parameters)
        )
    )
    control_workspace_path = _resolve_control_plane_workspace_path(
        required_fields["task_id"],
        workspace_path=workspace_path,
    )
    if not control_workspace_path:
        message = "runner command preparation requires a backend task workspace."
        return _graph_error_payload(
            tool_id=tool_id,
            message=message,
            error_code="missing_control_plane_workspace",
            status="missing_control_plane_workspace",
        )

    try:
        prep_executor = get_executor(
            control_workspace_path,
            int(required_fields["task_id"]),
            request.get("model") or (context.model if context else None),
            runtime_placement_mode=str(required_fields["runtime_placement_mode"]),
            ignore_provided=True,
        )
    except TypeError as exc:
        if "ignore_provided" not in str(exc):
            raise
        prep_executor = get_executor(
            control_workspace_path,
            int(required_fields["task_id"]),
            request.get("model") or (context.model if context else None),
            runtime_placement_mode=str(required_fields["runtime_placement_mode"]),
        )
    artifact_stamp = parallel_pty_identity.artifact_stamp if parallel_pty_identity else None
    prepared = await prepare_tool_command(
        tool_id=tool_id,
        parameters=dict(parameters),
        config=prep_executor.config,
        logger=getattr(prep_executor, "logger", None),
        transport=TRANSPORT_PTY if should_route_pty and allow_pty else TRANSPORT_FILE_COMM,
        explicit_command_builder=prep_executor._tool_to_shell_command,
        interrupt_id=request.get("interrupt_id"),
        tool_call_id=request.get("tool_call_id"),
        tool_batch_id=request.get("tool_batch_id"),
        artifact_stamp=artifact_stamp,
        timeout_plan=timeout_plan,
    )
    payload: Dict[str, Any] = {
        "tool": tool_id,
        "command": prepared.command,
        "cwd": "/workspace",
        "env": {},
        "timeout_seconds": float(timeout_plan.deadline_seconds),
        "timeout_policy": timeout_policy,
        "wait_for_result": True,
        "command_id": str(request.get("tool_call_id") or uuid.uuid4()),
        "tool_call_id": request.get("tool_call_id"),
        "tool_batch_id": request.get("tool_batch_id"),
        "workspace_files": runtime_workspace_files_to_payload(
            prepared.pre_execution_workspace_files
        ),
        "workspace_directories": runtime_workspace_directories_to_payload(
            prepared.pre_execution_workspace_directories
        ),
    }
    if should_route_pty and allow_pty:
        payload["transport"] = TRANSPORT_PTY
        payload["session_name"] = (
            parallel_pty_identity.session_name if parallel_pty_identity else None
        )
        payload["cleanup_session"] = bool(parallel_pty_identity)
        payload["artifact_stamp"] = artifact_stamp
    elif requested_transport in {TRANSPORT_FILE_COMM, TRANSPORT_PTY}:
        payload["transport"] = TRANSPORT_FILE_COMM

    runtime_request = RuntimeOperationRequest(
        tenant_id=int(required_fields["tenant_id"]),
        task_id=int(required_fields["task_id"]),
        user_id=request.get("user_id") or (context.user_id if context else None),
        actor_type=resolve_runtime_actor_type(
            request.get("actor_type") or (context.actor_type if context else None)
        ),
        actor_id=str(request.get("actor_id") or (context.actor_id if context else "langgraph")),
        runtime_placement_mode=RuntimePlacementMode(
            str(required_fields["runtime_placement_mode"])
        ),
        workspace_id=str(required_fields["workspace_id"]),
        runner_id=request.get("runner_id") or (context.runner_id if context else None),
        execution_site_id=request.get("execution_site_id")
        or (context.execution_site_id if context else None),
        operation="send_tool_command",
        payload=payload,
        metadata={
            "lane_dispatch": {
                "lane": decision.lane,
                "authority": decision.authority,
            },
            "wait_for_result": True,
        },
    )
    provider = get_provider(runtime_placement_mode=runtime_request.runtime_placement_mode)
    started = monotonic()
    result = await provider.send_tool_command(runtime_request)
    duration = monotonic() - started
    delegate = result.metadata.get("delegate_result") if isinstance(result.metadata, dict) else None
    has_delegate_result = isinstance(delegate, dict)
    route_policy = {
        "selected_lane": decision.lane,
        "selected_authority": decision.authority,
    }
    timeout_policy_metadata = timeout_plan.to_metadata()
    provider_metadata = dict(result.metadata) if isinstance(result.metadata, Mapping) else {}
    if not has_delegate_result and result.ok:
        provider_metadata.setdefault("error_code", "tool_result_missing")
    enriched_delegate = await finalize_runner_command_result(
        prepared=prepared,
        delegate=delegate if isinstance(delegate, Mapping) else None,
        provider_ok=result.ok,
        command=prepared.command,
        artifact_stamp=artifact_stamp,
        timeout_policy=timeout_policy_metadata,
        provider=provider,
        runtime_request=runtime_request,
        provider_metadata=provider_metadata,
    )

    if (
        isinstance(enriched_delegate, Mapping)
        and runtime_request.runtime_placement_mode is RuntimePlacementMode.RUNNER
        and getattr(provider, "provider_name", None) == "cloud_runner"
        and _is_completed_runner_process(
            raw_delegate=delegate if isinstance(delegate, Mapping) else None
        )
    ):
        enriched_delegate = await _finalize_and_promote_cloud_runner_command(
            provider=provider,
            runtime_request=runtime_request,
            tool_id=tool_id,
            payload=payload,
            provider_metadata=provider_metadata,
            enriched_delegate=enriched_delegate,
            raw_delegate=delegate if isinstance(delegate, Mapping) else None,
        ) or enriched_delegate

    return adapt_remote_tool_result(
        tool_id=tool_id,
        provider_ok=result.ok,
        provider_error_code=str(result.error_code) if result.error_code else None,
        provider_error_message=str(result.error_message) if result.error_message else None,
        provider_metadata=provider_metadata,
        delegate_result=enriched_delegate,
        duration_seconds=duration,
        route_policy=route_policy,
        timeout_policy=timeout_policy_metadata,
        missing_result=bool(result.ok and not has_delegate_result),
    )


def _is_completed_runner_process(*, raw_delegate: Mapping[str, Any] | None) -> bool:
    """Return True when the runner reported a finished process awaiting canonical verdict.

    Only a `completed` status triggers control-plane finalize/promote. Every other
    status is a runner-owned terminal non-completion (`timed_out`/`cancelled`/
    session/start failure), whose verdict the finalizer must never override.
    """
    if not isinstance(raw_delegate, Mapping):
        return False
    status = str(raw_delegate.get("status") or "").strip().lower()
    return is_completed_process_tool_result_status(status)


def _materialized_paths_from_delegate(delegate: Mapping[str, Any]) -> list[str]:
    metadata = delegate.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    materialization = metadata.get("artifact_materialization")
    if not isinstance(materialization, Mapping):
        return []
    materialized_paths = materialization.get("materialized_paths")
    if not isinstance(materialized_paths, list):
        return []
    return [str(path).strip() for path in materialized_paths if str(path).strip()]


def _canonical_tool_verdict_from_delegate(delegate: Mapping[str, Any]) -> tuple[str, bool, int]:
    enriched_status = str(delegate.get("status") or "").strip().lower()
    if enriched_status == "success":
        canonical_status = "succeeded"
    elif enriched_status in {"succeeded", "failed"}:
        canonical_status = enriched_status
    else:
        canonical_status = "succeeded" if bool(delegate.get("success")) else "failed"
    canonical_success = canonical_status == "succeeded"
    try:
        exit_code = int(delegate.get("exit_code") if delegate.get("exit_code") is not None else (0 if canonical_success else 1))
    except (TypeError, ValueError):
        exit_code = 0 if canonical_success else 1
    return canonical_status, canonical_success, exit_code


async def _finalize_and_promote_cloud_runner_command(
    *,
    provider: Any,
    runtime_request: RuntimeOperationRequest,
    tool_id: str,
    payload: Dict[str, Any],
    provider_metadata: Dict[str, Any],
    enriched_delegate: Mapping[str, Any],
    raw_delegate: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    finalize = getattr(provider, "finalize_tool_command_result", None)
    if finalize is None:
        return None

    canonical_status, canonical_success, canonical_exit_code = _canonical_tool_verdict_from_delegate(
        enriched_delegate
    )
    materialized_paths = _materialized_paths_from_delegate(enriched_delegate)
    artifacts = materialized_paths or list(enriched_delegate.get("artifacts") or [])
    raw_metadata = raw_delegate.get("metadata") if isinstance(raw_delegate, Mapping) else {}
    raw_metadata_mapping = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    enriched_metadata = enriched_delegate.get("metadata")
    enriched_metadata_mapping = (
        dict(enriched_metadata) if isinstance(enriched_metadata, Mapping) else {}
    )
    identity_payload: Dict[str, Any] = {
        "tool_command_runtime_job_id": provider_metadata.get("runtime_job_id"),
        "task_runtime_job_id": provider_metadata.get("task_runtime_job_id")
        or raw_metadata_mapping.get("task_runtime_job_id"),
        "command_id": payload.get("command_id"),
        "workspace_id": runtime_request.workspace_id,
        "tool_call_id": payload.get("tool_call_id"),
        "tool_batch_id": payload.get("tool_batch_id"),
        "tool": tool_id,
        "artifacts": artifacts,
        "canonical_status": canonical_status,
        "canonical_success": canonical_success,
        "canonical_exit_code": canonical_exit_code,
        "stdout": enriched_delegate.get("stdout") or "",
        "stderr": enriched_delegate.get("stderr") or "",
        "process_success": raw_delegate.get("success") if isinstance(raw_delegate, Mapping) else None,
        "process_exit_code": raw_delegate.get("exit_code") if isinstance(raw_delegate, Mapping) else None,
        "metadata": enriched_metadata_mapping,
        "wait_for_result": True,
    }
    finalize_request = RuntimeOperationRequest(
        tenant_id=runtime_request.tenant_id,
        task_id=runtime_request.task_id,
        user_id=runtime_request.user_id,
        actor_type=runtime_request.actor_type,
        actor_id=runtime_request.actor_id,
        runtime_placement_mode=runtime_request.runtime_placement_mode,
        workspace_id=runtime_request.workspace_id,
        runner_id=runtime_request.runner_id,
        execution_site_id=runtime_request.execution_site_id,
        operation="finalize_tool_command_result",
        payload=identity_payload,
        metadata={"wait_for_result": True},
    )
    finalize_result = await finalize(finalize_request)
    if not finalize_result.ok:
        return enriched_delegate
    finalized_delegate = (
        finalize_result.metadata.get("delegate_result")
        if isinstance(finalize_result.metadata, Mapping)
        else None
    )
    resolved_delegate = (
        finalized_delegate if isinstance(finalized_delegate, Mapping) else enriched_delegate
    )

    if not artifacts:
        return resolved_delegate

    promote = getattr(provider, "promote_artifact_refs", None)
    if promote is None:
        return resolved_delegate

    promote_payload = dict(identity_payload)
    promote_payload["timeout_seconds"] = payload.get("timeout_seconds")
    promote_payload["timeout_policy"] = payload.get("timeout_policy")
    promote_request = RuntimeOperationRequest(
        tenant_id=runtime_request.tenant_id,
        task_id=runtime_request.task_id,
        user_id=runtime_request.user_id,
        actor_type=runtime_request.actor_type,
        actor_id=runtime_request.actor_id,
        runtime_placement_mode=runtime_request.runtime_placement_mode,
        workspace_id=runtime_request.workspace_id,
        runner_id=runtime_request.runner_id,
        execution_site_id=runtime_request.execution_site_id,
        operation="promote_artifact_refs",
        payload=promote_payload,
        metadata={"wait_for_result": True},
    )
    promote_result = await promote(promote_request)
    if not promote_result.ok and isinstance(promote_result.metadata, Mapping):
        merged_metadata = dict(resolved_delegate.get("metadata") or {})
        merged_metadata["artifact_promotion"] = {
            "status": "failed",
            "error_code": promote_result.error_code,
            "error_message": promote_result.error_message,
        }
        return {**dict(resolved_delegate), "metadata": merged_metadata}
    return resolved_delegate


def _graph_error_payload(
    *,
    tool_id: str,
    message: str,
    error_code: str,
    status: str,
) -> Dict[str, Any]:
    """Build the graph tool-result error shape used before provider dispatch."""
    return {
        "tool": tool_id,
        "success": False,
        "stdout": "",
        "stderr": message,
        "stdout_excerpt": "",
        "stderr_excerpt": message[:STDERR_SNIPPET],
        "exit_code": 2,
        "observation": message,
        "approval_granted": True,
        "approval_reason": None,
        "approval_metadata": {},
        "duration": 0.0,
        "metadata": {"error_code": error_code},
        "status": status,
    }


def _runner_transport_from_parameters(parameters: Mapping[str, Any]) -> str | None:
    return normalize_tool_command_transport(parameters.get("transport"))


def _should_use_runner_pty(tool_id: str, parameters: Mapping[str, Any]) -> bool:
    return should_use_pty(
        tool_id,
        dict(parameters),
        is_pty_enabled_fn=lambda: os.getenv("ENABLE_PTY_EXECUTION", "false").lower() == "true",
        tool_supports_pty_fn=tool_supports_pty,
    )


def _resolve_control_plane_workspace_path(
    task_id: Any,
    *,
    workspace_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve the backend task workspace used for command prep and artifacts."""
    if isinstance(workspace_path, str) and workspace_path.strip():
        try:
            candidate = Path(workspace_path)
            if candidate.exists() and candidate.is_dir():
                return str(candidate)
        except Exception:
            pass
    try:
        from backend.config.workspace_config import WorkspaceConfig

        return str(WorkspaceConfig.ensure_workspace_structure(int(task_id)))
    except Exception:
        return None


__all__ = ["execute_runner_container_tool_via_provider"]
