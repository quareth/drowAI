"""End-to-end test for enhanced read_file via file-comm."""

import asyncio
import os

from backend.services.workspace.manager import WorkspaceManager
from agent.communication.file_comm import FileCommAgent
from kali_executor.communication.file_comm import FileCommExecutor
from unittest.mock import patch, MagicMock


async def _run_enhanced_read():
    manager = WorkspaceManager()
    task_id = 7001
    workspace = manager.create_workspace(task_id)

    file_path = os.path.join(workspace, "sample.txt")
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("line1\nline2\nline3\nline4\n")

    agent_comm = FileCommAgent(str(workspace))
    executor_comm = FileCommExecutor(str(workspace))

    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        cmd_id = await agent_comm.send_command(
            {
                "command": "sed -n '2,3p' sample.txt",
                "cwd": "/workspace",
            }
        )

        pending = await executor_comm.get_pending_commands()
        assert any(cmd["id"] == cmd_id for cmd in pending)

        with open(file_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        stdout = "".join(lines[1:3])
        metadata = {"total_lines": len(lines), "lines_read": 2, "mode": "range"}

        await executor_comm.send_result(
            cmd_id,
            {
                "success": True,
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "artifacts": [],
                "execution_time": 0.01,
                "metadata": metadata,
            },
        )

        result = await agent_comm.wait_for_result(cmd_id, timeout=2)
        assert result["success"]
        assert "line2" in result["stdout"]
        assert result["metadata"]["mode"] == "range"
        assert result["metadata"]["lines_read"] == 2

    manager.cleanup_workspace(task_id, archive_first=False)


def test_enhanced_read_file_via_file_comm():
    """Test enhanced read_file parameters through file-comm."""
    asyncio.run(_run_enhanced_read())
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.workspace.manager import WorkspaceManager
from agent.executor import CommandExecutor
from agent.models import Action, ActionType
from agent.communication.file_comm import FileCommAgent
from kali_executor.communication.file_comm import FileCommExecutor
from kali_executor.executor_daemon import process_commands_once
from agent.tools import BaseTool, BaseToolArgs, ToolResult, register_tool

class EchoArgs(BaseToolArgs):
    target: str
    message: str

class EchoTool(BaseTool):
    args_model = EchoArgs

    def run(self, args: EchoArgs) -> ToolResult:
        return ToolResult(success=True, exit_code=0, stdout=args.message, stderr="", artifacts=[], metadata={}, execution_time=0.0)


def test_command_executor_file_mode():
    async def run_test():
        manager = WorkspaceManager()
        workspace = manager.create_workspace(555)
        os.environ["WORKSPACE"] = str(workspace)
        os.environ["EXECUTION_MODE"] = "file"

        register_tool("echo_tool", EchoTool)
        agent_comm = FileCommAgent(str(workspace))
        exec_comm = FileCommExecutor(str(workspace))
        cfg = type("Cfg", (), {"nmap_timeout": 5, "openai_api_key": "test-key", "model_name": "gpt-4"})()
        with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
            mock_client.return_value = MagicMock()
            executor = CommandExecutor(cfg)
            executor.set_file_comm(agent_comm)

            async def patched_execute(action: Action):
                cmd_id = await agent_comm.send_command({"command": f"printf {action.target}"})
                await process_commands_once(exec_comm, str(workspace))
                return await agent_comm.wait_for_result(cmd_id, timeout=2)

            executor._execute_via_comm = patched_execute  # type: ignore

            action = Action(type=ActionType.SCAN_PORTS, target="hello", parameters={}, reasoning="", expected_outcome="")
            result = await executor._execute_via_comm(action)
            assert result["stdout"] == "hello"

        manager.cleanup_workspace(555, archive_first=False)

    asyncio.run(run_test())
