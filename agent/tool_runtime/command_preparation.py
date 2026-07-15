"""Prepare tool commands once for every container transport.

This module owns backend-side command preparation for transports that execute
inside a task runtime. PTY and file-comm both call this path so validation,
workspace translation, command construction, and runtime context binding do
not fork by transport.
"""

from __future__ import annotations

import os
import shlex
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .runtime_context import bind_tool_runtime_context, build_tool_runtime_context
from .timeout_policy import ToolTimeoutPlan, resolve_tool_timeout_plan
from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    RuntimeWorkspaceFile,
    RuntimeWorkspacePreparation,
    normalize_runtime_workspace_directories,
    normalize_runtime_workspace_files,
)


_COMMAND_BUILD_ENV_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class PreparedToolCommand:
    """Validated command payload plus tool context for post-execution enrichment."""

    tool_id: str
    parameters: Dict[str, Any]
    timeout_plan: ToolTimeoutPlan
    command: str
    safe_command: str
    tool: Any
    args: Any
    host_workspace_path: str
    runtime_context: Any
    task_id: int
    workspace_path: Optional[str]
    pre_execution_workspace_files: tuple[RuntimeWorkspaceFile, ...]
    pre_execution_workspace_directories: tuple[RuntimeWorkspaceDirectory, ...]


@contextmanager
def _temporary_command_build_env(
    workspace_path: Optional[str],
    *,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    artifact_stamp: Optional[int] = None,
):
    """Temporarily bind runtime env for command-build side effects."""

    updates: Dict[str, str] = {}
    if workspace_path:
        updates["WORKSPACE"] = str(workspace_path)
    if tool_call_id:
        updates["DROWAI_TOOL_CALL_ID"] = str(tool_call_id)
    if tool_batch_id:
        updates["DROWAI_TOOL_BATCH_ID"] = str(tool_batch_id)
    if artifact_stamp is not None:
        updates["DROWAI_ARTIFACT_STAMP"] = str(artifact_stamp)

    previous = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _build_command_with_runtime_env(
    tool: Any,
    args: Any,
    *,
    workspace_path: Optional[str],
    tool_call_id: Optional[str],
    tool_batch_id: Optional[str],
    artifact_stamp: Optional[int],
    runtime_context: Optional[Any],
) -> list[str]:
    """Serialize process-env mutation while leaving execution parallel."""

    with _COMMAND_BUILD_ENV_LOCK:
        with bind_tool_runtime_context(runtime_context), _temporary_command_build_env(
            workspace_path,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
        ):
            return list(tool.build_command(args))


async def _prepare_workspace_preparation_with_runtime_env(
    tool: Any,
    args: Any,
    *,
    workspace_path: Optional[str],
    tool_call_id: Optional[str],
    tool_batch_id: Optional[str],
    artifact_stamp: Optional[int],
    runtime_context: Optional[Any],
) -> RuntimeWorkspacePreparation:
    """Serialize workspace preparation with the same runtime env as command build."""

    with _COMMAND_BUILD_ENV_LOCK:
        with bind_tool_runtime_context(runtime_context), _temporary_command_build_env(
            workspace_path,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
        ):
            return RuntimeWorkspacePreparation(
                files=normalize_runtime_workspace_files(tool.prepare_workspace_files(args)),
                directories=normalize_runtime_workspace_directories(
                    tool.prepare_workspace_directories(args)
                ),
            )


def _set_transport(args: Any, transport: str) -> None:
    """Best-effort transport annotation for existing tool argument models."""
    if not hasattr(args, "transport"):
        return
    try:
        args.transport = transport
    except Exception:
        pass


async def prepare_tool_command(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    config: Any,
    logger: Any = None,
    transport: str,
    explicit_command_builder: Callable[[str, Dict[str, Any]], str],
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    artifact_stamp: Optional[int] = None,
    timeout_plan: Optional[ToolTimeoutPlan] = None,
) -> PreparedToolCommand:
    """Build the canonical shell command for a tool before transport dispatch."""
    from agent.tools.tool_registry import get_tool
    from agent.tools.utils import sanitize_command_text, safe_inc_metric
    from agent.utils.workspace_helpers import resolve_host_workspace_path

    if logger:
        logger.log_operation(
            "INFO",
            f"[command-prep] Preparing tool {tool_id} for {transport} execution",
        )

    if timeout_plan is None or timeout_plan.tool_id != str(tool_id):
        timeout_plan = resolve_tool_timeout_plan(
            tool_id=tool_id,
            parameters=parameters,
            config=config,
        )
    normalized_parameters = dict(timeout_plan.normalized_parameters)

    task_id = getattr(config, "task_id", normalized_parameters.get("task_id"))
    if not task_id:
        raise ValueError("task_id not available for container command execution")
    task_id_int = int(task_id)
    tenant_id = getattr(config, "tenant_id", normalized_parameters.get("tenant_id"))

    workspace_path = getattr(config, "workspace_path", normalized_parameters.get("workspace_path"))
    host_workspace_path = resolve_host_workspace_path(
        task_id=task_id,
        workspace_hint=workspace_path,
    )
    runtime_context = build_tool_runtime_context(
        task_id=task_id,
        tenant_id=tenant_id,
        workspace_path=workspace_path,
        host_workspace_path=host_workspace_path,
        container_workspace_path="/workspace",
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        artifact_stamp=artifact_stamp,
        interrupt_id=interrupt_id,
    )

    tool_cls = get_tool(tool_id)
    tool = tool_cls()
    args = None
    command = ""
    pre_execution_workspace_files: tuple[RuntimeWorkspaceFile, ...] = ()
    pre_execution_workspace_directories: tuple[RuntimeWorkspaceDirectory, ...] = ()

    if tool_id in ("shell.exec", "shell.script"):
        try:
            args = tool.args_model(**normalized_parameters)
            _set_transport(args, transport)
            if not tool.supports_pty():
                raise ValueError(f"Tool {tool_id} does not support command execution")
            if logger:
                logger.log_operation("INFO", f"[command-prep] Using tool.build_command() for {tool_id}")
            safe_inc_metric("executor_command_prep_tool_interface")
            command = shlex.join(
                await _build_command_with_runtime_env(
                    tool,
                    args,
                    workspace_path=host_workspace_path,
                    tool_call_id=tool_call_id,
                    tool_batch_id=tool_batch_id,
                    artifact_stamp=artifact_stamp,
                    runtime_context=runtime_context,
                )
            )
        except Exception:
            if logger:
                logger.log_operation(
                    "WARNING",
                    f"[command-prep] Using explicit command builder for {tool_id}",
                )
            safe_inc_metric("executor_command_prep_explicit_builder")
            command = explicit_command_builder(tool_id, normalized_parameters)
            args = tool.args_model(**normalized_parameters)
            _set_transport(args, transport)
    elif tool_id.startswith("filesystem."):
        safe_inc_metric("executor_command_prep_explicit_builder")
        command = explicit_command_builder(tool_id, normalized_parameters)
        args = tool.args_model(**normalized_parameters)
        _set_transport(args, transport)
    else:
        if not tool.supports_pty():
            raise ValueError(f"Tool {tool_id} does not support command execution")
        args = tool.args_model(**normalized_parameters)
        _set_transport(args, transport)
        command = shlex.join(
            await _build_command_with_runtime_env(
                tool,
                args,
                workspace_path=host_workspace_path,
                tool_call_id=tool_call_id,
                tool_batch_id=tool_batch_id,
                artifact_stamp=artifact_stamp,
                runtime_context=runtime_context,
            )
        )

    if args is not None:
        pre_execution_preparation = await _prepare_workspace_preparation_with_runtime_env(
            tool,
            args,
            workspace_path=host_workspace_path,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
            runtime_context=runtime_context,
        )
        pre_execution_workspace_files = pre_execution_preparation.files
        pre_execution_workspace_directories = pre_execution_preparation.directories

    safe_command = sanitize_command_text(command)
    return PreparedToolCommand(
        tool_id=str(tool_id),
        parameters=normalized_parameters,
        timeout_plan=timeout_plan,
        command=command,
        safe_command=safe_command,
        tool=tool,
        args=args,
        host_workspace_path=str(host_workspace_path),
        runtime_context=runtime_context,
        task_id=task_id_int,
        workspace_path=str(workspace_path) if workspace_path is not None else None,
        pre_execution_workspace_files=pre_execution_workspace_files,
        pre_execution_workspace_directories=pre_execution_workspace_directories,
    )


__all__ = ["PreparedToolCommand", "prepare_tool_command"]
