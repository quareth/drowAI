"""Runner-local Docker exec PTY adapter for cloud terminal operations.

Owns the per-session Docker exec PTY sockets used to back cloud terminal
operations (open/input/output/resize/close). Talks only to the runner Docker
runtime; no protocol or websocket knowledge.
"""

from __future__ import annotations

import select
from typing import Mapping

from drowai_runner.docker_runtime import RunnerDockerRuntime
from runtime_shared.docker_contracts import CONTAINER_WORKSPACE_PATH
from runtime_shared.terminal_contracts import TerminalReadResult


class _RunnerPtyAdapter:
    """Runner-local Docker exec PTY adapter used by cloud terminal operations."""

    def __init__(self, *, docker_runtime: RunnerDockerRuntime) -> None:
        self._docker_runtime = docker_runtime
        self._sessions: dict[str, dict[str, object]] = {}

    def open_session(
        self,
        *,
        container_id: str,
        session_id: str,
        cols: int,
        rows: int,
        command: str | None = None,
        cwd: str = CONTAINER_WORKSPACE_PATH,
        env: Mapping[str, str] | None = None,
        interactive: bool = True,
    ) -> None:
        client = self._docker_runtime._client()
        if not hasattr(client, "api"):
            self._docker_runtime.container_status(container_id)
            self._sessions[session_id] = {
                "container_id": container_id,
                "cols": cols,
                "rows": rows,
                "buffer": bytearray(),
            }
            return
        container = client.containers.get(container_id)
        exec_command: str | list[str] = "/bin/bash"
        if command is not None:
            exec_command = ["/bin/bash", "-lc", command]
        exec_id = client.api.exec_create(
            container.id,
            cmd=exec_command,
            tty=True,
            stdin=True,
            stdout=True,
            stderr=True,
            environment=dict(env or {}) or None,
            privileged=True,
            user="root",
            workdir=cwd or CONTAINER_WORKSPACE_PATH,
        )["Id"]
        sock = client.api.exec_start(
            exec_id,
            detach=False,
            tty=True,
            stream=True,
            socket=True,
            demux=False,
        )
        try:
            client.api.exec_resize(exec_id, height=max(rows, 10), width=max(cols, 20))
        except Exception:
            pass
        raw_sock = getattr(sock, "_sock", sock)
        try:
            raw_sock.setblocking(False)
        except Exception:
            pass
        self._sessions[session_id] = {
            "container_id": container_id,
            "exec_id": exec_id,
            "socket": sock,
            "raw_socket": raw_sock,
            "cols": cols,
            "rows": rows,
            "dedicated_command": command is not None,
            "interactive": bool(interactive),
        }

    def send_input(self, *, session_id: str, data: str) -> None:
        session = self._require_session(session_id)
        if "raw_socket" not in session:
            buffer = session["buffer"]
            assert isinstance(buffer, bytearray)
            buffer.extend(data.encode("utf-8"))
            return
        raw_sock = session["raw_socket"]
        payload = data.encode("utf-8")
        try:
            raw_sock.setblocking(True)
            raw_sock.settimeout(1.0)
        except Exception:
            pass
        raw_sock.sendall(payload)
        try:
            raw_sock.setblocking(False)
        except Exception:
            pass

    def read_output(self, *, session_id: str, max_bytes: int) -> bytes:
        return self.read_output_result(
            session_id=session_id,
            max_bytes=max_bytes,
        ).data

    def read_output_result(
        self,
        *,
        session_id: str,
        max_bytes: int,
    ) -> TerminalReadResult:
        session = self._require_session(session_id)
        if "raw_socket" not in session:
            buffer = session["buffer"]
            assert isinstance(buffer, bytearray)
            chunk = bytes(buffer[:max_bytes])
            del buffer[:max_bytes]
            return TerminalReadResult(
                ok=True,
                data=chunk,
                process_status="running",
            )
        raw_sock = session["raw_socket"]
        try:
            readable, _, _ = select.select([raw_sock], [], [], 0.005)
        except Exception:
            readable = [raw_sock]
        if not readable:
            return self._process_state_result(session)
        try:
            data = raw_sock.recv(max(1, max_bytes))
        except (BlockingIOError, TimeoutError):
            return self._process_state_result(session)
        if data:
            return TerminalReadResult(
                ok=True,
                data=data,
                process_status="running",
            )
        return self._process_state_result(session, socket_eof=True)

    def resize_session(self, *, session_id: str, cols: int, rows: int) -> None:
        session = self._require_session(session_id)
        client = self._docker_runtime._client()
        exec_id = session.get("exec_id")
        if isinstance(exec_id, str) and exec_id and hasattr(client, "api"):
            client.api.exec_resize(exec_id, height=max(rows, 10), width=max(cols, 20))
        session["cols"] = cols
        session["rows"] = rows

    def close_session(self, *, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            return
        raw_sock = session.get("raw_socket")
        close = getattr(raw_sock, "close", None)
        if callable(close):
            close()

    def _process_state_result(
        self,
        session: dict[str, object],
        *,
        socket_eof: bool = False,
    ) -> TerminalReadResult:
        if not bool(session.get("dedicated_command")):
            return TerminalReadResult(
                ok=True,
                eof=socket_eof,
                process_status="failed" if socket_eof else "running",
            )
        exec_id = session.get("exec_id")
        client = self._docker_runtime._client()
        if not isinstance(exec_id, str) or not exec_id or not hasattr(client, "api"):
            return TerminalReadResult(
                ok=True,
                eof=socket_eof,
                process_status="failed" if socket_eof else "running",
            )
        try:
            inspection = client.api.exec_inspect(exec_id)
        except Exception:
            return TerminalReadResult(
                ok=True,
                eof=socket_eof,
                process_status="failed" if socket_eof else "running",
            )
        if bool(inspection.get("Running")):
            return TerminalReadResult(ok=True, process_status="running")
        if not socket_eof:
            return TerminalReadResult(ok=True, process_status="running")
        exit_code_raw = inspection.get("ExitCode")
        try:
            exit_code = int(exit_code_raw) if exit_code_raw is not None else None
        except (TypeError, ValueError):
            exit_code = None
        return TerminalReadResult(
            ok=True,
            eof=True,
            process_status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
        )

    def _require_session(self, session_id: str) -> dict[str, object]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id}")
        return session
