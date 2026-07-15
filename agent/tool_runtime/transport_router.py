"""Transport sequencing and compatibility exports for runtime tool execution.

This module owns route sequencing, fail-closed enforcement, direct execution
control flow, and the compatibility import surface used by executor wrappers
and tests. It does not own PTY internals or transport-aware result enrichment.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

try:
    from .backend_tool_policy import (
        build_route_policy_metadata,
        classify_tool_surface,
        lane_allows_direct_execution,
        lane_allows_file_comm,
        lane_allows_pty,
        require_runtime_placement_mode,
        resolve_selected_authority,
        resolve_runner_runtime_tool_support,
        resolve_execution_lane,
    )
except Exception:  # pragma: no cover
    from agent.tool_runtime.backend_tool_policy import (
        build_route_policy_metadata,
        classify_tool_surface,
        lane_allows_direct_execution,
        lane_allows_file_comm,
        lane_allows_pty,
        require_runtime_placement_mode,
        resolve_selected_authority,
        resolve_runner_runtime_tool_support,
        resolve_execution_lane,
    )
try:
    from .pty_transport import (
        build_pty_transport_command,
        execute_via_pty_transport,
        resolve_pty_enabled_cached,
        should_use_pty,
        tool_supports_pty,
    )
except Exception:  # pragma: no cover
    from agent.tool_runtime.pty_transport import (
        build_pty_transport_command,
        execute_via_pty_transport,
        resolve_pty_enabled_cached,
        should_use_pty,
        tool_supports_pty,
    )
try:
    from .result_enrichment import build_pty_tool_result, enrich_direct_execution_result
except Exception:  # pragma: no cover
    from agent.tool_runtime.result_enrichment import (
        build_pty_tool_result,
        enrich_direct_execution_result,
    )
try:
    from ..tools.parameter_validation import validation_result_from_exception
except Exception:  # pragma: no cover
    from agent.tools.parameter_validation import validation_result_from_exception
from .runtime_context import bind_tool_runtime_context, build_tool_runtime_context, coerce_task_id
from .timeout_policy import (
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
    ToolTimeoutPlan,
    resolve_tool_timeout_plan,
)

if TYPE_CHECKING:
    from agent.models import ExecutionResult


def _safe_log_route_event(
    logger: Any,
    *,
    event: str,
    tool_id: str,
    lane: str,
    selected_authority: str,
    selected_transport: str,
    fallback_reason: str = "",
) -> None:
    """Emit deterministic route diagnostics without letting logger failures break execution."""
    if not logger:
        return
    payload = build_route_policy_metadata(
        event=event,
        tool_id=tool_id,
        lane=lane,
        selected_authority=selected_authority,
        selected_transport=selected_transport,
        fallback_reason=fallback_reason,
    )
    try:
        logger.log_operation("INFO", f"[route] {event}", metadata=payload)
    except Exception:
        try:
            logger.log_operation(
                "INFO",
                f"[route] {event} lane={lane} authority={selected_authority} "
                f"transport={selected_transport} "
                f"tool={tool_id} fallback_reason={fallback_reason or '<none>'}",
            )
        except Exception:
            pass


def _optional_pty_identity_kwargs(
    *,
    tool_batch_id: Optional[str],
    session_name: Optional[str],
    cleanup_session: bool,
    artifact_stamp: Optional[int],
) -> Dict[str, Any]:
    """Return PTY identity kwargs only when the caller supplied them."""
    kwargs: Dict[str, Any] = {}
    if tool_batch_id:
        kwargs["tool_batch_id"] = tool_batch_id
    if session_name:
        kwargs["session_name"] = session_name
        kwargs["cleanup_session"] = cleanup_session
    if artifact_stamp is not None:
        kwargs["artifact_stamp"] = artifact_stamp
    return kwargs


async def execute_single_tool_with_fallback(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    config: Any,
    logger: Any = None,
    file_comm: Any = None,
    validate_tool_parameters_fn: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    build_validation_error_result_fn: Optional[Callable[[Any], "ExecutionResult"]] = None,
    should_use_pty_fn: Callable[[str, Dict[str, Any]], bool],
    execute_via_pty_fn: Callable[..., Any],
    execute_tool_via_comm_fn: Callable[..., Any],
    run_tool_by_name_fn: Callable[[str, Dict[str, Any]], Any],
    safe_inc_metric_fn: Callable[[str], None],
    pty_session_not_available_exc_type: Optional[type] = None,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    allow_pty: bool = True,
    tool_batch_id: Optional[str] = None,
    session_name: Optional[str] = None,
    cleanup_session: bool = False,
    artifact_stamp: Optional[int] = None,
    timeout_plan: Optional[ToolTimeoutPlan] = None,
) -> "ExecutionResult":
    """Execute one tool call with explicit lane policy and guarded transport fallback."""
    try:
        from ..models import ExecutionResult
        from ..tools.utils import attach_execution_result_extras
        from ..utils.workspace_helpers import resolve_host_workspace_path, temporary_cwd
    except Exception:  # pragma: no cover
        from agent.models import ExecutionResult
        from agent.tools.utils import attach_execution_result_extras
        from agent.utils.workspace_helpers import resolve_host_workspace_path, temporary_cwd

    if isinstance(parameters, dict):
        parameters = {k: v for k, v in parameters.items() if v is not None}
    if timeout_plan is None or timeout_plan.tool_id != str(tool_id):
        timeout_plan = resolve_tool_timeout_plan(
            tool_id=tool_id,
            parameters=parameters,
            config=config,
        )
    parameters = dict(timeout_plan.normalized_parameters)

    execution_lane = resolve_execution_lane(str(tool_id))
    try:
        runtime_placement_mode = require_runtime_placement_mode(
            getattr(config, "runtime_placement_mode", None)
        )
        selected_authority = resolve_selected_authority(
            lane=execution_lane,
            runtime_placement_mode=runtime_placement_mode,
        )
    except ValueError as exc:
        missing_result = ExecutionResult(
            success=False,
            stdout="",
            stderr=str(exc),
            exit_code=2,
        )
        attach_execution_result_extras(
            missing_result,
            metadata={
                "error_code": "missing_runtime_placement",
                "route_policy": build_route_policy_metadata(
                    event="missing_runtime_placement",
                    tool_id=tool_id,
                    lane=execution_lane,
                    selected_authority="none",  # type: ignore[arg-type]
                    selected_transport="blocked-pre-dispatch",
                    fallback_reason="missing_runtime_placement",
                ),
            },
        )
        return missing_result
    runner_support = resolve_runner_runtime_tool_support(str(tool_id))
    allows_pty = lane_allows_pty(execution_lane)
    allows_file_comm = lane_allows_file_comm(execution_lane)
    allows_direct_execution = lane_allows_direct_execution(execution_lane)
    is_artifact_tool = execution_lane == "artifact_scoped"
    runtime_task_id_raw = getattr(config, "task_id", parameters.get("task_id"))
    runtime_task_id = coerce_task_id(runtime_task_id_raw)
    runtime_tenant_id = getattr(config, "tenant_id", parameters.get("tenant_id"))
    workspace_path = getattr(config, "workspace_path", parameters.get("workspace_path"))
    if (
        runtime_placement_mode == "runner"
        and selected_authority in {"backend_direct", "artifact_direct"}
    ):
        workspace_path = parameters.get("workspace_path")
    selected_transport = "none"
    fallback_reason = ""

    _safe_log_route_event(
        logger,
        event="lane_selected",
        tool_id=tool_id,
        lane=execution_lane,
        selected_authority=selected_authority,
        selected_transport=selected_transport,
    )

    if is_artifact_tool and runtime_task_id is None:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=(
                "artifact tools require active runtime task context. "
                "The current execution path did not provide a valid task_id."
            ),
            exit_code=2,
        )

    if (
        runtime_placement_mode == "runner"
        and not runner_support.supported
    ):
        selected_transport = "blocked-pre-dispatch"
        fallback_reason = str(runner_support.error_code or "unsupported_in_runner_v1")
        _safe_log_route_event(
            logger,
            event="runner_tool_unsupported",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport=selected_transport,
            fallback_reason=fallback_reason,
        )
        unsupported_result = ExecutionResult(
            success=False,
            stdout="",
            stderr=str(
                runner_support.error_message
                or f"Tool `{tool_id}` is unsupported in runner runtime image v1."
            ),
            exit_code=2,
        )
        attach_execution_result_extras(
            unsupported_result,
            metadata={
                "error_code": str(runner_support.error_code or "unsupported_in_runner_v1"),
                "route_policy": build_route_policy_metadata(
                    event="runner_tool_unsupported",
                    tool_id=tool_id,
                    lane=execution_lane,
                    selected_authority=selected_authority,
                    selected_transport=selected_transport,
                    fallback_reason=fallback_reason,
                ),
                "runner_tool_policy": {
                    "runtime_placement_mode": runtime_placement_mode,
                    "supported": False,
                    "classification": runner_support.classification,
                    "tool_surface_classification": classify_tool_surface(tool_id),
                },
            },
        )
        return unsupported_result

    if validate_tool_parameters_fn is not None:
        validation_result = validate_tool_parameters_fn(tool_id, parameters)
        if validation_result is not None and not getattr(validation_result, "valid", False):
            if logger:
                logger.log_operation(
                    "WARNING",
                    f"EnhancedExecutor: parameter validation failed for {tool_id}",
                    metadata={
                        "tool_id": tool_id,
                        "validation_error_count": len(
                            getattr(validation_result, "validation_errors", []) or []
                        ),
                        "validation_reason": getattr(validation_result, "reason", None),
                    },
                )
            safe_inc_metric_fn("executor_param_validation_failures")
            if build_validation_error_result_fn is None:
                return ExecutionResult(False, "", "Validation failed", -1)
            return build_validation_error_result_fn(
                getattr(validation_result, "validation_errors", [])
            )
        if validation_result is not None and getattr(validation_result, "valid", False):
            parameters = dict(getattr(validation_result, "normalized_parameters", {}) or parameters)

    if allows_pty and allow_pty and should_use_pty_fn(tool_id, parameters):
        selected_transport = "pty"
        _safe_log_route_event(
            logger,
            event="transport_attempt",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport=selected_transport,
        )
        if logger:
            logger.log_operation("INFO", f"[PTY] Attempting PTY execution for {tool_id}")

        safe_inc_metric_fn("executor_pty_attempts")

        try:
            result = await execute_via_pty_fn(
                tool_id,
                parameters,
                interrupt_id=interrupt_id,
                tool_call_id=tool_call_id,
                **_optional_pty_identity_kwargs(
                    tool_batch_id=tool_batch_id,
                    session_name=session_name,
                cleanup_session=cleanup_session,
                artifact_stamp=artifact_stamp,
                ),
                timeout_plan=timeout_plan,
            )
        except Exception as exc:
            if (
                pty_session_not_available_exc_type is not None
                and isinstance(exc, pty_session_not_available_exc_type)
            ):
                if logger:
                    logger.log_operation(
                        "ERROR",
                        f"[PTY] PTY session unavailable for {tool_id}: {exc}",
                    )
                raise

            validation_result = validation_result_from_exception(
                tool_id,
                exc,
                raw_parameters=parameters,
            )
            if validation_result is not None:
                if logger:
                    logger.log_operation(
                        "WARNING",
                        f"[PTY] Validation failed during PTY preparation for {tool_id}: {exc}",
                        metadata={
                            "tool_id": tool_id,
                            "validation_error_count": len(validation_result.validation_errors),
                            "validation_reason": validation_result.reason,
                        },
                    )
                safe_inc_metric_fn("executor_param_validation_failures")
                if build_validation_error_result_fn is None:
                    return ExecutionResult(False, "", "Validation failed", -1)
                return build_validation_error_result_fn(validation_result.validation_errors)

            if logger:
                logger.log_operation(
                    "WARNING",
                    f"[PTY] PTY execution failed for {tool_id}, falling back: {exc}",
                )
            safe_inc_metric_fn("executor_pty_fallback")
            fallback_reason = f"pty_failed:{exc.__class__.__name__}"
            _safe_log_route_event(
                logger,
                event="transport_fallback",
                tool_id=tool_id,
                lane=execution_lane,
                selected_authority=selected_authority,
                selected_transport=selected_transport,
                fallback_reason=fallback_reason,
            )
        else:
            safe_inc_metric_fn("executor_pty_success")
            return result
    elif allows_pty:
        fallback_reason = "pty_missing_parallel_identity" if not allow_pty else "pty_not_selected"
        _safe_log_route_event(
            logger,
            event="transport_skipped",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport="pty",
            fallback_reason=fallback_reason,
        )

    if file_comm is not None and allows_file_comm:
        selected_transport = "file-comm"
        _safe_log_route_event(
            logger,
            event="transport_attempt",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport=selected_transport,
            fallback_reason=fallback_reason,
        )
        via_comm = await execute_tool_via_comm_fn(
            tool_id,
            parameters,
            log_mode="enhanced",
            include_metrics=True,
            timeout_plan=timeout_plan,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
        )
        if via_comm is not None:
            return via_comm
        fallback_reason = "file_comm_unavailable_or_failed"
        _safe_log_route_event(
            logger,
            event="transport_fallback",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport=selected_transport,
            fallback_reason=fallback_reason,
        )
    elif file_comm is not None and execution_lane == "backend_scoped" and logger:
        logger.log_operation(
            "DEBUG",
            f"[backend-scoped] Skipping file-comm route for {tool_id}; using direct runtime-scoped execution.",
        )
    elif file_comm is not None and is_artifact_tool and logger:
        logger.log_operation(
            "DEBUG",
            f"[artifact] Skipping file-comm route for {tool_id}; using direct runtime-scoped execution.",
        )

    if not allows_direct_execution:
        safe_inc_metric_fn("executor_route_policy_violation")
        selected_transport = "blocked-direct"
        if not fallback_reason:
            fallback_reason = "no_allowed_transport_available"
        _safe_log_route_event(
            logger,
            event="route_policy_violation",
            tool_id=tool_id,
            lane=execution_lane,
            selected_authority=selected_authority,
            selected_transport=selected_transport,
            fallback_reason=fallback_reason,
        )
        violation_result = ExecutionResult(
            success=False,
            stdout="",
            stderr=(
                "Route policy violation: container-scoped tools cannot execute via direct runtime fallback. "
                f"tool_id={tool_id} lane={execution_lane} fallback_reason={fallback_reason}"
            ),
            exit_code=3,
        )
        try:
            setattr(
                violation_result,
                "metadata",
                {
                    "route_policy": build_route_policy_metadata(
                        event="route_policy_violation",
                        tool_id=tool_id,
                        lane=execution_lane,
                        selected_authority=selected_authority,
                        selected_transport=selected_transport,
                        fallback_reason=fallback_reason,
                    )
                },
            )
        except Exception:
            pass
        return violation_result

    selected_transport = "direct"
    _safe_log_route_event(
        logger,
        event="transport_attempt",
        tool_id=tool_id,
        lane=execution_lane,
        selected_authority=selected_authority,
        selected_transport=selected_transport,
        fallback_reason=fallback_reason,
    )
    if logger:
        logger.log_operation(
            "INFO",
            f"EnhancedExecutor: Running tool {tool_id} with parameters {parameters}",
        )

    try:
        task_id = runtime_task_id_raw
        host_workspace_path = None
        if workspace_path:
            host_workspace_path = resolve_host_workspace_path(
                task_id=task_id,
                workspace_hint=workspace_path,
            )
        runtime_context = build_tool_runtime_context(
            task_id=runtime_task_id,
            tenant_id=runtime_tenant_id,
            workspace_path=workspace_path,
            host_workspace_path=host_workspace_path,
            container_workspace_path="/workspace",
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
            interrupt_id=interrupt_id,
        )

        if execution_lane == "backend_scoped":

            def _run_backend_scoped_direct() -> Any:
                with bind_tool_runtime_context(runtime_context):
                    return run_tool_by_name_fn(tool_id, parameters)

            result = await asyncio.wait_for(
                asyncio.to_thread(_run_backend_scoped_direct),
                timeout=timeout_plan.deadline_seconds,
            )
        else:
            def _run_artifact_scoped_direct() -> Any:
                with bind_tool_runtime_context(runtime_context):
                    if host_workspace_path:
                        with temporary_cwd(host_workspace_path):
                            return run_tool_by_name_fn(tool_id, parameters)
                    return run_tool_by_name_fn(tool_id, parameters)

            result = await asyncio.wait_for(
                asyncio.to_thread(_run_artifact_scoped_direct),
                timeout=timeout_plan.deadline_seconds,
            )

        if logger:
            logger.log_operation(
                "INFO",
                f"EnhancedExecutor: Tool {tool_id} completed with success={result.success}, exit_code={result.exit_code}",
            )
            if result.stdout:
                logger.log_operation(
                    "DEBUG",
                    f"EnhancedExecutor: Tool {tool_id} stdout: {result.stdout[:200]}...",
                )
            if result.stderr:
                logger.log_operation(
                    "DEBUG",
                    f"EnhancedExecutor: Tool {tool_id} stderr: {result.stderr[:200]}...",
                )
        enriched = enrich_direct_execution_result(
            tool_id=tool_id,
            parameters=parameters,
            result=result,
        )
        metadata = getattr(enriched, "metadata", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.setdefault("timeout_policy", timeout_plan.to_metadata())
        if getattr(enriched, "exit_code", None) == TOOL_TIMEOUT_EXIT_CODE:
            metadata.setdefault("failure_category", TOOL_TIMEOUT_FAILURE_CATEGORY)
            metadata.setdefault("timed_out", True)
            metadata.setdefault("killed", False)
        attach_execution_result_extras(enriched, metadata=metadata)
        return enriched
    except asyncio.TimeoutError:
        result = ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Tool {tool_id} timed out after {timeout_plan.deadline_seconds} seconds",
            exit_code=TOOL_TIMEOUT_EXIT_CODE,
        )
        attach_execution_result_extras(
            result,
            metadata={
                "failure_category": TOOL_TIMEOUT_FAILURE_CATEGORY,
                "timeout_policy": timeout_plan.to_metadata(),
                "timed_out": True,
                "killed": False,
            },
        )
        return result
    except Exception as exc:  # pragma: no cover - unexpected failures
        if logger:
            logger.log_operation("ERROR", f"Tool execution failed: {exc}")
        return ExecutionResult(False, "", str(exc), -1)


__all__ = [
    "build_pty_transport_command",
    "build_pty_tool_result",
    "execute_single_tool_with_fallback",
    "execute_via_pty_transport",
    "resolve_pty_enabled_cached",
    "should_use_pty",
    "tool_supports_pty",
]
