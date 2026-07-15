"""Migration parity checks for direct and command-envelope file execution."""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.workspace.manager import WorkspaceManager
from agent.executor import CommandExecutor
from agent.models import Action, ActionType, ExecutionResult
from agent.communication.file_comm import FileCommAgent
from kali_executor.communication.file_comm import FileCommExecutor
from kali_executor.executor_daemon import process_commands_once


def test_migration_validation():
    async def run_test():
        previous_workspace = os.environ.get("WORKSPACE")
        previous_execution_mode = os.environ.get("EXECUTION_MODE")
        os.environ.pop("EXECUTION_MODE", None)
        action = Action(
            type=ActionType.SCAN_PORTS,
            target="1.2.3.4",
            parameters={},
            reasoning="",
            expected_outcome="",
        )

        # Direct execution path with stubbed nmap
        executor_direct = CommandExecutor(type("Cfg", (), {"nmap_timeout": 5})())

        async def stub_scan(self, target: str) -> ExecutionResult:
            return ExecutionResult(True, f"scanned {target}", "", 0)

        executor_direct._execute_nmap_scan = stub_scan.__get__(
            executor_direct, CommandExecutor
        )
        direct_result = await executor_direct._execute_nmap_scan(action.target)

        # File-based execution path
        manager = WorkspaceManager()
        workspace = manager.create_workspace(777)
        try:
            os.environ["WORKSPACE"] = str(workspace)
            os.environ["EXECUTION_MODE"] = "file"

            agent_comm = FileCommAgent(str(workspace))
            exec_comm = FileCommExecutor(str(workspace))
            executor_file = CommandExecutor(type("Cfg", (), {"nmap_timeout": 5})())
            executor_file.set_file_comm(agent_comm)

            async def patched_execute(action: Action) -> ExecutionResult:
                cmd_id = await agent_comm.send_command(
                    {"command": f"printf 'scanned {action.target}'"}
                )
                await process_commands_once(exec_comm, str(workspace))
                result = await agent_comm.wait_for_result(cmd_id, timeout=2)
                return ExecutionResult(
                    result.get("success", False),
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                    result.get("exit_code", -1),
                )

            executor_file._execute_via_comm = patched_execute  # type: ignore
            file_result = await executor_file.execute_action(action)

            assert direct_result.stdout == file_result.stdout
            assert direct_result.exit_code == file_result.exit_code
        finally:
            manager.cleanup_workspace(777, archive_first=False)
            if previous_workspace is None:
                os.environ.pop("WORKSPACE", None)
            else:
                os.environ["WORKSPACE"] = previous_workspace
            if previous_execution_mode is None:
                os.environ.pop("EXECUTION_MODE", None)
            else:
                os.environ["EXECUTION_MODE"] = previous_execution_mode

    asyncio.run(run_test())
