"""Tests for runner-owned Docker exec command lifecycle observation."""

from __future__ import annotations

from types import SimpleNamespace

from drowai_runner.control_channel.terminal import pty_adapter as adapter_module
from drowai_runner.control_channel.terminal.pty_adapter import _RunnerPtyAdapter


class _RawSocket:
    def __init__(self) -> None:
        self.chunks = [b"live\n", b""]
        self.sent: list[bytes] = []
        self.closed = False

    def setblocking(self, value: bool) -> None:
        del value

    def recv(self, size: int) -> bytes:
        del size
        return self.chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class _Api:
    def __init__(self, raw_socket: _RawSocket) -> None:
        self.raw_socket = raw_socket
        self.created: dict[str, object] | None = None
        self.inspect_running = False

    def exec_create(self, container_id: str, **kwargs):
        self.created = {"container_id": container_id, **kwargs}
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kwargs):
        del exec_id, kwargs
        return SimpleNamespace(_sock=self.raw_socket)

    def exec_resize(self, *args, **kwargs) -> None:
        del args, kwargs

    def exec_inspect(self, exec_id: str):
        assert exec_id == "exec-1"
        return {"Running": self.inspect_running, "ExitCode": 0}


def test_runner_adapter_starts_exact_dedicated_exec_and_reports_final_exit(
    monkeypatch,
) -> None:
    raw_socket = _RawSocket()
    api = _Api(raw_socket)
    client = SimpleNamespace(
        api=api,
        containers=SimpleNamespace(get=lambda _container_id: SimpleNamespace(id="cid-1")),
    )
    runtime = SimpleNamespace(_client=lambda: client)
    adapter = _RunnerPtyAdapter(docker_runtime=runtime)
    monkeypatch.setattr(
        adapter_module.select,
        "select",
        lambda *_args, **_kwargs: ([raw_socket], [], []),
    )

    adapter.open_session(
        container_id="cid-1",
        session_id="session-1",
        cols=120,
        rows=30,
        command="printf hello",
        cwd="/workspace/results",
        env={"APP_MODE": "test"},
        interactive=False,
    )
    first = adapter.read_output_result(session_id="session-1", max_bytes=4096)
    final = adapter.read_output_result(session_id="session-1", max_bytes=4096)

    assert api.created is not None
    assert api.created["cmd"] == ["/bin/bash", "-lc", "printf hello"]
    assert api.created["workdir"] == "/workspace/results"
    assert api.created["environment"] == {"APP_MODE": "test"}
    assert first.data == b"live\n"
    assert first.eof is False
    assert final.eof is True
    assert final.process_status == "completed"
    assert final.exit_code == 0


def test_runner_adapter_reports_stopped_exec_before_socket_eof(monkeypatch) -> None:
    raw_socket = _RawSocket()
    api = _Api(raw_socket)
    client = SimpleNamespace(
        api=api,
        containers=SimpleNamespace(get=lambda _container_id: SimpleNamespace(id="cid-1")),
    )
    adapter = _RunnerPtyAdapter(docker_runtime=SimpleNamespace(_client=lambda: client))
    monkeypatch.setattr(
        adapter_module.select,
        "select",
        lambda *_args, **_kwargs: ([], [], []),
    )

    adapter.open_session(
        container_id="cid-1",
        session_id="session-1",
        cols=120,
        rows=30,
        command="printf hello",
        interactive=False,
    )
    result = adapter.read_output_result(session_id="session-1", max_bytes=4096)

    assert result.eof is False
    assert result.process_status == "running"


def test_runner_adapter_reports_terminal_after_bounded_drain_grace(monkeypatch) -> None:
    raw_socket = _RawSocket()
    api = _Api(raw_socket)
    client = SimpleNamespace(
        api=api,
        containers=SimpleNamespace(get=lambda _container_id: SimpleNamespace(id="cid-1")),
    )
    adapter = _RunnerPtyAdapter(docker_runtime=SimpleNamespace(_client=lambda: client))
    observed_times = iter((10.0, 10.051))
    monkeypatch.setattr(
        adapter_module.select,
        "select",
        lambda *_args, **_kwargs: ([], [], []),
    )
    monkeypatch.setattr(
        adapter_module.time,
        "monotonic",
        lambda: next(observed_times),
    )

    adapter.open_session(
        container_id="cid-1",
        session_id="session-1",
        cols=120,
        rows=30,
        command="printf hello",
        interactive=False,
    )
    draining = adapter.read_output_result(session_id="session-1", max_bytes=4096)
    terminal = adapter.read_output_result(session_id="session-1", max_bytes=4096)

    assert draining.eof is False
    assert terminal.eof is True
    assert terminal.process_status == "completed"
    assert terminal.exit_code == 0


def test_runner_adapter_drains_tail_after_exec_stops(monkeypatch) -> None:
    raw_socket = _RawSocket()
    raw_socket.chunks = [b"tail\n", b""]
    api = _Api(raw_socket)
    client = SimpleNamespace(
        api=api,
        containers=SimpleNamespace(get=lambda _container_id: SimpleNamespace(id="cid-1")),
    )
    adapter = _RunnerPtyAdapter(docker_runtime=SimpleNamespace(_client=lambda: client))
    readiness = iter((False, True, True))
    monkeypatch.setattr(
        adapter_module.select,
        "select",
        lambda *_args, **_kwargs: (
            ([raw_socket], [], []) if next(readiness) else ([], [], [])
        ),
    )

    adapter.open_session(
        container_id="cid-1",
        session_id="session-1",
        cols=120,
        rows=30,
        command="printf tail",
        interactive=False,
    )
    stopped = adapter.read_output_result(session_id="session-1", max_bytes=4096)
    tail = adapter.read_output_result(session_id="session-1", max_bytes=4096)
    terminal = adapter.read_output_result(session_id="session-1", max_bytes=4096)

    assert stopped.eof is False
    assert tail.data == b"tail\n"
    assert tail.eof is False
    assert terminal.eof is True
    assert terminal.process_status == "completed"
    assert terminal.exit_code == 0
