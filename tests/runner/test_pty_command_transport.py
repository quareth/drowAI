"""Tests for runner PTY command submit/status behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

from drowai_runner.pty_command_transport import RunnerPtyCommandTransport, _extract_stdout
from drowai_runner.terminal_proxy import TerminalProxyResponse


class _FakeTerminalProxy:
    def __init__(self, *, output: str = "", session_id: str = "session-1") -> None:
        self.output = output
        self.session_id = session_id
        self.sent: list[str] = []
        self.closed: list[str] = []
        self.read_count = 0

    def open_terminal_session(
        self,
        *,
        runtime_job_id: str,
        session_name: str = "terminal",
        cols: int = 120,
        rows: int = 30,
    ) -> TerminalProxyResponse:
        del runtime_job_id, session_name, cols, rows
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={"session_id": self.session_id},
        )

    def send_terminal_input(self, *, session_id: str, data: str) -> TerminalProxyResponse:
        del session_id
        self.sent.append(data)
        return TerminalProxyResponse(accepted=True, status="succeeded")

    def read_terminal_output(
        self,
        *,
        session_id: str,
        max_bytes: int = 32768,
    ) -> TerminalProxyResponse:
        del session_id, max_bytes
        self.read_count += 1
        output = self.output if self.read_count == 1 else ""
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={"session_id": self.session_id, "output": output},
        )

    def close_terminal_session(self, *, session_id: str) -> TerminalProxyResponse:
        self.closed.append(session_id)
        return TerminalProxyResponse(accepted=True, status="succeeded")


def test_pty_transport_submit_then_status_returns_terminal_result(tmp_path: Path) -> None:
    async def _run() -> None:
        output = "echoed\n__DROWAI_START_cmd_pty__\nok\n__DROWAI_EXIT_CODE_cmd_pty__=0\n"
        terminal = _FakeTerminalProxy(output=output)
        transport = RunnerPtyCommandTransport(
            terminal_proxy=terminal,  # type: ignore[arg-type]
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )

        submitted = await transport.submit_command(
            runtime_job_id="runtime-1",
            command="echo ok",
            timeout_seconds=1,
            command_id="cmd-pty",
            cleanup_session=True,
        )
        await asyncio.sleep(0.02)
        status = await transport.get_command_status("cmd-pty")

        assert submitted.status == "running"
        assert status.status == "completed"
        assert status.success is True
        assert status.stdout == "ok"
        assert status.artifacts == ()
        assert status.metadata["command_text"] == "echo ok"
        assert terminal.closed == ["session-1"]

    asyncio.run(_run())


def test_pty_transport_timeout_keeps_partial_output(tmp_path: Path) -> None:
    async def _run() -> None:
        terminal = _FakeTerminalProxy(output="__DROWAI_START_cmd_slow__\npartial\n")
        transport = RunnerPtyCommandTransport(
            terminal_proxy=terminal,  # type: ignore[arg-type]
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )

        await transport.submit_command(
            runtime_job_id="runtime-1",
            command="sleep 10",
            timeout_seconds=0.03,
            command_id="cmd-slow",
            cleanup_session=True,
        )
        await asyncio.sleep(0.08)
        status = await transport.get_command_status("cmd-slow")

        assert status.status == "timed_out"
        assert status.success is False
        assert "partial" in status.stdout
        assert status.error_code == "PTY_COMMAND_TIMEOUT"
        assert terminal.closed == ["session-1"]

    asyncio.run(_run())


def test_pty_transport_skips_workspace_artifacts_for_read_only_tools(tmp_path: Path) -> None:
    async def _run() -> None:
        output = (
            "echoed\n"
            "__DROWAI_START_cmd_read__\n"
            "file content\n"
            "__DROWAI_EXIT_CODE_cmd_read__=0\n"
        )
        terminal = _FakeTerminalProxy(output=output)
        transport = RunnerPtyCommandTransport(
            terminal_proxy=terminal,  # type: ignore[arg-type]
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )

        await transport.submit_command(
            runtime_job_id="runtime-1",
            command="cat artifacts/example.txt",
            timeout_seconds=1,
            command_id="cmd-read",
            cleanup_session=True,
        )
        status = await transport.get_command_status("cmd-read")
        for _ in range(100):
            if status.status != "running":
                break
            await asyncio.sleep(0.01)
            status = await transport.get_command_status("cmd-read")

        assert status.status == "completed"
        assert status.stdout == "file content"
        assert status.artifacts == ()
        assert not list((tmp_path / "artifacts").glob("*"))
        assert not list((tmp_path / "index").glob("chunks_*.jsonl"))

    asyncio.run(_run())


def test_pty_stdout_extraction_ignores_echoed_wrapper_marker() -> None:
    start_marker = "__DROWAI_START_cmd_nmap__"
    exit_marker = "__DROWAI_EXIT_CODE_cmd_nmap__=0"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<nmaprun>\n"
        '<host><status state="up"/><address addr="127.0.0.1" addrtype="ipv4"/></host>\n'
        '<runstats><hosts up="1" down="0" total="1"/></runstats>\n'
        "</nmaprun>\n"
    )
    raw = (
        f"printf '\\n{start_marker}\\n'; nmap -oX - 127.0.0.1; "
        f"printf '\\n{exit_marker}\\n'\r\n"
        "\x1b]0;root@container: /workspace\x07┌──(root㉿container)-[/workspace]\r\n"
        f"{start_marker}\r\n"
        f"{xml}\r\n"
        f"{exit_marker}\r\n"
    )

    stdout = _extract_stdout(raw, start_marker, raw.index(exit_marker, raw.index(xml)))

    assert stdout.startswith('<?xml version="1.0"')
    assert "printf" not in stdout
    assert "root@container" not in stdout


def test_pty_transport_returns_prepared_command_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _run() -> None:
        monkeypatch.chdir(tmp_path)
        start_marker = "__DROWAI_START_cmd_nmap__"
        exit_marker = "__DROWAI_EXIT_CODE_cmd_nmap__=0"
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<nmaprun>\n"
            '<host><status state="up"/><address addr="127.0.0.1" addrtype="ipv4"/>'
            '<ports><port protocol="tcp" portid="5432"><state state="open"/>'
            '<service name="postgresql"/></port></ports></host>\n'
            '<runstats><hosts up="1" down="0" total="1"/></runstats>\n'
            "</nmaprun>\n"
        )
        output = (
            f"printf '\\n{start_marker}\\n'; nmap -T4 -p 5432 -sV -oX - 127.0.0.1; "
            f"printf '\\n{exit_marker}\\n'\r\n"
            "\x1b]0;root@container: /workspace\x07┌──(root㉿container)-[/workspace]\r\n"
            f"{start_marker}\r\n"
            f"{xml}\r\n"
            f"{exit_marker}\r\n"
        )
        terminal = _FakeTerminalProxy(output=output)
        transport = RunnerPtyCommandTransport(
            terminal_proxy=terminal,  # type: ignore[arg-type]
            workspace_path=tmp_path,
            max_parallel_commands=1,
            poll_interval_seconds=0.01,
        )

        await transport.submit_command(
            runtime_job_id="runtime-1",
            command="nmap -T4 -p 5432 -sV -oX - 127.0.0.1",
            timeout_seconds=1,
            command_id="cmd-nmap",
            cleanup_session=True,
        )
        status = await transport.get_command_status("cmd-nmap")
        for _ in range(100):
            if status.status != "running":
                break
            await asyncio.sleep(0.01)
            status = await transport.get_command_status("cmd-nmap")

        assert status.status == "completed"
        assert status.stdout.startswith('<?xml version="1.0"')
        assert status.metadata["command_text"] == "nmap -T4 -p 5432 -sV -oX - 127.0.0.1"

    asyncio.run(_run())
