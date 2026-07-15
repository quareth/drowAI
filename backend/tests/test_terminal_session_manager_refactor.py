"""Characterization tests for the terminal session manager refactor.

These tests lock the public facade, shared contracts, cleanup policy, and
agent bootstrap behavior that must remain stable across the extraction.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from backend.core.time_utils import utc_now
from backend.services import terminal_session_manager as legacy_terminal_module
from backend.services.terminal.contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
    build_agent_session_id,
    build_named_agent_session_id,
)
from backend.services.terminal.manager import (
    TerminalSessionManager as ExtractedTerminalSessionManager,
    terminal_session_manager as extracted_terminal_session_manager,
)
from backend.services.terminal.models import TerminalSession as ExtractedTerminalSession
from backend.services.terminal_session_manager import TerminalSessionManager, TerminalSession
from backend.services.runtime_provider import RuntimeCallScope


def test_terminal_session_manager_facade_reexports_public_surface() -> None:
    """The legacy import path must keep exposing the same public objects."""
    assert legacy_terminal_module.TerminalSession is ExtractedTerminalSession
    assert legacy_terminal_module.TerminalSessionManager is ExtractedTerminalSessionManager
    assert (
        legacy_terminal_module.terminal_session_manager
        is extracted_terminal_session_manager
    )


def test_terminal_contracts_preserve_prompt_and_session_id_values() -> None:
    """Shared agent PTY contracts must stay byte-for-byte stable."""
    assert AGENT_PROMPT_MARKER == "__DROWAI_PROMPT__> "
    assert AGENT_PROMPT_ENV == "__DROWAI_PROMPT__>"
    assert build_agent_session_id(123) == "agent_task_123"
    assert build_named_agent_session_id(456, "my-session.name") == "agent_task_456_my_session_name"


@pytest.mark.asyncio
async def test_terminal_session_active_io_methods_are_disabled() -> None:
    """TerminalSession is a passive handle; manager/provider own active I/O."""
    session = TerminalSession(
        session_id="session-1",
        task_id=1,
        user_id=1,
        container_name="task-1",
        connection_type="docker_exec",
    )

    with pytest.raises(RuntimeError, match="TerminalSessionManager"):
        await session.write(b"x")
    with pytest.raises(RuntimeError, match="TerminalSessionManager"):
        await session.read(1)


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_uses_user_timeout() -> None:
    """User sessions should expire on the user timeout without closing newer agent sessions."""
    manager = TerminalSessionManager()
    manager.session_timeout = 1
    manager.agent_session_timeout = 10

    stale_user_session = TerminalSession(
        session_id="user-stale",
        task_id=1,
        user_id=1,
        container_name="task-1",
        connection_type="docker_exec",
        last_activity=utc_now() - timedelta(seconds=2),
    )
    fresh_agent_session = TerminalSession(
        session_id="agent-fresh",
        task_id=1,
        user_id=0,
        container_name="task-1",
        connection_type="docker_exec",
        session_type="agent",
        last_activity=utc_now() - timedelta(seconds=2),
    )

    manager.sessions[stale_user_session.session_id] = stale_user_session
    manager.sessions[fresh_agent_session.session_id] = fresh_agent_session

    await manager._cleanup_stale_sessions()

    assert stale_user_session.session_id not in manager.sessions
    assert fresh_agent_session.session_id in manager.sessions


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_uses_agent_timeout() -> None:
    """Agent sessions should use the longer agent timeout before cleanup."""
    manager = TerminalSessionManager()
    manager.session_timeout = 10
    manager.agent_session_timeout = 1

    stale_agent_session = TerminalSession(
        session_id="agent-stale",
        task_id=1,
        user_id=0,
        container_name="task-1",
        connection_type="docker_exec",
        session_type="agent",
        last_activity=utc_now() - timedelta(seconds=2),
    )

    manager.sessions[stale_agent_session.session_id] = stale_agent_session

    await manager._cleanup_stale_sessions()

    assert stale_agent_session.session_id not in manager.sessions


@pytest.mark.asyncio
async def test_cleanup_all_sessions_cancels_background_cleanup() -> None:
    """Shutdown cleanup should cancel the background cleanup task."""
    manager = TerminalSessionManager()
    manager.cleanup_interval = 3600

    manager.start()
    cleanup_task = manager._cleanup_task

    assert cleanup_task is not None

    await manager.cleanup_all_sessions()

    assert cleanup_task.cancelled()
    assert manager._cleanup_task is None


@pytest.mark.asyncio
async def test_prepare_agent_session_preserves_prompt_workspace_and_history_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent session bootstrap must still export PS1, cd to /workspace, and unset HISTFILE."""
    manager = TerminalSessionManager()
    session = MagicMock()
    session.session_id = "agent_task_99"
    session.update_activity = MagicMock()
    session._drowai_initialized = False
    send_input = AsyncMock(return_value=True)

    monkeypatch.setattr(
        manager,
        "get_or_create_agent_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(manager, "send_input", send_input)
    monkeypatch.setattr(
        manager,
        "_read_until_agent_prompt",
        AsyncMock(return_value=AGENT_PROMPT_MARKER),
    )

    async def _immediate_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "backend.services.terminal.manager.asyncio.sleep",
        _immediate_sleep,
    )

    result = await manager.prepare_agent_session(task_id=99)

    assert result is session
    assert session._drowai_initialized is True
    assert [call.args[1] for call in send_input.await_args_list] == [
        b"export PS1='__DROWAI_PROMPT__> '\n",
        b"cd /workspace 2>/dev/null || true\n",
        b"unset HISTFILE\n",
    ]


@pytest.mark.asyncio
async def test_named_agent_sessions_are_distinct_and_close_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named agent PTY sessions for one task should map to distinct session ids."""
    manager = TerminalSessionManager()

    async def _fake_create_agent_session(
        task_id: int,
        cols: int,
        rows: int,
        session_name: str | None = None,
    ) -> TerminalSession:
        _ = cols, rows
        session_id = (
            build_named_agent_session_id(task_id, session_name)
            if session_name
            else build_agent_session_id(task_id)
        )
        session = TerminalSession(
            session_id=session_id,
            task_id=task_id,
            user_id=0,
            container_name=f"task-{task_id}",
            connection_type="docker_exec",
            session_type="agent",
        )
        manager.sessions[session_id] = session
        return session

    monkeypatch.setattr(manager, "_create_agent_session", _fake_create_agent_session)

    first = await manager.get_or_create_agent_session(task_id=101, session_name="call-one")
    second = await manager.get_or_create_agent_session(task_id=101, session_name="call-two")

    assert first.session_id == "agent_task_101_call_one"
    assert second.session_id == "agent_task_101_call_two"
    assert first.session_id != second.session_id

    await manager.close_session(first.session_id)

    assert first.session_id not in manager.sessions
    assert second.session_id in manager.sessions


@pytest.mark.asyncio
async def test_runner_mode_terminal_uses_session_id_when_socket_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner-mode sessions should use provider session_id payloads."""
    manager = TerminalSessionManager()
    session = TerminalSession(
        session_id="term-runner",
        task_id=22,
        user_id=9,
        container_name="task-22",
        connection_type="docker_exec",
        exec_id="runner-session-22",
        socket=None,
    )
    manager.sessions[session.session_id] = session

    class _Result:
        def __init__(self, *, ok: bool, metadata: dict[str, object] | None = None) -> None:
            self.ok = ok
            self.metadata = metadata or {}

    calls: list[tuple[str, dict[str, object] | None]] = []

    async def _fake_run_session_provider_operation(
        *,
        session: TerminalSession,
        operation: str,
        call,
        payload: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> _Result:
        del call, metadata
        assert session.session_id == "term-runner"
        calls.append((operation, payload))
        if operation == "read_terminal_output":
            return _Result(ok=True, metadata={"delegate_result": {"data": "runner-output", "next_cursor": 12}})
        return _Result(ok=True, metadata={})

    monkeypatch.setattr(
        manager,
        "_run_session_provider_operation",
        _fake_run_session_provider_operation,
    )

    assert await manager.send_input("term-runner", b"pwd\n") is True
    assert await manager.read_output("term-runner", size=64) == b"runner-output"
    assert await manager.resize_session("term-runner", cols=120, rows=40) is True
    assert await manager.close_session("term-runner") is True

    op_to_payload = {operation: payload for operation, payload in calls}
    assert op_to_payload["send_terminal_input"] == {
        "session_id": "runner-session-22",
        "data": b"pwd\n",
    }
    assert op_to_payload["read_terminal_output"] == {
        "session_id": "runner-session-22",
        "cursor": -1,
        "size": 64,
        "timeout": None,
    }
    assert session.output_cursor == 12
    assert op_to_payload["resize_terminal_session"] == {
        "session_id": "runner-session-22",
        "cols": 120,
        "rows": 40,
    }
    assert op_to_payload["close_terminal_session"] == {"session_id": "runner-session-22"}


@pytest.mark.asyncio
async def test_runner_terminal_read_advances_cursor_between_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud terminal readers must consume each frame once instead of replaying from the start."""
    manager = TerminalSessionManager()
    session = TerminalSession(
        session_id="term-runner-cursor",
        task_id=42,
        user_id=7,
        container_name="runner-task-42",
        connection_type="docker_exec",
        exec_id="runner-session-42",
        runtime_job_id="runtime-job-42",
    )
    manager.sessions[session.session_id] = session

    payloads: list[dict[str, object] | None] = []

    class _Result:
        def __init__(self, *, data: str, next_cursor: int) -> None:
            self.ok = True
            self.metadata = {"delegate_result": {"data": data, "next_cursor": next_cursor}}

    async def _fake_run_session_provider_operation(
        *,
        session: TerminalSession,
        operation: str,
        call,
        payload: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> _Result:
        del session, operation, call, metadata
        payloads.append(payload)
        return _Result(data="chunk", next_cursor=5 if len(payloads) == 1 else 8)

    monkeypatch.setattr(
        manager,
        "_run_session_provider_operation",
        _fake_run_session_provider_operation,
    )

    assert await manager.read_output("term-runner-cursor", size=64) == b"chunk"
    assert await manager.read_output("term-runner-cursor", size=64) == b"chunk"

    assert payloads[0] == {
        "session_id": "runner-session-42",
        "cursor": -1,
        "runtime_job_id": "runtime-job-42",
        "size": 64,
        "timeout": None,
    }
    assert payloads[1] == {
        "session_id": "runner-session-42",
        "cursor": 5,
        "runtime_job_id": "runtime-job-42",
        "size": 64,
        "timeout": None,
    }
    assert session.output_cursor == 8


@pytest.mark.asyncio
async def test_provider_stream_frame_fans_out_without_polling() -> None:
    """Pushed cloud terminal frames should use the manager replay/listener path directly."""
    manager = TerminalSessionManager()
    session = TerminalSession(
        session_id="term-push",
        task_id=42,
        user_id=7,
        container_name="runner-task-42",
        connection_type="docker_exec",
        exec_id="runner-session-42",
        stream_mode=True,
    )
    sent: list[bytes] = []

    class _WebSocket:
        async def send_bytes(self, payload: bytes) -> None:
            sent.append(payload)

    session.listeners.add(_WebSocket())
    manager.sessions[session.session_id] = session

    accepted = await manager.ingest_provider_stream_frame(
        tenant_id=1,
        runner_id="runner-1",
        task_id=42,
        provider_session_id="runner-session-42",
        data=b"hello\n",
    )

    assert accepted is True
    assert sent == [b"hello\n"]
    assert list(session.output_buffer) == [b"hello\n"]
    assert session.buffer_bytes == len(b"hello\n")


@pytest.mark.asyncio
async def test_initial_stream_drain_waits_for_first_prompt_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial stream drain should tolerate runner prompt frames arriving just after open."""
    manager = TerminalSessionManager()
    session = TerminalSession(
        session_id="term-initial-drain",
        task_id=42,
        user_id=7,
        container_name="runner-task-42",
        connection_type="docker_exec",
        exec_id="runner-session-42",
        stream_mode=True,
    )
    manager.sessions[session.session_id] = session
    timeouts: list[float | None] = []
    chunks = [b"prompt> ", b""]

    async def _fake_read_output(_session_id: str, _size: int, *, timeout: float | None = None) -> bytes:
        timeouts.append(timeout)
        return chunks.pop(0)

    monkeypatch.setattr(manager, "read_output", _fake_read_output)

    await manager._drain_initial_stream_buffer(session)

    assert timeouts == [1.0, 0.0]
    assert list(session.output_buffer) == [b"prompt> "]
    assert session.buffer_bytes == len(b"prompt> ")


@pytest.mark.asyncio
async def test_create_user_session_with_authorized_task_uses_tenant_scoped_runtime_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant-authorized session creation should dispatch via authorized task context."""
    manager = TerminalSessionManager()
    authorized_task = SimpleNamespace(id=88, tenant_id=701)
    calls: list[str] = []

    async def _fake_validate_container_access(
        task_id: int,
        user_id: int,
        *,
        authorized_task,
        runtime_call_scope,
    ) -> bool:
        assert runtime_call_scope is RuntimeCallScope.PRODUCT_TASK
        assert task_id == 88
        assert user_id == 9
        assert getattr(authorized_task, "id", None) == 88
        return True

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **kwargs):
            calls.append(str(kwargs.get("operation")))
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "exec_id": "exec-88",
                        "socket": object(),
                        "container_name": "task-88",
                    }
                },
                error_message=None,
            )

        async def run_user_task_operation(self, **_kwargs):
            raise AssertionError("run_user_task_operation should not be used with authorized_task")

    async def _noop_reader(_session):
        return None

    monkeypatch.setattr(manager, "_validate_task_ownership", lambda *_args: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(manager, "_validate_container_access", _fake_validate_container_access)
    monkeypatch.setattr(manager, "_pty_reader", _noop_reader)
    monkeypatch.setattr("backend.services.terminal.manager.RuntimeOperationService", _FakeRuntimeOperations)
    monkeypatch.setattr("backend.services.terminal.manager.SessionLocal", lambda: SimpleNamespace(close=lambda: None))

    session = await manager.create_session(task_id=88, user_id=9, authorized_task=authorized_task)

    assert session is not None
    assert session.task_id == 88
    assert "open_terminal_session" in calls
    await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_create_user_session_rejects_product_local_before_terminal_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Product local terminal creation must fail before terminal provider dispatch."""
    manager = TerminalSessionManager()
    calls: list[str] = []

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_user_task_operation(self, **kwargs):
            operation = str(kwargs.get("operation"))
            calls.append(operation)
            assert kwargs.get("runtime_call_scope") is RuntimeCallScope.PRODUCT_TASK
            if operation == "get_runtime_status":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason_code": "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN",
                        "task_id": 91,
                    },
                )
            raise AssertionError("terminal open must not run after product-local rejection")

    monkeypatch.setattr(manager, "_validate_task_ownership", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("backend.services.terminal.manager.RuntimeOperationService", _FakeRuntimeOperations)
    monkeypatch.setattr("backend.services.terminal.manager.SessionLocal", lambda: SimpleNamespace(close=lambda: None))

    session = await manager.create_session(task_id=91, user_id=12, tenant_id=701)

    assert session is None
    assert calls == ["get_runtime_status"]


@pytest.mark.asyncio
async def test_create_user_session_local_behavior_requires_explicit_test_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local terminal behavior in tests must opt into the non-product test scope."""
    manager = TerminalSessionManager()
    scopes: list[RuntimeCallScope | str | None] = []

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_user_task_operation(self, **kwargs):
            scopes.append(kwargs.get("runtime_call_scope"))
            operation = str(kwargs.get("operation"))
            if operation == "get_runtime_status":
                return SimpleNamespace(ok=True, metadata={"delegate_result": "running"})
            if operation == "open_terminal_session":
                return SimpleNamespace(
                    ok=True,
                    metadata={
                        "delegate_result": {
                            "exec_id": "local-test-exec",
                            "socket": object(),
                            "container_name": "task-92",
                        }
                    },
                    error_message=None,
                )
            raise AssertionError(operation)

    async def _noop_reader(_session):
        return None

    monkeypatch.setattr(manager, "_validate_task_ownership", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "_pty_reader", _noop_reader)
    monkeypatch.setattr("backend.services.terminal.manager.RuntimeOperationService", _FakeRuntimeOperations)
    monkeypatch.setattr("backend.services.terminal.manager.SessionLocal", lambda: SimpleNamespace(close=lambda: None))

    session = await manager.create_session(
        task_id=92,
        user_id=12,
        tenant_id=701,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert session is not None
    assert session.runtime_call_scope == RuntimeCallScope.TEST.value
    assert scopes == [RuntimeCallScope.TEST, RuntimeCallScope.TEST]


@pytest.mark.asyncio
async def test_create_agent_session_uses_internal_runtime_context_for_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent PTY creation must resolve internal runtime identity and avoid placeholders."""
    manager = TerminalSessionManager()

    resolver_calls: dict[str, object] = {}
    socket_obj = object()

    class _FakeResolver:
        def __init__(self, _db):
            pass

        def resolve_internal_task_context(self, **kwargs):
            resolver_calls.update(kwargs)
            return SimpleNamespace(
                tenant_id=9,
                task_id=44,
                workspace_id="task-44",
                runtime_placement_mode="local",
                actor_type=kwargs["actor_type"],
                actor_id=kwargs["actor_id"],
                user_id=17,
                runner_id=None,
                execution_site_id=None,
            )

    monkeypatch.setattr(
        "backend.services.terminal.manager.RuntimeProviderContextResolver",
        _FakeResolver,
    )
    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_for_context(self, *, operation, **_kwargs):
            if operation == "get_runtime_status":
                return SimpleNamespace(ok=True, metadata={"delegate_result": "running"})
            if operation == "open_terminal_session":
                return SimpleNamespace(
                    ok=True,
                    metadata={
                        "delegate_result": {
                            "exec_id": "exec-44",
                            "socket": socket_obj,
                            "container_name": "task-44",
                        }
                    },
                    error_message=None,
                )
            raise AssertionError(operation)

    monkeypatch.setattr(
        "backend.services.terminal.manager.RuntimeOperationService",
        _FakeRuntimeOperations,
    )
    monkeypatch.setattr(
        "backend.services.terminal.manager.SessionLocal",
        lambda: SimpleNamespace(close=lambda: None),
    )

    session = await manager._create_agent_session(
        task_id=44,
        cols=120,
        rows=30,
        session_name="named-session",
    )

    assert resolver_calls["task_id"] == 44
    assert resolver_calls["actor_type"].value == "agent"
    assert resolver_calls["actor_id"] == "agent_session:named-session"
    assert session.user_id == 17


@pytest.mark.asyncio
async def test_disconnect_grace_preserves_session_until_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = TerminalSessionManager()
    manager.ws_disconnect_grace_seconds = 0.05
    session = TerminalSession(
        session_id="user-grace",
        task_id=1,
        user_id=1,
        container_name="task-1",
        connection_type="docker_exec",
    )
    manager.sessions[session.session_id] = session
    closed: list[str] = []

    async def _fake_close(session_id: str) -> bool:
        closed.append(session_id)
        session.is_active = False
        manager.sessions.pop(session_id, None)
        return True

    monkeypatch.setattr(manager, "close_session", _fake_close)

    await manager.schedule_disconnect_grace(session.session_id)
    assert session.is_active is True
    assert closed == []

    await asyncio.sleep(0.08)
    assert closed == [session.session_id]


@pytest.mark.asyncio
async def test_attach_websocket_cancels_pending_disconnect_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = TerminalSessionManager()
    manager.ws_disconnect_grace_seconds = 0.05
    session = TerminalSession(
        session_id="user-resume",
        task_id=1,
        user_id=1,
        container_name="task-1",
        connection_type="docker_exec",
    )
    manager.sessions[session.session_id] = session
    closed: list[str] = []

    async def _fake_close(session_id: str) -> bool:
        closed.append(session_id)
        return True

    monkeypatch.setattr(manager, "close_session", _fake_close)

    websocket = AsyncMock()
    websocket.send_text = AsyncMock()

    await manager.schedule_disconnect_grace(session.session_id)
    await manager.attach_websocket(session.session_id, websocket)
    await asyncio.sleep(0.08)

    assert closed == []
    assert session.is_active is True
