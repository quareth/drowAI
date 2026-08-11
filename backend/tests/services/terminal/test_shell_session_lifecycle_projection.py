"""Characterize shell-session terminal lifecycle projection behavior.

This module owns black-box tests for how public shell-session close paths project
already-decided terminal lifecycle outcomes into canonical chat rows and live
task-stream packets. It does not test PTY framing, provider transports, or future
projector internals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.chat import ChatTurnEvent
from backend.models.core import Task, User
from backend.services.chat.shell_session_lifecycle_projector import (
    ShellSessionLifecycleProjector,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
)
from backend.services.terminal.shell_session_service import ShellSessionService
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionOrigin,
    ShellWaitRequest,
    ShellWriteRequest,
)

from backend.tests.test_shell_session_service import (
    FakeTerminalManager,
    MutableClock,
    RecordingStreamHub,
    _config,
    _context,
    _identity,
    _seed_shell_turn,
)


class FailingPublishHub(RecordingStreamHub):
    """Recording hub that fails publication after row persistence commits."""

    async def publish(self, task_id: int, event: dict[str, object]) -> None:
        await super().publish(task_id, event)
        raise RuntimeError("publish failed")


class StartFailTerminalManager(FakeTerminalManager):
    """Fake terminal manager that opens a terminal but rejects command start."""

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        self.sent_inputs.append(
            (session_id, data.encode() if isinstance(data, str) else data)
        )
        return False


class BlockingReadTerminalManager(FakeTerminalManager):
    """Fake terminal manager that blocks reads until the caller cancels."""

    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ):
        del session_id, size, timeout
        self.read_started.set()
        await asyncio.Event().wait()


class TrackingSession(Session):
    """SQLAlchemy session that records rollback and close calls."""

    rollbacks = 0
    closes = 0
    fail_commits = False

    def commit(self) -> None:
        if TrackingSession.fail_commits:
            raise RuntimeError("commit failed")
        super().commit()

    def rollback(self) -> None:
        TrackingSession.rollbacks += 1
        super().rollback()

    def close(self) -> None:
        TrackingSession.closes += 1
        super().close()


def _build_tracking_shell_turn_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TrackingSession.rollbacks = 0
    TrackingSession.closes = 0
    TrackingSession.fail_commits = False
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        class_=TrackingSession,
    )


def _build_shell_turn_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_task(session_factory, *, tenant_id: int) -> int:
    with session_factory() as db:
        user = User(username="shell-projection-owner", password="secret")
        db.add(user)
        db.flush()
        task = Task(user_id=user.id, tenant_id=tenant_id, name="shell-projection")
        db.add(task)
        db.flush()
        db.commit()
        return int(task.id)


def _seed_turn_without_reserved_message(
    session_factory,
    *,
    tenant_id: int,
    turn_id: str,
    conversation_id: str,
) -> int:
    with session_factory() as db:
        user = User(username=f"shell-projection-owner-{turn_id}", password="secret")
        db.add(user)
        db.flush()
        task = Task(user_id=user.id, tenant_id=tenant_id, name=f"shell-{turn_id}")
        db.add(task)
        db.flush()
        TurnWorkflowService(db).start_turn(
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=None,
        )
        db.commit()
        return int(task.id)


def _real_lifecycle_projector(
    *,
    session_factory,
    hub: RecordingStreamHub,
) -> ShellSessionLifecycleProjector:
    return ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=asyncio.get_running_loop().time,
    )


def _make_service(
    manager: FakeTerminalManager,
    *,
    tenant_id: int,
    task_id: int,
    session_factory,
    hub: RecordingStreamHub,
    clock: MutableClock | None = None,
    idle_timeout_sec: float = 300.0,
) -> ShellSessionService:
    return ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_real_lifecycle_projector(
            session_factory=session_factory,
            hub=hub,
        ),
        config=_config(
            idle_timeout_sec=idle_timeout_sec,
            termination_grace_sec=0,
            terminal_io_grace_sec=0,
        ),
        runtime_context_resolver=lambda _identity_arg: _context(
            tenant_id=tenant_id,
            task_id=task_id,
        ),
        clock=clock,
    )


async def _start_live_session(
    service: ShellSessionService,
    *,
    tenant_id: int,
    task_id: int,
    turn_id: str,
    command: str = "delayed",
    execution_owner_id: str | None = None,
    max_runtime_sec: float = 10.0,
    origin: ShellSessionOrigin | None = None,
    capability: ShellCapability = ShellCapability.ASSESSMENT,
):
    update = await service.execute(
        identity=_identity(
            tenant_id=tenant_id,
            task_id=task_id,
            execution_owner_id=execution_owner_id or f"main:{turn_id}",
        ),
        request=ShellExecRequest(
            command=command,
            yield_time_ms=0,
            max_runtime_sec=max_runtime_sec,
        ),
        capability=capability,
        origin=origin,
    )
    assert update.session_id is not None
    return update.session_id


def _load_terminal_rows(session_factory, *, message_id: int) -> list[ChatTurnEvent]:
    with session_factory() as db:
        return list(
            db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            )
            .scalars()
            .all()
        )


def _assert_no_rows_anywhere(session_factory) -> None:
    with session_factory() as db:
        assert db.execute(select(ChatTurnEvent)).scalars().all() == []


def _assert_projected_row_and_packet(
    *,
    rows: list[ChatTurnEvent],
    hub: RecordingStreamHub,
    task_id: int,
    close_reason: str,
    session_id: str,
    expected_status: str,
    expected_process_status: str,
) -> None:
    assert len(rows) == 1
    row = rows[0]
    metadata = row.event_metadata
    assert isinstance(metadata, dict)
    assert row.content == "Shell session closed"
    assert metadata["lifecycle_event"] == "shell_session_terminal"
    assert metadata["close_reason"] == close_reason
    assert metadata["session_id"] == session_id
    assert metadata["status"] == expected_status
    assert metadata["process_status"] == expected_process_status
    assert metadata["session_status"] == "closed"
    assert metadata["interaction_boundary"] == "terminal"
    assert metadata["compact_tool_result"]["summary"] == "Shell session closed"

    assert len(hub.published) == 1
    published_task_id, packet = hub.published[0]
    assert published_task_id == task_id
    packet_metadata = packet["metadata"]
    assert packet["type"] == "tool_end"
    assert packet["content"] == "Shell session closed"
    assert packet_metadata["lifecycle_event"] == "shell_session_terminal"
    assert packet_metadata["close_reason"] == close_reason
    assert packet_metadata["session_id"] == session_id
    assert packet_metadata["status"] == expected_status
    assert packet_metadata["process_status"] == expected_process_status
    assert packet_metadata["session_status"] == "closed"
    assert packet_metadata["interaction_boundary"] == "terminal"
    assert packet_metadata["output_persistence"] == "transient"


CloseAction = Callable[
    [ShellSessionService, FakeTerminalManager, str, int, int, str],
    Awaitable[str],
]


async def _close_by_task_cleanup(
    service: ShellSessionService,
    _manager: FakeTerminalManager,
    session_id: str,
    tenant_id: int,
    task_id: int,
    _turn_id: str,
) -> str:
    await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)
    return session_id


async def _close_by_owner_cleanup(
    service: ShellSessionService,
    _manager: FakeTerminalManager,
    session_id: str,
    tenant_id: int,
    task_id: int,
    turn_id: str,
) -> str:
    await service.close_owner_sessions(
        tenant_id=tenant_id,
        task_id=task_id,
        execution_owner_id=f"main:{turn_id}",
    )
    return session_id


async def _close_by_idle_cleanup(
    service: ShellSessionService,
    _manager: FakeTerminalManager,
    session_id: str,
    _tenant_id: int,
    _task_id: int,
    _turn_id: str,
) -> str:
    assert isinstance(service._clock, MutableClock)
    service._clock.advance(2)
    await service.cleanup_stale_sessions()
    return session_id


async def _close_by_deadline_cleanup(
    service: ShellSessionService,
    _manager: FakeTerminalManager,
    session_id: str,
    _tenant_id: int,
    _task_id: int,
    _turn_id: str,
) -> str:
    assert isinstance(service._clock, MutableClock)
    service._clock.advance(2)
    await service.cleanup_stale_sessions()
    return session_id


async def _close_by_interrupt(
    service: ShellSessionService,
    _manager: FakeTerminalManager,
    session_id: str,
    tenant_id: int,
    task_id: int,
    turn_id: str,
) -> str:
    update = await service.write_stdin(
        identity=_identity(
            tenant_id=tenant_id,
            task_id=task_id,
            execution_owner_id=f"main:{turn_id}",
        ),
        request=ShellWriteRequest(session_id=session_id, chars="\u0003", yield_time_ms=0),
    )
    assert update.process_status is ShellProcessStatus.TERMINATED
    return session_id


async def _close_by_operation_failure(
    service: ShellSessionService,
    manager: FakeTerminalManager,
    session_id: str,
    tenant_id: int,
    task_id: int,
    turn_id: str,
) -> str:
    manager.fail_read = True
    update = await service.wait_for_output(
        identity=_identity(
            tenant_id=tenant_id,
            task_id=task_id,
            execution_owner_id=f"main:{turn_id}",
        ),
        request=ShellWaitRequest(session_id=session_id),
    )
    assert update.process_status is ShellProcessStatus.FAILED
    return session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "close_reason",
        "action",
        "command",
        "max_runtime_sec",
        "idle_timeout_sec",
        "expected_status",
        "expected_process_status",
    ),
    [
        (
            "task_cleanup",
            _close_by_task_cleanup,
            "delayed",
            10.0,
            300.0,
            "cancelled",
            "terminated",
        ),
        (
            "owner_cleanup",
            _close_by_owner_cleanup,
            "delayed",
            10.0,
            300.0,
            "cancelled",
            "terminated",
        ),
        (
            "idle_expired",
            _close_by_idle_cleanup,
            "no-output",
            10.0,
            1.0,
            "cancelled",
            "terminated",
        ),
        (
            "deadline_expired",
            _close_by_deadline_cleanup,
            "delayed",
            1.0,
            300.0,
            "timeout",
            "timed_out",
        ),
        (
            "interrupted",
            _close_by_interrupt,
            "interactive",
            10.0,
            300.0,
            "cancelled",
            "terminated",
        ),
        (
            "operation_failed",
            _close_by_operation_failure,
            "delayed",
            10.0,
            300.0,
            "failed",
            "failed",
        ),
    ],
)
async def test_projected_close_reasons_persist_and_publish_terminal_lifecycle(
    close_reason: str,
    action: CloseAction,
    command: str,
    max_runtime_sec: float,
    idle_timeout_sec: float,
    expected_status: str,
    expected_process_status: str,
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = f"{close_reason}-turn"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id=f"conv-{close_reason}",
    )
    hub = RecordingStreamHub()
    try:
        manager = FakeTerminalManager()
        clock = MutableClock()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
            clock=clock,
            idle_timeout_sec=idle_timeout_sec,
        )
        session_id = await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
            command=command,
            max_runtime_sec=max_runtime_sec,
        )

        closed_session_id = await action(
            service,
            manager,
            session_id,
            tenant_id,
            task_id,
            turn_id,
        )

        rows = _load_terminal_rows(session_factory, message_id=message_id)
        _assert_projected_row_and_packet(
            rows=rows,
            hub=hub,
            task_id=task_id,
            close_reason=close_reason,
            session_id=closed_session_id,
            expected_status=expected_status,
            expected_process_status=expected_process_status,
        )
        assert manager.closed_sessions == ["terminal-1"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_close_persists_without_live_publication_and_releases_terminal(
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "cancelled-turn"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-cancelled",
    )
    hub = RecordingStreamHub()
    try:
        manager = BlockingReadTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        task = asyncio.create_task(
            service.execute(
                identity=_identity(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    execution_owner_id=f"main:{turn_id}",
                ),
                request=ShellExecRequest(command="delayed", yield_time_ms=1),
            )
        )
        await manager.read_started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        rows = _load_terminal_rows(session_factory, message_id=message_id)
        assert len(rows) == 1
        metadata = rows[0].event_metadata
        assert metadata["close_reason"] == "cancelled"
        assert metadata["status"] == "cancelled"
        assert metadata["process_status"] == "terminated"
        assert hub.published == []
        assert manager.closed_sessions == ["terminal-1"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_path", ["process_completed", "start_failed"])
async def test_non_projected_close_reasons_do_not_persist_or_publish(
    close_path: str,
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = f"{close_path}-turn"
    task_id, _message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id=f"conv-{close_path}",
    )
    hub = RecordingStreamHub()
    try:
        manager: FakeTerminalManager
        if close_path == "start_failed":
            manager = StartFailTerminalManager()
            command = "delayed"
        else:
            manager = FakeTerminalManager()
            command = "echo quick"
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )

        update = await service.execute(
            identity=_identity(
                tenant_id=tenant_id,
                task_id=task_id,
                execution_owner_id=f"main:{turn_id}",
            ),
            request=ShellExecRequest(command=command, yield_time_ms=0),
        )

        if close_path == "process_completed":
            assert update.process_status is ShellProcessStatus.COMPLETED
        else:
            assert update.process_status is None
        _assert_no_rows_anywhere(session_factory)
        assert hub.published == []
        assert manager.closed_sessions == ["terminal-1"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_owner_id", "seed_workflow", "expected_projected"),
    [
        ("main:correlated-turn", True, True),
        ("subagent:correlated-turn", True, False),
        ("malformed-owner", True, False),
        ("main:missing-reserved-row", False, False),
    ],
)
async def test_execution_owner_correlation_is_main_turn_only(
    execution_owner_id: str,
    seed_workflow: bool,
    expected_projected: bool,
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = execution_owner_id.removeprefix("main:")
    if seed_workflow:
        task_id, _message_id = _seed_shell_turn(
            session_factory,
            tenant_id=tenant_id,
            turn_id="correlated-turn",
            conversation_id=f"conv-{execution_owner_id}",
        )
    else:
        task_id = _seed_turn_without_reserved_message(
            session_factory,
            tenant_id=tenant_id,
            turn_id=turn_id,
            conversation_id="conv-missing-reserved-row",
        )
    hub = RecordingStreamHub()
    TrackingSession.rollbacks = 0
    TrackingSession.closes = 0

    try:
        manager = FakeTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        session_id = await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
            execution_owner_id=execution_owner_id,
        )

        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        with session_factory() as db:
            rows = db.execute(select(ChatTurnEvent)).scalars().all()
        if expected_projected:
            assert len(rows) == 1
            assert rows[0].event_metadata["session_id"] == session_id
            assert len(hub.published) == 1
        else:
            assert rows == []
            assert hub.published == []
        assert manager.closed_sessions == ["terminal-1"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_originating_tool_correlation_is_locked_for_rows_and_packets(
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "originating-tool-turn"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-originating-tool",
    )
    hub = RecordingStreamHub()
    try:
        manager = FakeTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        session_id = await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
            origin=ShellSessionOrigin(
                tool_call_id="call-shell-origin",
                tool_batch_id="batch-shell-origin",
                tool_name="shell.utility",
            ),
            capability=ShellCapability.UTILITY,
        )

        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        rows = _load_terminal_rows(session_factory, message_id=message_id)
        assert len(rows) == 1
        metadata = rows[0].event_metadata
        assert rows[0].tool_call_id == "call-shell-origin"
        assert metadata["tool_call_id"] == "call-shell-origin"
        assert metadata["tool_batch_id"] == "batch-shell-origin"
        assert metadata["tool_name"] == "shell.utility"
        assert metadata["originating_capability"] == "utility"
        assert metadata["session_id"] == session_id
        assert metadata["compact_tool_result"]["tool"] == "shell.session"

        packet_metadata = hub.published[0][1]["metadata"]
        assert packet_metadata["tool_call_id"] == "call-shell-origin"
        assert packet_metadata["tool_batch_id"] == "batch-shell-origin"
        assert packet_metadata["tool_name"] == "shell.utility"
        assert packet_metadata["originating_capability"] == "utility"
        assert packet_metadata["compact_tool_result"]["tool"] == "shell.session"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_closes_db_and_releases_terminal(
) -> None:
    engine, session_factory = _build_tracking_shell_turn_session()
    tenant_id = 1
    turn_id = "persistence-failure-turn"
    task_id, _message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-persistence-failure",
    )
    hub = RecordingStreamHub()
    TrackingSession.rollbacks = 0
    TrackingSession.closes = 0
    TrackingSession.fail_commits = True

    try:
        manager = FakeTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
        )

        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        _assert_no_rows_anywhere(session_factory)
        assert hub.published == []
        assert manager.closed_sessions == ["terminal-1"]
        assert TrackingSession.rollbacks == 1
        assert TrackingSession.closes >= 1
    finally:
        TrackingSession.fail_commits = False
        engine.dispose()


@pytest.mark.asyncio
async def test_publication_failure_preserves_committed_row_and_releases_terminal(
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "publication-failure-turn"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-publication-failure",
    )
    hub = FailingPublishHub()
    try:
        manager = FakeTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        session_id = await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
        )

        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        rows = _load_terminal_rows(session_factory, message_id=message_id)
        assert len(rows) == 1
        assert rows[0].event_metadata["session_id"] == session_id
        assert rows[0].event_metadata["close_reason"] == "task_cleanup"
        assert len(hub.published) == 1
        assert manager.closed_sessions == ["terminal-1"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_lifecycle_projection_logs_only_safe_stable_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "raw-owner-secret-should-not-log"
    task_id, _message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-log-safety",
    )
    hub = RecordingStreamHub()
    try:
        manager = FakeTerminalManager()
        service = _make_service(
            manager,
            tenant_id=tenant_id,
            task_id=task_id,
            session_factory=session_factory,
            hub=hub,
        )
        session_id = await _start_live_session(
            service,
            tenant_id=tenant_id,
            task_id=task_id,
            turn_id=turn_id,
            command="delayed",
        )
        caplog.clear()

        with caplog.at_level(
            logging.INFO,
            logger="backend.services.terminal.shell_session_service",
        ):
            await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        log_text = caplog.text
        assert "event=session_closed" in log_text
        assert f"tenant_id={tenant_id}" in log_text
        assert f"task_id={task_id}" in log_text
        assert "owner_fp=" in log_text
        assert "session_fp=" in log_text
        assert "close_reason=task_cleanup" in log_text
        assert f"main:{turn_id}" not in log_text
        assert turn_id not in log_text
        assert session_id not in log_text
        assert "delayed" not in log_text
        assert "started" not in log_text
        assert "secret-should-not-log" not in log_text
    finally:
        engine.dispose()
