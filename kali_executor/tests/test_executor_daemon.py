"""Regression tests for executor daemon command processing behavior."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from backend.services.workspace.manager import WorkspaceManager
from backend.services.runtime_provider.local_file_comm_cancel import append_file_comm_cancellations
from agent.communication.file_comm import FileCommAgent
from kali_executor.communication.file_comm import FileCommExecutor
from kali_executor.executor_daemon import process_commands_once


def test_executor_processes_command():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(999)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        cmd_id = await agent_comm.send_command({"command": "printf hi"})
        await process_commands_once(exec_comm, str(workspace))
        result = await agent_comm.wait_for_result(cmd_id, timeout=2)
        assert result["stdout"] == "hi"
        assert (Path(workspace) / "artifacts").exists()
        assert result["artifacts"] == []

        manager.cleanup_workspace(999, archive_first=False)

    asyncio.run(run_test())


def test_executor_runs_commands_in_non_login_shell():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(998)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        cmd_id = await agent_comm.send_command(
            {"command": "shopt -q login_shell && printf login || printf non-login"}
        )
        await process_commands_once(exec_comm, str(workspace))
        result = await agent_comm.wait_for_result(cmd_id, timeout=2)

        assert result["success"] is True
        assert result["stdout"] == "non-login"

        manager.cleanup_workspace(998, archive_first=False)

    asyncio.run(run_test())


def test_executor_reports_tool_errors_without_crashing():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(1000)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        cmd_id = await agent_comm.send_command(
            {"command": "printf boom >&2; exit 7"}
        )
        await process_commands_once(exec_comm, str(workspace))
        result = await agent_comm.wait_for_result(cmd_id, timeout=2)

        assert result["success"] is False
        assert result["exit_code"] == 7
        assert "boom" in result["stderr"]

        manager.cleanup_workspace(1000, archive_first=False)

    asyncio.run(run_test())


def test_executor_processes_pending_commands_with_bounded_concurrency():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(1001)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        command_ids = [
            await agent_comm.send_command(
                {
                    "command": f"sleep 0.2; printf msg-{idx}",
                }
            )
            for idx in range(3)
        ]

        started = time.perf_counter()
        await process_commands_once(
            exec_comm,
            str(workspace),
            max_concurrent_commands=3,
        )
        elapsed = time.perf_counter() - started

        results = await asyncio.gather(
            *(agent_comm.wait_for_result(cmd_id, timeout=2) for cmd_id in command_ids)
        )
        assert {result["stdout"] for result in results} == {"msg-0", "msg-1", "msg-2"}
        assert elapsed < 0.45

        manager.cleanup_workspace(1001, archive_first=False)

    asyncio.run(run_test())


def test_executor_kills_timed_out_tool_and_continues_queue():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(1002)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        slow_id = await agent_comm.send_command(
            {
                "command": "sleep 2; printf late",
                "timeout": 0.1,
            }
        )
        fast_id = await agent_comm.send_command(
            {
                "command": "printf after-timeout",
                "timeout": 2,
            }
        )

        started = time.perf_counter()
        await process_commands_once(
            exec_comm,
            str(workspace),
            max_concurrent_commands=1,
        )
        elapsed = time.perf_counter() - started

        slow_result = await agent_comm.wait_for_result(slow_id, timeout=2)
        fast_result = await agent_comm.wait_for_result(fast_id, timeout=2)

        assert elapsed < 1.5
        assert slow_result["success"] is False
        assert slow_result["exit_code"] == -2
        assert slow_result["metadata"]["failure_category"] == "tool_timeout"
        assert slow_result["metadata"]["timed_out"] is True
        assert slow_result["metadata"]["killed"] is True
        assert fast_result["success"] is True
        assert fast_result["stdout"] == "after-timeout"

        manager.cleanup_workspace(1002, archive_first=False)

    asyncio.run(run_test())


def test_executor_cancels_active_file_comm_command():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(1003)

        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))

        cmd_id = await agent_comm.send_command(
            {
                "command": "sleep 5; printf late",
                "timeout": 10,
            }
        )

        started = time.perf_counter()
        task = asyncio.create_task(process_commands_once(exec_comm, str(workspace)))
        await asyncio.sleep(0.2)
        append_file_comm_cancellations(
            workspace_path=workspace,
            command_ids=[cmd_id],
            reason="user_stop",
        )
        await asyncio.wait_for(task, timeout=3)
        elapsed = time.perf_counter() - started

        result = await agent_comm.wait_for_result(cmd_id, timeout=2)

        assert elapsed < 3
        assert result["success"] is False
        assert result["metadata"]["failure_category"] == "user_cancelled"
        assert result["metadata"]["cancel_requested"] is True
        assert result["metadata"]["killed"] is True

        manager.cleanup_workspace(1003, archive_first=False)

    asyncio.run(run_test())


def test_executor_runtime_info_flag_outputs_manifest_and_exits() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kali_executor.executor_daemon",
            "--runtime-info",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads((result.stdout or "").strip())
    assert payload["runtime_contract_version"]
    assert payload["file_comm_schema_version"]
    assert payload["workspace_layout_version"]
    assert payload["semantic_schema_versions"]
    assert payload["supported_tool_families"]


def test_executor_version_flag_outputs_manifest_and_exits() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kali_executor.executor_daemon",
            "--version",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads((result.stdout or "").strip())
    assert payload["runtime_contract_version"]
