"""JSONL file-comm round-trip regression (agent + executor sides)."""

import asyncio

import pytest

from agent.communication.file_comm import FileCommAgent
from backend.services.workspace.manager import WorkspaceManager
from kali_executor.communication.file_comm import FileCommExecutor

pytestmark = pytest.mark.execution_plane_non_dind_regression

async def run_comm():
    manager = WorkspaceManager()
    task_id = 98765
    workspace = manager.create_workspace(task_id)

    agent_comm = FileCommAgent(workspace)
    executor_comm = FileCommExecutor(workspace)

    cmd_id = await agent_comm.send_command({"command": "printf hi", "timeout": 1})
    commands = await executor_comm.get_pending_commands()
    assert any(c["id"] == cmd_id for c in commands)

    await executor_comm.send_result(cmd_id, {"success": True, "exit_code": 0, "stdout": "hi", "stderr": "", "artifacts": [], "execution_time": 0.1})
    result = await agent_comm.wait_for_result(cmd_id, timeout=2)
    assert result["stdout"] == "hi"

    manager.cleanup_workspace(task_id, archive_first=False)


def test_file_comm_basic():
    asyncio.run(run_comm())


async def _setup_comm(task_id: int):
    manager = WorkspaceManager()
    workspace = manager.create_workspace(task_id)
    agent_comm = FileCommAgent(workspace)
    executor_comm = FileCommExecutor(workspace)
    return manager, workspace, agent_comm, executor_comm


async def _run_file_comm_with_read_modes():
    """Test file-comm command envelopes for read-mode-equivalent commands."""
    manager, workspace, agent_comm, executor_comm = await _setup_comm(1001)
    commands = [
        "head -n 5 file.txt",
        "tail -n 7 file.txt",
        "sed -n '10,12p' file.txt",
        "grep ERROR file.txt",
        "dd if=binary.dat bs=1 skip=5 count=10 2>/dev/null",
    ]
    ids = []
    for command in commands:
        cmd_id = await agent_comm.send_command({"command": command})
        ids.append(cmd_id)

    pending = await executor_comm.get_pending_commands()
    assert len(pending) == len(commands)
    assert set(ids) == {cmd["id"] for cmd in pending}
    manager.cleanup_workspace(1001, archive_first=False)


async def _run_file_comm_command_envelope():
    """Test command envelopes are accepted without tool metadata."""
    manager, workspace, agent_comm, executor_comm = await _setup_comm(1002)
    cmd_id = await agent_comm.send_command({"command": "printf command-envelope"})
    pending = await executor_comm.get_pending_commands()
    assert any(cmd["id"] == cmd_id for cmd in pending)
    manager.cleanup_workspace(1002, archive_first=False)


async def _run_file_comm_line_range_parameters():
    """Test line-oriented range parameters."""
    manager, workspace, agent_comm, executor_comm = await _setup_comm(1003)
    cmd_id = await agent_comm.send_command(
        {
            "command": "sed -n '2,5p' log.txt",
        }
    )
    pending = await executor_comm.get_pending_commands()
    assert any(cmd["id"] == cmd_id for cmd in pending)
    assert pending[0]["command"] == "sed -n '2,5p' log.txt"
    manager.cleanup_workspace(1003, archive_first=False)


async def _run_file_comm_byte_range_parameters():
    """Test byte-oriented range parameters."""
    manager, workspace, agent_comm, executor_comm = await _setup_comm(1004)
    cmd_id = await agent_comm.send_command(
        {
            "command": "dd if=binary.dat bs=1 skip=1 count=32 2>/dev/null",
        }
    )
    pending = await executor_comm.get_pending_commands()
    assert any(cmd["id"] == cmd_id for cmd in pending)
    cmd = next(c for c in pending if c["id"] == cmd_id)
    assert "skip=1" in cmd["command"]
    assert "count=32" in cmd["command"]
    manager.cleanup_workspace(1004, archive_first=False)


async def _run_concurrent_waits_receive_matching_results():
    manager, workspace, agent_comm, executor_comm = await _setup_comm(1005)
    first_id = await agent_comm.send_command({"command": "printf first"})
    second_id = await agent_comm.send_command({"command": "printf second"})

    await executor_comm.send_result(
        second_id,
        {
            "success": True,
            "exit_code": 0,
            "stdout": "second",
            "stderr": "",
            "artifacts": [],
            "execution_time": 0.1,
        },
    )
    await executor_comm.send_result(
        first_id,
        {
            "success": True,
            "exit_code": 0,
            "stdout": "first",
            "stderr": "",
            "artifacts": [],
            "execution_time": 0.1,
        },
    )

    first, second = await asyncio.gather(
        agent_comm.wait_for_result(first_id, timeout=2),
        agent_comm.wait_for_result(second_id, timeout=2),
    )
    assert first["id"] == first_id
    assert first["stdout"] == "first"
    assert second["id"] == second_id
    assert second["stdout"] == "second"

    manager.cleanup_workspace(1005, archive_first=False)


def test_file_comm_enhanced_parameters():
    asyncio.run(_run_file_comm_with_read_modes())
    asyncio.run(_run_file_comm_command_envelope())
    asyncio.run(_run_file_comm_line_range_parameters())
    asyncio.run(_run_file_comm_byte_range_parameters())


def test_file_comm_concurrent_waits_receive_matching_results():
    asyncio.run(_run_concurrent_waits_receive_matching_results())
