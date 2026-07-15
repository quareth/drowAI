"""Tests for runner file-comm bridge command/result behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from drowai_runner.file_comm_bridge import (
    ERROR_FILE_COMM_TIMEOUT,
    ERROR_RESULT_MALFORMED,
    FileCommBridgeResult,
    RunnerFileCommBridge,
)
from runtime_shared.file_comm_contracts import FileCommWorkspacePaths
from runtime_shared.workspace_filesystem import WorkspaceEntryUnsafeError


@pytest.mark.parametrize("lock_name", ["commands.lock", "results.lock"])
def test_bridge_rejects_symlinked_lock_without_touching_target(
    tmp_path: Path, lock_name: str
) -> None:
    workspace = tmp_path / "task-symlink-lock"
    locks = workspace / "locks"
    locks.mkdir(parents=True)
    canary = tmp_path / "outside.lock"
    canary.write_bytes(b"canary")
    (locks / lock_name).symlink_to(canary)

    with pytest.raises(WorkspaceEntryUnsafeError):
        RunnerFileCommBridge(workspace, max_parallel_commands=1)

    assert canary.read_bytes() == b"canary"


def test_bridge_rejects_symlinked_lock_parent_without_touching_sibling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "task-parent-link"
    workspace.mkdir()
    sibling = tmp_path / "task-sibling"
    sibling.mkdir()
    (workspace / "locks").symlink_to(sibling)

    with pytest.raises(WorkspaceEntryUnsafeError):
        RunnerFileCommBridge(workspace, max_parallel_commands=1)

    assert list(sibling.iterdir()) == []


def _append_result(paths: FileCommWorkspacePaths, row: dict[str, object]) -> None:
    with paths.results_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _read_command_ids(paths: FileCommWorkspacePaths) -> list[str]:
    if not paths.commands_file.exists():
        return []
    command_ids: list[str] = []
    for line in paths.commands_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        command_ids.append(str(payload["id"]))
    return command_ids


def _read_command_payload(paths: FileCommWorkspacePaths, command_id: str) -> dict[str, object]:
    for line in paths.commands_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if str(payload.get("id")) == command_id:
            return payload
    raise AssertionError(f"Command payload not found: {command_id}")


def test_bridge_dispatches_command_and_returns_relative_artifacts(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-11"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=2, poll_interval_seconds=0.01)

        async def _writer() -> None:
            while "cmd-1" not in _read_command_ids(paths):
                await asyncio.sleep(0.01)
            command_payload = _read_command_payload(paths, "cmd-1")
            assert str(command_payload["command"]).startswith("nmap ")
            assert "scanme.nmap.org" in str(command_payload["command"])
            assert "tool" not in command_payload
            assert "args" not in command_payload
            assert command_payload["timeout_policy"] == {
                "deadline_seconds": 1.0,
                "grace_seconds": 0.25,
            }
            _append_result(
                paths,
                {
                    "id": "cmd-1",
                    "timestamp": "2026-05-22T10:00:00Z",
                    "success": True,
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "artifacts": [
                        str(workspace / "artifacts" / "scan.json"),
                        "/tmp/other-host-path.txt",
                        "logs/result.log",
                    ],
                    "execution_time": 0.2,
                    "metadata": {"family": "nmap"},
                },
            )

        writer = asyncio.create_task(_writer())
        result = await bridge.dispatch_command(
            command="nmap -T4 -n -oX - scanme.nmap.org",
            command_id="cmd-1",
            timeout_seconds=1.0,
            timeout_policy={"deadline_seconds": 1.0, "grace_seconds": 0.25},
        )
        await writer

        assert result.success is True
        assert result.status == "completed"
        assert result.artifacts == ("artifacts/scan.json", "logs/result.log")
        assert result.error_code is None

    asyncio.run(_run())


def test_bridge_returns_failure_result_from_executor_row(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-12"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=1, poll_interval_seconds=0.01)

        async def _writer() -> None:
            while "cmd-fail" not in _read_command_ids(paths):
                await asyncio.sleep(0.01)
            _append_result(
                paths,
                {
                    "id": "cmd-fail",
                    "timestamp": "2026-05-22T10:00:00Z",
                    "success": False,
                    "exit_code": 2,
                    "stdout": "",
                    "stderr": "tool failed",
                    "artifacts": [],
                    "execution_time": 0.05,
                    "metadata": {},
                },
            )

        writer = asyncio.create_task(_writer())
        result = await bridge.dispatch_command(
            command="printf fail",
            command_id="cmd-fail",
            timeout_seconds=1.0,
        )
        await writer

        assert result.success is False
        assert result.status == "completed"
        assert result.exit_code == 2
        assert result.error_code is None

    asyncio.run(_run())


def test_bridge_returns_stable_timeout_error_code(tmp_path: Path) -> None:
    async def _run() -> None:
        bridge = RunnerFileCommBridge(tmp_path / "task-13", max_parallel_commands=1, poll_interval_seconds=0.01)

        result = await bridge.dispatch_command(
            command="printf timeout",
            command_id="cmd-timeout",
            timeout_seconds=0.08,
        )

        assert result.success is False
        assert result.error_code == ERROR_FILE_COMM_TIMEOUT
        assert result.status == "failed"

    asyncio.run(_run())


def test_bridge_submit_and_status_poll_are_non_blocking(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-submit"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=1, poll_interval_seconds=0.01)

        submit = await bridge.submit_command(
            command="printf done",
            command_id="cmd-submit",
            timeout_seconds=1.0,
        )
        running = await bridge.get_command_status("cmd-submit")

        _append_result(
            paths,
            {
                "id": "cmd-submit",
                "timestamp": "2026-05-22T10:00:00Z",
                "success": True,
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
                "artifacts": [],
                "execution_time": 0.01,
                "metadata": {},
            },
        )
        completed = await bridge.get_command_status("cmd-submit")

        assert submit.status == "running"
        assert running.status == "running"
        assert completed.status == "completed"
        assert completed.stdout == "done"
        assert _read_command_ids(paths) == ["cmd-submit"]

    asyncio.run(_run())


def test_bridge_submit_is_idempotent_for_duplicate_command_id(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-submit-dupe"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=1, poll_interval_seconds=0.01)

        first = await bridge.submit_command(
            command="printf dupe",
            command_id="cmd-dupe",
        )
        second = await bridge.submit_command(
            command="printf dupe",
            command_id="cmd-dupe",
        )

        assert first.status == "running"
        assert second.status == "running"
        assert _read_command_ids(paths).count("cmd-dupe") == 1

    asyncio.run(_run())


def test_bridge_reports_running_after_executor_picks_up_command(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-picked-up"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=1, poll_interval_seconds=0.01)

        await bridge.submit_command(
            command="printf picked",
            command_id="cmd-picked-up",
            timeout_seconds=1.0,
        )
        # The runtime executor removes commands.jsonl rows when it accepts work.
        paths.commands_file.write_text("", encoding="utf-8")

        status = await bridge.get_command_status("cmd-picked-up")

        assert status.status == "running"
        assert status.error_code is None

    asyncio.run(_run())


def test_bridge_is_idempotent_for_duplicate_command_id(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-14"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=2, poll_interval_seconds=0.01)

        async def _writer() -> None:
            while "cmd-dupe" not in _read_command_ids(paths):
                await asyncio.sleep(0.01)
            _append_result(
                paths,
                {
                    "id": "cmd-dupe",
                    "timestamp": "2026-05-22T10:00:00Z",
                    "success": True,
                    "exit_code": 0,
                    "stdout": "done",
                    "stderr": "",
                    "artifacts": [],
                    "execution_time": 0.01,
                    "metadata": {},
                },
            )

        writer = asyncio.create_task(_writer())
        first = await bridge.dispatch_command(
            command="printf dupe",
            command_id="cmd-dupe",
        )
        second = await bridge.dispatch_command(
            command="printf dupe",
            command_id="cmd-dupe",
        )
        await writer

        assert first == second
        assert _read_command_ids(paths).count("cmd-dupe") == 1

    asyncio.run(_run())


def test_bridge_handles_malformed_result_rows_with_stable_error(tmp_path: Path) -> None:
    async def _run() -> None:
        workspace = tmp_path / "task-15"
        paths = FileCommWorkspacePaths.from_workspace(workspace)
        bridge = RunnerFileCommBridge(workspace, max_parallel_commands=1, poll_interval_seconds=0.01)

        async def _writer() -> None:
            while "cmd-bad" not in _read_command_ids(paths):
                await asyncio.sleep(0.01)
            # Missing required fields for ResultMessage validation.
            _append_result(paths, {"id": "cmd-bad", "success": True})

        writer = asyncio.create_task(_writer())
        result = await bridge.dispatch_command(
            command="printf bad",
            command_id="cmd-bad",
        )
        await writer

        assert result.success is False
        assert result.error_code == ERROR_RESULT_MALFORMED
        assert result.status == "failed"

    asyncio.run(_run())


def test_bridge_respects_max_parallel_commands_per_task(tmp_path: Path) -> None:
    async def _run() -> None:
        bridge = RunnerFileCommBridge(
            tmp_path / "task-16",
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )
        active = 0
        peak_active = 0
        lock = asyncio.Lock()

        async def _fake_execute(
            *,
            command_id: str,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_seconds: float,
            timeout_policy: dict[str, object] | None = None,
        ):
            del command, cwd, env, timeout_seconds, timeout_policy
            nonlocal active, peak_active
            async with lock:
                active += 1
                peak_active = max(peak_active, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return FileCommBridgeResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout=command_id,
                stderr="",
            )

        bridge._execute_command = _fake_execute  # type: ignore[assignment]

        await asyncio.gather(
            bridge.dispatch_command(
                command="printf a",
                command_id="cmd-a",
            ),
            bridge.dispatch_command(
                command="printf b",
                command_id="cmd-b",
            ),
        )

        assert peak_active == 1

    asyncio.run(_run())
