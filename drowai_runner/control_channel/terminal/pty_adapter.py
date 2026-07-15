"""Runner-local Docker exec PTY adapter for cloud terminal operations.

Owns the per-session Docker exec PTY sockets used to back cloud terminal
operations (open/input/output/resize/close). Talks only to the runner Docker
runtime; no protocol or websocket knowledge.
"""

from __future__ import annotations

import select

from drowai_runner.docker_runtime import RunnerDockerRuntime


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
        exec_id = client.api.exec_create(
            container.id,
            cmd="/bin/bash",
            tty=True,
            stdin=True,
            stdout=True,
            stderr=True,
            privileged=True,
            user="root",
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
        session = self._require_session(session_id)
        if "raw_socket" not in session:
            buffer = session["buffer"]
            assert isinstance(buffer, bytearray)
            chunk = bytes(buffer[:max_bytes])
            del buffer[:max_bytes]
            return chunk
        raw_sock = session["raw_socket"]
        try:
            readable, _, _ = select.select([raw_sock], [], [], 0.005)
        except Exception:
            readable = [raw_sock]
        if not readable:
            return b""
        try:
            return raw_sock.recv(max(1, max_bytes))
        except (BlockingIOError, TimeoutError):
            return b""

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

    def _require_session(self, session_id: str) -> dict[str, object]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id}")
        return session
