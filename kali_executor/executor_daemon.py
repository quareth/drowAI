"""Kali runtime command daemon and runtime metadata probe entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from runtime_shared.file_comm_contracts import (
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
)
from runtime_shared.runtime_manifest import build_runtime_manifest

if TYPE_CHECKING:
    from kali_executor.communication.file_comm import FileCommExecutor

logger = logging.getLogger(__name__)

RUNNING = True
DEFAULT_MAX_CONCURRENT_COMMANDS = 3


def _runtime_dependencies() -> Any:
    """Load transport dependency lazily for metadata-only probes."""
    from kali_executor.communication.file_comm import FileCommExecutor

    return FileCommExecutor


def _handle_stop(signum, frame) -> None:
    global RUNNING
    RUNNING = False


def _resolve_max_concurrent_commands(value: Optional[int] = None) -> int:
    if value is None:
        raw = os.getenv("KALI_EXECUTOR_MAX_CONCURRENT_COMMANDS", "")
        try:
            value = int(raw) if raw.strip() else DEFAULT_MAX_CONCURRENT_COMMANDS
        except ValueError:
            value = DEFAULT_MAX_CONCURRENT_COMMANDS
    return max(1, int(value))


def _coerce_deadline(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 600.0
    return parsed if parsed > 0 else 600.0


def _resolve_cwd(workspace: str, cwd: Any) -> str:
    workspace_path = Path(workspace).resolve()
    if cwd in (None, ""):
        return str(workspace_path)

    cwd_text = str(cwd)
    if cwd_text == "/workspace":
        return str(workspace_path)
    if cwd_text.startswith("/workspace/"):
        raw = workspace_path / cwd_text.removeprefix("/workspace/")
    else:
        raw = Path(cwd_text)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (workspace_path / raw).resolve()

    try:
        candidate.relative_to(workspace_path)
    except ValueError as exc:
        raise ValueError("file-comm cwd must stay inside /workspace") from exc
    return str(candidate)


def _coerce_env(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    env: Dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        env[str(key)] = str(item)
    return env


def _terminate_process_group(process: subprocess.Popen[str], *, force_after_seconds: float = 2.0) -> tuple[str, str, bool]:
    """Terminate one process group and return collected output."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=force_after_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()
        stdout, stderr = process.communicate(timeout=1)
    return stdout or "", stderr or "", process.poll() is not None


def _run_prepared_command(
    cmd: Dict[str, Any],
    workspace: str,
    should_cancel: Callable[[], bool] | None = None,
) -> Dict[str, Any]:
    command = str(cmd.get("command") or "").strip()
    if not command:
        return {
            "success": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": "file-comm command payload requires a non-empty command",
            "artifacts": [],
            "execution_time": 0.0,
            "metadata": {"error_code": "missing_command"},
        }

    start = time.time()
    timeout = _coerce_deadline(cmd.get("timeout"))
    cwd = _resolve_cwd(workspace, cmd.get("cwd"))
    env = os.environ.copy()
    env.update(_coerce_env(cmd.get("env")))

    process = subprocess.Popen(
        ["bash", "-c", command],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    cancelled = False
    killed = False
    try:
        while True:
            if should_cancel is not None and should_cancel():
                cancelled = True
                stdout, stderr, killed = _terminate_process_group(process)
                break
            elapsed = time.time() - start
            remaining = timeout - elapsed
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr, killed = _terminate_process_group(process)

    duration = time.time() - start
    exit_code = process.returncode
    metadata: Dict[str, Any] = {}
    if cancelled:
        exit_code = int(exit_code if exit_code is not None else -15)
        metadata.update(
            {
                "failure_category": "user_cancelled",
                "cancelled": True,
                "cancel_requested": True,
                "killed": killed,
            }
        )
        if not stderr:
            stderr = "Command cancelled by user stop request"
    elif timed_out:
        exit_code = TOOL_TIMEOUT_EXIT_CODE
        metadata.update(
            {
                "failure_category": TOOL_TIMEOUT_FAILURE_CATEGORY,
                "timed_out": True,
                "killed": killed,
                "timeout_policy": cmd.get("timeout_policy")
                or {"deadline_seconds": timeout},
            }
        )
        if not stderr:
            stderr = f"Command timed out after {timeout} seconds"

    return {
        "success": exit_code == 0,
        "exit_code": int(exit_code if exit_code is not None else -1),
        "stdout": stdout or "",
        "stderr": stderr or "",
        "artifacts": [],
        "execution_time": duration,
        "metadata": metadata,
    }


async def _execute_command(comm: FileCommExecutor, cmd: Dict[str, Any], workspace: str) -> None:
    cmd_id = cmd.get("id")
    try:
        result_payload = await asyncio.to_thread(
            _run_prepared_command,
            cmd,
            workspace,
            lambda: comm.is_cancel_requested_sync(str(cmd_id or "")),
        )
    except Exception as exc:
        logger.exception("Prepared command failed")
        result_payload = {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "artifacts": [],
            "execution_time": 0.0,
            "metadata": {"exception_type": type(exc).__name__},
        }

    await comm.send_result(cmd_id, result_payload)
    await comm.acknowledge_cancellation(str(cmd_id or ""))


async def process_commands_once(
    comm: FileCommExecutor,
    workspace: str,
    *,
    max_concurrent_commands: Optional[int] = None,
) -> None:
    os.makedirs(Path(workspace) / "artifacts", exist_ok=True)
    commands = await comm.get_pending_commands()
    if not commands:
        return

    concurrency = _resolve_max_concurrent_commands(max_concurrent_commands)
    if concurrency == 1 or len(commands) == 1:
        for cmd in commands:
            await _execute_command(comm, cmd, workspace)
        return

    semaphore = asyncio.Semaphore(concurrency)

    async def _run(cmd: Dict[str, Any]) -> None:
        async with semaphore:
            await _execute_command(comm, cmd, workspace)

    await asyncio.gather(*(_run(cmd) for cmd in commands))


async def run_daemon(
    workspace: str = "/workspace",
    *,
    max_concurrent_commands: Optional[int] = None,
) -> None:
    FileCommExecutor = _runtime_dependencies()
    comm = FileCommExecutor(workspace)
    logger.info("Executor daemon started for %s", workspace)
    while RUNNING:
        await process_commands_once(
            comm,
            workspace,
            max_concurrent_commands=max_concurrent_commands,
        )
        await asyncio.sleep(0.5)
    logger.info("Executor daemon stopping")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kali Executor Daemon")
    parser.add_argument("--workspace", type=str, default=os.environ.get("WORKSPACE", "/workspace"))
    parser.add_argument(
        "--max-concurrent-commands",
        type=int,
        default=None,
        help="Maximum queued tool commands to execute concurrently (default: 3 or KALI_EXECUTOR_MAX_CONCURRENT_COMMANDS).",
    )
    parser.add_argument(
        "--runtime-info",
        action="store_true",
        help="Print runtime manifest metadata and exit.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print runtime manifest metadata and exit.",
    )
    args = parser.parse_args()

    if args.runtime_info or args.version:
        print(json.dumps(build_runtime_manifest().to_dict(), sort_keys=True))
        return

    signal.signal(signal.SIGTERM, _handle_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_stop)

    asyncio.run(
        run_daemon(
            args.workspace,
            max_concurrent_commands=args.max_concurrent_commands,
        )
    )


if __name__ == "__main__":
    main()
