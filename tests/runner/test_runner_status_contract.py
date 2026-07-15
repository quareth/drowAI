"""Tests for runner raw process status labeling in the status contract fix."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from drowai_runner.control_channel.tool_commands.result_payload import (
    _build_tooling_plane_tool_result_payload,
)
from drowai_runner.file_comm_bridge import RunnerFileCommBridge
from drowai_runner.pty_command_transport import RunnerPtyCommandTransport
from drowai_runner.terminal_proxy import TerminalProxyResponse
from runtime_shared.runner_protocol import RUNNER_TOOL_RESULT_COMPLETED_STATUS


class _FakeTerminalProxy:
    def __init__(self, *, output: str, session_id: str = "session-1") -> None:
        self.output = output
        self.session_id = session_id

    def open_terminal_session(self, **kwargs) -> TerminalProxyResponse:  # noqa: ANN003
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={"session_id": self.session_id},
        )

    def send_terminal_input(self, **kwargs) -> TerminalProxyResponse:  # noqa: ANN003
        return TerminalProxyResponse(accepted=True, status="succeeded")

    def read_terminal_output(self, **kwargs) -> TerminalProxyResponse:  # noqa: ANN003
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={"session_id": self.session_id, "output": self.output},
        )

    def close_terminal_session(self, **kwargs) -> TerminalProxyResponse:  # noqa: ANN003
        return TerminalProxyResponse(accepted=True, status="succeeded")


def test_pty_labels_nonzero_exit_as_completed_process(tmp_path: Path) -> None:
    async def _run() -> None:
        output = (
            "__DROWAI_START_cmd_fping__\n"
            "172.17.0.1 is alive\n"
            "__DROWAI_EXIT_CODE_cmd_fping__=1\n"
        )
        transport = RunnerPtyCommandTransport(
            terminal_proxy=_FakeTerminalProxy(output=output),  # type: ignore[arg-type]
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )
        await transport.submit_command(
            runtime_job_id="runtime-1",
            command="fping 172.17.0.1",
            timeout_seconds=1,
            command_id="cmd-fping",
            cleanup_session=True,
        )
        status = await transport.get_command_status("cmd-fping")
        for _ in range(100):
            if status.status != "running":
                break
            await asyncio.sleep(0.01)
            status = await transport.get_command_status("cmd-fping")

        assert status.status == "completed"
        assert status.success is False
        assert status.exit_code == 1

    asyncio.run(_run())


def test_file_comm_maps_nonzero_exit_to_completed_process(tmp_path: Path) -> None:
    async def _run() -> None:
        bridge = RunnerFileCommBridge(
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )
        results_path = tmp_path / "results.jsonl"
        results_path.write_text(
            '{"id":"cmd-fc","timestamp":"2026-05-22T10:00:00Z","success":false,"exit_code":1,"stdout":"alive","stderr":"","artifacts":[],"execution_time":0.1,"metadata":{}}\n',
            encoding="utf-8",
        )
        result = bridge._result_from_row(
            "cmd-fc",
            {
                "id": "cmd-fc",
                "timestamp": "2026-05-22T10:00:00Z",
                "success": False,
                "exit_code": 1,
                "stdout": "alive",
                "stderr": "",
                "artifacts": [],
                "execution_time": 0.1,
                "metadata": {},
            },
        )
        assert result.status == "completed"
        assert result.success is False
        assert result.exit_code == 1

    asyncio.run(_run())


def test_file_comm_maps_executor_timeout_to_timed_out(tmp_path: Path) -> None:
    bridge = RunnerFileCommBridge(
        workspace_path=tmp_path,
        max_parallel_commands=1,
        poll_interval_seconds=0.01,
    )
    result = bridge._result_from_row(
        "cmd-timeout",
        {
            "id": "cmd-timeout",
            "timestamp": "2026-05-22T10:00:00Z",
            "success": False,
            "exit_code": -2,
            "stdout": "",
            "stderr": "timed out",
            "artifacts": [],
            "execution_time": 0.2,
            "metadata": {"failure_category": "tool_timeout"},
        },
    )
    assert result.status == "timed_out"
    assert result.success is False
    assert result.exit_code == -2


def test_cloud_client_emits_completed_wire_status_for_finished_process() -> None:
    payload = _build_tooling_plane_tool_result_payload(
        inbound=SimpleNamespace(  # type: ignore[arg-type]
            payload=SimpleNamespace(
                operation_id="op-1",
                command_id="cmd-1",
                tool="fping",
                task_runtime_job_id="task-start-1",
                workspace_id="task-60",
                command="fping 172.17.0.1",
                tool_call_id="call-1",
                tool_batch_id="batch-1",
            )
        ),
        response={
            "accepted": True,
            "status": "completed",
            "metadata": {
                "exit_code": 1,
                "stdout": "alive",
                "stderr": "",
                "artifacts": [],
                "success": False,
            },
        },
    )
    assert payload.status == RUNNER_TOOL_RESULT_COMPLETED_STATUS
    assert payload.success is False
    assert payload.exit_code == 1
    assert payload.metadata["process_success"] is False
    assert payload.metadata["process_exit_code"] == 1


def test_drowai_runner_does_not_import_agent() -> None:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "drowai_runner"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from agent" in text or "import agent" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
