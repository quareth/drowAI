"""File-based command/result transport and agent-side execution bridge helpers."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import uuid
from pydantic import ValidationError

from agent.tool_runtime.timeout_policy import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
    ToolTimeoutPlan,
    resolve_tool_timeout_plan,
)
from agent.tool_runtime.command_preparation import prepare_tool_command
from agent.tool_runtime.result_enrichment import build_command_transport_tool_result
from runtime_shared.file_comm_contracts import (
    CommandMessage,
    FileCommWorkspacePaths,
)
from runtime_shared.workspace_files import materialize_runtime_workspace_preparation

# Platform-specific imports for file locking
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # Windows doesn't have fcntl, we'll use file-based locking
    HAS_FCNTL = False

MAX_RETRIES = 3

if TYPE_CHECKING:
    from agent.models import ExecutionResult


class _FileLock:
    """Cross-platform file locking context manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[Any] = None
        self.lock_file = path.with_suffix(path.suffix + '.lock')

    def __enter__(self) -> Any:
        if HAS_FCNTL:
            # Unix-style fcntl locking
            self.fd = open(self.path, "a+")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self.fd
        else:
            # Windows-style file-based locking
            retry_count = 0
            while retry_count < MAX_RETRIES:
                try:
                    # Try to create lock file exclusively
                    self.fd = open(self.lock_file, "x")
                    # Open the actual file
                    actual_fd = open(self.path, "a+")
                    return actual_fd
                except FileExistsError:
                    # Lock file exists, wait and retry
                    time.sleep(0.1)
                    retry_count += 1
            raise OSError(f"Could not acquire lock for {self.path}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd:
            if HAS_FCNTL:
                # Unix-style fcntl unlocking
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
            else:
                # Windows-style: close and remove lock file
                try:
                    self.fd.close()
                    if self.lock_file.exists():
                        self.lock_file.unlink()
                except OSError:
                    pass


def _cleanup_file(file_path: Path, lock_path: Path, keep_ids: set[str]) -> None:
    if not file_path.exists():
        return
    with _FileLock(lock_path):
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        lines = [entry for entry in lines if entry.get("id") not in keep_ids]
        with open(file_path, "w", encoding="utf-8") as f:
            for entry in lines:
                f.write(json.dumps(entry) + "\n")


class FileCommAgent:
    """File-based communication interface for the agent."""

    def __init__(self, workspace_path: str = "/workspace") -> None:
        paths = FileCommWorkspacePaths.from_workspace(workspace_path)
        self.commands_file = paths.commands_file
        self.results_file = paths.results_file
        self.commands_lock = paths.commands_lock
        self.results_lock = paths.results_lock

        self._processed_result_ids: set[str] = set()
        self._result_lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    def _append_line(self, file_path: Path, lock_path: Path, line: str) -> None:
        os.makedirs(file_path.parent, exist_ok=True)
        for _ in range(MAX_RETRIES):
            try:
                with _FileLock(lock_path):
                    with open(file_path, "a", encoding="utf-8") as f:
                        f.write(line)
                return
            except OSError:
                time.sleep(0.1)
        raise OSError(f"Failed to append to {file_path}")

    def _read_all(self, file_path: Path, lock_path: Path) -> List[Dict[str, Any]]:
        if not file_path.exists():
            return []
        with _FileLock(lock_path):
            with open(file_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        data = []
        for line in lines:
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return data


    async def send_command(self, command: Dict[str, Any]) -> str:
        """Write a command to commands.jsonl and return its id."""
        cmd_id = command.get("id") or str(uuid.uuid4())
        command["id"] = cmd_id
        command.setdefault("timestamp", datetime.utcnow().isoformat())
        command.setdefault("timeout", DEFAULT_TOOL_TIMEOUT_SECONDS)
        self._logger.debug("[FileCommAgent] Command envelope queued for file-comm transport")

        try:
            CommandMessage.model_validate(command)
        except ValidationError as e:
            raise ValueError(f"Invalid command schema: {e}")

        line = json.dumps(command) + "\n"
        await asyncio.to_thread(self._append_line, self.commands_file, self.commands_lock, line)
        return cmd_id

    async def wait_for_result(
        self,
        command_id: str,
        timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Wait until a result with the given id appears or timeout occurs."""
        deadline = datetime.utcnow() + timedelta(seconds=timeout)
        while datetime.utcnow() < deadline:
            async with self._result_lock:
                results = await asyncio.to_thread(
                    self._read_all,
                    self.results_file,
                    self.results_lock,
                )
                for res in results:
                    if res.get("id") == command_id:
                        self._processed_result_ids.add(command_id)
                        await asyncio.to_thread(
                            _cleanup_file,
                            self.results_file,
                            self.results_lock,
                            {command_id},
                        )
                        return res
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Result for {command_id} not received")

    async def get_pending_results(self) -> List[Dict[str, Any]]:
        """Return all results currently in results.jsonl."""
        async with self._result_lock:
            results = await asyncio.to_thread(
                self._read_all,
                self.results_file,
                self.results_lock,
            )
            pending = [r for r in results if r.get("id") not in self._processed_result_ids]
            pending_ids = {str(r.get("id")) for r in pending if r.get("id")}
            self._processed_result_ids.update(pending_ids)
            if pending_ids:
                await asyncio.to_thread(
                    _cleanup_file,
                    self.results_file,
                    self.results_lock,
                    pending_ids,
                )
            return pending


async def execute_tool_via_file_comm(
    *,
    file_comm: Optional["FileCommAgent"],
    tool_id: str,
    args: Dict[str, Any],
    config: Any,
    logger: Any = None,
    timeout_seconds: Optional[float] = None,
    log_mode: str = "enhanced",
    include_metrics: bool = True,
    timeout_plan: Optional[ToolTimeoutPlan] = None,
    explicit_command_builder: Optional[Any] = None,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    artifact_stamp: Optional[int] = None,
) -> Optional["ExecutionResult"]:
    """Execute a tool through file-comm and normalize into ExecutionResult."""
    if file_comm is None:
        return None

    try:
        from ..models import ExecutionResult
        from ..tools.utils import (
            attach_execution_result_extras,
            resolve_command_text_for_execution,
            safe_inc_metric,
        )
        from ..tools.shell.contracts import ShellCommandResult
    except Exception:  # pragma: no cover - fallback for script-style imports
        try:
            from agent.models import ExecutionResult
            from agent.tools.utils import (
                attach_execution_result_extras,
                resolve_command_text_for_execution,
                safe_inc_metric,
            )
            from agent.tools.shell.contracts import ShellCommandResult
        except Exception:
            from models import ExecutionResult  # type: ignore
            from tools.utils import (  # type: ignore
                attach_execution_result_extras,
                resolve_command_text_for_execution,
                safe_inc_metric,
            )
            from tools.shell.contracts import ShellCommandResult  # type: ignore

    if timeout_plan is None or timeout_plan.tool_id != str(tool_id):
        timeout_plan = resolve_tool_timeout_plan(
            tool_id=tool_id,
            parameters=args,
            config=config,
            override_deadline_seconds=timeout_seconds,
        )

    if include_metrics:
        enhanced_keys = {"read_mode", "start_line", "num_lines", "start_byte", "max_bytes", "grep_pattern"}
        args_dict = dict(timeout_plan.normalized_parameters)
        if any(key in args_dict for key in enhanced_keys):
            safe_inc_metric("executor_file_comm_enhanced_params")
        else:
            safe_inc_metric("executor_file_comm_command_format")

    if log_mode == "planner":
        log_prefix = "[planner]"
    elif log_mode == "legacy":
        log_prefix = "File communication"
    else:
        log_prefix = "EnhancedExecutor: [file-comm]"

    if explicit_command_builder is None:
        raise ValueError("file-comm command execution requires an explicit command builder")

    prepared = await prepare_tool_command(
        tool_id=tool_id,
        parameters=args,
        config=config,
        logger=logger,
        transport="file-comm",
        explicit_command_builder=explicit_command_builder,
        interrupt_id=interrupt_id,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        artifact_stamp=artifact_stamp,
        timeout_plan=timeout_plan,
    )
    command = {
        "command": prepared.command,
        "cwd": "/workspace",
        "env": {},
        "timeout": prepared.timeout_plan.deadline_seconds,
        "timeout_policy": prepared.timeout_plan.to_metadata(),
    }
    materialize_runtime_workspace_preparation(
        workspace=prepared.host_workspace_path,
        files=prepared.pre_execution_workspace_files,
        directories=prepared.pre_execution_workspace_directories,
    )
    if logger:
        if log_mode == "legacy":
            logger.log_operation("INFO", f"Sending command via file comm: {prepared.safe_command}")
        else:
            logger.log_operation("INFO", f"{log_prefix} Sending command: {prepared.safe_command}")

    try:
        cmd_id = await file_comm.send_command(command)
        if logger:
            if log_mode == "legacy":
                logger.log_operation("INFO", f"Command sent with ID: {cmd_id}")
            else:
                logger.log_operation("INFO", f"{log_prefix} Command sent with ID: {cmd_id}")

        if logger and log_mode == "legacy":
            logger.log_operation(
                "INFO",
                f"Waiting for result with timeout: {timeout_plan.deadline_seconds}s",
            )
        result = await file_comm.wait_for_result(
            cmd_id,
            timeout=timeout_plan.deadline_seconds,
        )
        if logger:
            if log_mode == "legacy":
                logger.log_operation("INFO", f"Received result: {result}")
            else:
                logger.log_operation(
                    "INFO",
                    f"{log_prefix} Received result for {tool_id}: success={result.get('success')} exit={result.get('exit_code')}",
                )
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("timeout_policy", prepared.timeout_plan.to_metadata())
        if result.get("exit_code") == TOOL_TIMEOUT_EXIT_CODE:
            metadata.setdefault("failure_category", TOOL_TIMEOUT_FAILURE_CATEGORY)
            metadata.setdefault("timed_out", True)
            metadata.setdefault("killed", False)
        duration = float(result.get("execution_time") or 0.0)
        status = (
            "timeout"
            if result.get("exit_code") == TOOL_TIMEOUT_EXIT_CODE
            else "success"
            if result.get("success", False)
            else "error"
        )
        shell_result = ShellCommandResult(
            status=status,
            exit_code=int(result.get("exit_code", -1)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            duration_ms=max(0, int(duration * 1000)),
            transport="file-comm",
            truncated=False,
        )
        enriched = build_command_transport_tool_result(
            tool=prepared.tool,
            args=prepared.args,
            shell_result=shell_result,
            command=prepared.command,
            host_workspace_path=prepared.host_workspace_path,
            runtime_context=prepared.runtime_context,
            artifact_stamp=artifact_stamp,
            existing_metadata=metadata,
        )
        artifacts = result.get("artifacts", [])
        raw_artifacts = list(artifacts) if isinstance(artifacts, list) and artifacts else []
        enriched_artifacts = list(getattr(enriched, "artifacts", []) or [])
        merged_artifacts = []
        for artifact in [*raw_artifacts, *enriched_artifacts]:
            if artifact not in merged_artifacts:
                merged_artifacts.append(artifact)
        enriched_metadata = getattr(enriched, "metadata", None)
        enriched_metadata = dict(enriched_metadata) if isinstance(enriched_metadata, dict) else {}
        enriched_metadata.setdefault("timeout_policy", prepared.timeout_plan.to_metadata())
        if result.get("exit_code") == TOOL_TIMEOUT_EXIT_CODE:
            enriched_metadata.setdefault("failure_category", TOOL_TIMEOUT_FAILURE_CATEGORY)
            enriched_metadata.setdefault("timed_out", True)
            enriched_metadata.setdefault("killed", False)
        attach_execution_result_extras(
            enriched,
            metadata=enriched_metadata,
            artifacts=merged_artifacts or None,
            command_text=resolve_command_text_for_execution(
                tool_id,
                prepared.parameters,
                metadata,
            )
            or prepared.safe_command,
        )
        return enriched
    except TimeoutError:
        exec_result = ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Tool {tool_id} timed out after {timeout_plan.deadline_seconds} seconds",
            exit_code=TOOL_TIMEOUT_EXIT_CODE,
        )
        attach_execution_result_extras(
            exec_result,
            metadata={
                "failure_category": TOOL_TIMEOUT_FAILURE_CATEGORY,
                "timeout_policy": timeout_plan.to_metadata(),
                "timed_out": True,
                "killed": False,
            },
        )
        return exec_result
    except Exception as exc:  # pragma: no cover - defensive
        if logger:
            if log_mode == "legacy":
                logger.log_operation("ERROR", f"File communication failed: {exc}")
            elif log_mode == "planner":
                logger.log_operation("ERROR", f"[planner] FileComm tool execution failed: {exc}")
            else:
                logger.log_operation(
                    "ERROR",
                    f"{log_prefix} Execution failed for {tool_id}: {exc}",
                )
        return None
