"""PTY selection and orchestration helpers for runtime tool execution.

This module owns PTY feature-flag checks, tool capability checks, PTY command
synthesis for shell/filesystem transports, and PTY execution orchestration. It
does not own lane policy sequencing and it does not own direct or file-comm routing.
"""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from runtime_shared.terminal_manager_port import get_terminal_session_manager
from runtime_shared.workspace_files import materialize_runtime_workspace_preparation

try:
    from ..tools.filesystem._helpers import build_pty_filesystem_command
except Exception:  # pragma: no cover
    from agent.tools.filesystem._helpers import build_pty_filesystem_command

from .command_preparation import prepare_tool_command
from .result_enrichment import (
    build_command_transport_tool_result,
    include_stderr_in_artifacts_for_tool,
)
from .timeout_policy import (
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
    ToolTimeoutPlan,
)

if TYPE_CHECKING:
    from agent.models import ExecutionResult


def _get_terminal_session_manager():
    """Resolve terminal manager through runtime-shared adapter boundary."""
    return get_terminal_session_manager()


def _include_stderr_in_artifacts(tool_id: str) -> bool:
    """Return whether PTY artifact creation should receive raw stderr."""
    return include_stderr_in_artifacts_for_tool(tool_id)


def resolve_pty_enabled_cached(cached_value: Optional[bool], *, logger: Any = None) -> bool:
    """Return cached PTY flag state or compute and log the first-read status."""
    if cached_value is None:
        enabled = os.getenv("ENABLE_PTY_EXECUTION", "false").lower() == "true"
        if logger:
            if enabled:
                logger.log_operation("INFO", "[PTY] PTY execution enabled via feature flag")
            else:
                logger.log_operation("DEBUG", "[PTY] PTY execution disabled via feature flag")
        return enabled
    return cached_value


def tool_supports_pty(
    tool_id: str,
    *,
    get_tool_fn: Optional[Callable[[str], Any]] = None,
) -> bool:
    """Return whether ``tool_id`` supports PTY execution."""
    if tool_id in ("shell.exec", "shell.script"):
        return True
    if tool_id.startswith("filesystem."):
        return True

    try:
        resolver = get_tool_fn
        if resolver is None:
            from agent.tools.tool_registry import get_tool as resolver
        tool_cls = resolver(tool_id)
        tool = tool_cls()
        return tool.supports_pty()
    except Exception:
        return False


def should_use_pty(
    tool_id: str,
    parameters: Dict[str, Any],
    *,
    is_pty_enabled_fn: Callable[[], bool],
    tool_supports_pty_fn: Callable[[str], bool],
    logger: Any = None,
) -> bool:
    """Decide whether a tool call should route through PTY."""
    try:
        from ..tools.utils import safe_inc_metric
    except Exception:  # pragma: no cover
        from agent.tools.utils import safe_inc_metric

    if not is_pty_enabled_fn():
        return False

    if not tool_supports_pty_fn(tool_id):
        if logger:
            logger.log_operation("DEBUG", f"[PTY] Tool {tool_id} does not support PTY execution")
        safe_inc_metric("executor_pty_unsupported")
        return False

    transport = parameters.get("transport")

    if tool_id.startswith("filesystem.") and transport != "pty":
        if logger:
            logger.log_operation(
                "DEBUG",
                f"[PTY] Filesystem tool {tool_id} requires transport=pty for PTY execution",
            )
        return False

    if tool_id == "filesystem.read_file":
        encoding_in_params = "encoding" in parameters
        encoding = parameters.get("encoding")
        if not encoding_in_params:
            encoding = "utf-8"

        start_byte = parameters.get("start_byte") or 0
        max_bytes_param = parameters.get("max_bytes")
        default_max_bytes = 200_000
        try:
            start_byte_int = int(start_byte)
        except Exception:
            start_byte_int = 0
        try:
            max_bytes_int = (
                int(max_bytes_param) if max_bytes_param is not None else default_max_bytes
            )
        except Exception:
            max_bytes_int = default_max_bytes

        if encoding is None and (start_byte_int > 0 or max_bytes_int < default_max_bytes):
            if logger:
                logger.log_operation(
                    "WARNING",
                    "[PTY] filesystem.read_file binary byte-range requested; falling back to non-PTY transport",
                )
            safe_inc_metric("executor_pty_read_binary_skip")
            return False

    if transport in ("direct", "file-comm", "file"):
        if logger:
            logger.log_operation(
                "DEBUG",
                f"[PTY] Explicit transport={transport} requested, skipping PTY",
            )
        return False

    return True


def build_pty_transport_command(
    tool_id: str,
    parameters: Dict[str, Any],
    *,
    resolve_container_path_fn: Callable[[str], str],
    logger: Any = None,
) -> str:
    """Build the shell command used by container transports for shell/filesystem tools."""
    if tool_id == "shell.exec":
        if logger:
            logger.log_operation(
                "DEBUG",
                f"[PTY] Using explicit command builder for {tool_id}",
            )
        return parameters["command"]

    if tool_id == "shell.script":
        if logger:
            logger.log_operation(
                "DEBUG",
                f"[PTY] Using explicit command builder for {tool_id}",
            )
        script = parameters["script"]
        return f"bash -c {shlex.quote(script)}"

    if tool_id.startswith("filesystem."):
        return build_pty_filesystem_command(
            tool_id,
            parameters,
            resolve_container_path=resolve_container_path_fn,
            logger=logger,
        )

    raise ValueError(
        f"Tool {tool_id} does not support PTY execution. "
        f"PTY is only available for shell (shell.exec, shell.script) and filesystem (filesystem.*) tools."
    )


def _record_agent_command(task_id: int, command: str, session_name: Optional[str] = None) -> None:
    """Persist a sanitized PTY command in the agent terminal session history."""
    try:
        terminal_session_manager = _get_terminal_session_manager()
        terminal_session_manager.record_agent_command(
            int(task_id),
            command,
            session_name=session_name,
        )
    except Exception:
        pass


def _pty_session_kwargs(
    *,
    session_name: Optional[str],
) -> Dict[str, Any]:
    """Return optional PTY session kwargs without forcing test fakes to accept them."""
    if not session_name:
        return {}
    return {"session_name": session_name}


async def _close_named_pty_session(*, task_id: int, session_name: str) -> None:
    """Best-effort cleanup for internal named PTY sessions."""
    try:
        from runtime_shared.terminal_contracts import build_named_agent_session_id
        terminal_session_manager = _get_terminal_session_manager()
        await terminal_session_manager.close_session(
            build_named_agent_session_id(task_id, session_name)
        )
    except Exception:
        pass


async def execute_via_pty_transport(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    config: Any,
    logger: Any = None,
    tool_to_shell_command_fn: Callable[[str, Dict[str, Any]], str],
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    session_name: Optional[str] = None,
    cleanup_session: bool = False,
    artifact_stamp: Optional[int] = None,
    timeout_plan: Optional[ToolTimeoutPlan] = None,
) -> "ExecutionResult":
    """Execute a tool call via PTY transport and return normalized output."""
    from agent.tools.shell._pty_executor import PTYSessionNotAvailable, execute_via_pty

    if logger:
        logger.log_operation("INFO", f"[PTY] Preparing tool {tool_id} for PTY execution")

    task_id_for_cleanup: Optional[int] = None
    try:
        prepared = await prepare_tool_command(
            tool_id=tool_id,
            parameters=parameters,
            config=config,
            logger=logger,
            transport="pty",
            explicit_command_builder=tool_to_shell_command_fn,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
            timeout_plan=timeout_plan,
        )
        task_id_for_cleanup = prepared.task_id
        materialize_runtime_workspace_preparation(
            workspace=prepared.host_workspace_path,
            files=prepared.pre_execution_workspace_files,
            directories=prepared.pre_execution_workspace_directories,
        )
        if logger:
            logger.log_operation("INFO", f"[PTY] Executing tool command: {prepared.safe_command[:200]}")

        shell_result = await execute_via_pty(
            command=prepared.command,
            task_id=prepared.task_id,
            timeout_sec=prepared.timeout_plan.deadline_seconds,
            workspace_path=getattr(prepared.runtime_context, "container_workspace_path", None)
            or "/workspace",
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            **_pty_session_kwargs(
                session_name=session_name,
            ),
        )

        _record_agent_command(prepared.task_id, prepared.safe_command, session_name=session_name)

        result = build_command_transport_tool_result(
            tool=prepared.tool,
            args=prepared.args,
            shell_result=shell_result,
            command=prepared.command,
            host_workspace_path=prepared.host_workspace_path,
            runtime_context=prepared.runtime_context,
            include_stderr_in_artifacts=_include_stderr_in_artifacts(tool_id),
            artifact_stamp=artifact_stamp,
        )
        metadata = getattr(result, "metadata", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.setdefault("timeout_policy", prepared.timeout_plan.to_metadata())
        if getattr(result, "exit_code", None) == TOOL_TIMEOUT_EXIT_CODE:
            metadata.setdefault("failure_category", TOOL_TIMEOUT_FAILURE_CATEGORY)
            metadata.setdefault("timed_out", True)
            metadata.setdefault("killed", False)
        from agent.tools.utils import attach_execution_result_extras

        attach_execution_result_extras(result, metadata=metadata)

        if logger and not result.stdout:
            logger.log_operation(
                "WARNING",
                f"[PTY] Returning empty stdout for {tool_id}, exit_code={result.exit_code}",
            )
        return result

    except PTYSessionNotAvailable as exc:
        if logger:
            logger.log_operation("WARNING", f"[PTY] Session not available: {exc}")
        raise
    except ValueError as exc:
        if logger:
            logger.log_operation("WARNING", f"[PTY] Tool not supported or validation failed: {exc}")
        raise
    except Exception as exc:
        if logger:
            logger.log_operation("ERROR", f"[PTY] Execution failed: {exc}")
        raise
    finally:
        if cleanup_session and session_name and task_id_for_cleanup is not None:
            await _close_named_pty_session(
                task_id=task_id_for_cleanup,
                session_name=session_name,
            )


__all__ = [
    "build_pty_transport_command",
    "execute_via_pty_transport",
    "resolve_pty_enabled_cached",
    "should_use_pty",
    "tool_supports_pty",
]
