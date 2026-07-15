"""Concurrency regression tests for command-envelope file-comm execution."""

import asyncio

from backend.services.workspace.manager import WorkspaceManager
from agent.communication.file_comm import FileCommAgent
from kali_executor.communication.file_comm import FileCommExecutor
from kali_executor.executor_daemon import process_commands_once

async def run_workspace(task_id: int, message: str, fail: bool = False):
    manager = WorkspaceManager()
    workspace = manager.create_workspace(task_id)
    agent_comm = FileCommAgent(str(workspace))
    executor_comm = FileCommExecutor(str(workspace))

    command = "missing-command-for-negative-path" if fail else f"printf {message!r}"
    cmd_id = await agent_comm.send_command({"command": command})
    await process_commands_once(executor_comm, str(workspace))
    result = await agent_comm.wait_for_result(cmd_id, timeout=2)
    manager.cleanup_workspace(task_id, archive_first=False)
    return result

def test_file_comm_concurrency():
    async def run_test():
        tasks = [run_workspace(i, f"msg{i}") for i in range(1, 6)]
        tasks.append(run_workspace(99, "fail", True))
        results = await asyncio.gather(*tasks)
        for i in range(5):
            assert results[i]["stdout"] == f"msg{i+1}"
            assert results[i]["success"]
        assert not results[-1]["success"]
    asyncio.run(run_test())


def test_file_comm_command_envelopes_with_concurrent_workspaces():
    """Test concurrent command envelopes across task workspaces."""
    async def run_test():
        enhanced = [
            run_workspace(200 + i, f"enhanced{i}", False)
            for i in range(3)
        ]
        additional = [
            run_workspace(300 + i, f"command{i}", False)
            for i in range(2)
        ]
        results = await asyncio.gather(*enhanced, *additional)
        texts = [r["stdout"] for r in results]
        assert "enhanced0" in texts
        assert "command0" in texts
    asyncio.run(run_test())
