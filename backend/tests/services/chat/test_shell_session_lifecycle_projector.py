"""Tests for chat-owned shell session lifecycle projection.

This module verifies the extracted projector for already-decided terminal close
facts. It does not exercise PTY mechanics, shell-session registries, providers,
or production singleton composition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task, User
from backend.services.chat.shell_session_lifecycle_projector import (
    ShellSessionLifecycleProjector,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
)
from backend.services.streaming.stream_event_schema import TOOL_PHASE_INDEX
from backend.services.terminal.contracts import ShellSessionTerminalEvent
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellSessionIdentity,
    ShellSessionOrigin,
)


class TrackingSession(Session):
    """SQLAlchemy session that records transaction and cleanup calls."""

    events: list[str] = []
    fail_commits = False

    def commit(self) -> None:
        TrackingSession.events.append("commit")
        if TrackingSession.fail_commits:
            raise RuntimeError("commit failed")
        super().commit()

    def rollback(self) -> None:
        TrackingSession.events.append("rollback")
        super().rollback()

    def close(self) -> None:
        TrackingSession.events.append("close")
        super().close()


class RecordingStreamHub:
    """Stream hub double that records publish calls and ordering."""

    def __init__(
        self,
        events: list[str] | None = None,
        *,
        fail_publish: bool = False,
    ) -> None:
        self.events = events if events is not None else []
        self.fail_publish = fail_publish
        self.published: list[tuple[int, dict[str, object]]] = []

    async def publish(self, task_id: int, event: dict[str, object]) -> None:
        self.events.append("publish")
        self.published.append((task_id, event))
        if self.fail_publish:
            raise RuntimeError("publish failed")


class CountingSessionFactory:
    """Callable wrapper that records whether projection opened a DB session."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self.opens = 0

    def __call__(self) -> Session:
        self.opens += 1
        return self._session_factory()


def _build_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TrackingSession.events = []
    TrackingSession.fail_commits = False
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        class_=TrackingSession,
    )


def _identity(**overrides: object) -> ShellSessionIdentity:
    values = {
        "tenant_id": 1,
        "task_id": 11,
        "execution_owner_id": "main:turn-123",
        "runtime_placement_mode": "runner",
        "workspace_id": "workspace-11",
        "workspace_path": "/workspace",
        "runner_id": "runner-1",
        "execution_site_id": "site-1",
    }
    values.update(overrides)
    return ShellSessionIdentity(**values)


def _event(
    *,
    task_id: int = 11,
    execution_owner_id: str = "main:turn-123",
    close_reason: str = "task_cleanup",
    origin: ShellSessionOrigin | None = None,
    capability: ShellCapability = ShellCapability.ASSESSMENT,
) -> ShellSessionTerminalEvent:
    return ShellSessionTerminalEvent(
        identity=_identity(task_id=task_id, execution_owner_id=execution_owner_id),
        public_session_id="shs_public_1",
        originating_capability=capability,
        origin=origin,
        close_reason=close_reason,
    )


def _seed_shell_turn(
    session_factory: Callable[[], Session],
    *,
    tenant_id: int = 1,
    turn_id: str = "turn-123",
    conversation_id: str = "conv-123",
    reserved_message: bool = True,
) -> tuple[int, int | None]:
    with session_factory() as db:
        user = User(username=f"projector-owner-{turn_id}", password="secret")
        db.add(user)
        db.flush()
        task = Task(user_id=user.id, tenant_id=tenant_id, name=f"projector-{turn_id}")
        db.add(task)
        db.flush()
        message_id: int | None = None
        if reserved_message:
            message = ChatMessage(
                task_id=task.id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_type="assistant",
                message="",
                token_count=0,
                turn_number=1,
            )
            db.add(message)
            db.flush()
            message_id = int(message.id)
        TurnWorkflowService(db).start_turn(
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=message_id,
        )
        db.commit()
        return int(task.id), message_id


def _load_rows(session_factory: Callable[[], Session]) -> list[ChatTurnEvent]:
    with session_factory() as db:
        return list(
            db.execute(select(ChatTurnEvent).order_by(ChatTurnEvent.phase_sequence.asc()))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_reason", "expected_status", "expected_process_status", "publish_count"),
    [
        ("task_cleanup", "cancelled", "terminated", 1),
        ("owner_cleanup", "cancelled", "terminated", 1),
        ("idle_expired", "cancelled", "terminated", 1),
        ("interrupted", "cancelled", "terminated", 1),
        ("deadline_expired", "timeout", "timed_out", 1),
        ("operation_failed", "failed", "failed", 1),
        ("cancelled", "cancelled", "terminated", 0),
    ],
)
async def test_projector_persists_rows_and_publishes_live_packet_after_commit(
    close_reason: str,
    expected_status: str,
    expected_process_status: str,
    publish_count: int,
) -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(session_factory)
    TrackingSession.events = []
    hub = RecordingStreamHub(TrackingSession.events)
    projector = ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        await projector.project_terminal_event(
            _event(task_id=task_id, close_reason=close_reason)
        )

        rows = _load_rows(session_factory)
        assert len(rows) == 1
        metadata = rows[0].event_metadata
        assert isinstance(metadata, dict)
        assert rows[0].content == "Shell session closed"
        assert rows[0].tool_call_id == "shell-session-shs_public_1"
        assert metadata["tool_call_id"] == "shell-session-shs_public_1"
        assert metadata.get("tool_batch_id") is None
        assert metadata["tool_name"] == "shell.session"
        assert metadata["execution_owner_id"] == "main:turn-123"
        assert metadata["runtime_placement_mode"] == "runner"
        assert metadata["runner_id"] == "runner-1"
        assert metadata["execution_site_id"] == "site-1"
        assert metadata["originating_capability"] == "assessment"
        assert metadata["status"] == expected_status
        assert metadata["process_status"] == expected_process_status
        assert metadata["session_status"] == "closed"
        assert metadata["interaction_boundary"] == "terminal"
        assert metadata["session_id"] == "shs_public_1"
        assert metadata["close_reason"] == close_reason
        assert metadata["lifecycle_event"] == "shell_session_terminal"
        assert metadata["output_persistence"] == "transient"
        assert metadata["compact_tool_result"] == {
            "schema_version": "2.0",
            "tool": "shell.session",
            "status": expected_status,
            "success": False,
            "exit_code": None,
            "summary": "Shell session closed",
            "key_findings": [],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": None,
            "process_status": expected_process_status,
            "session_status": "closed",
            "interaction_boundary": "terminal",
            "session_id": "shs_public_1",
        }
        assert TrackingSession.events.count("commit") == 1
        assert TrackingSession.events.count("rollback") == 0
        assert TrackingSession.events.count("close") >= 1

        assert len(hub.published) == publish_count
        if publish_count:
            assert TrackingSession.events.index("commit") < TrackingSession.events.index(
                "publish"
            )
            published_task_id, packet = hub.published[0]
            assert published_task_id == task_id
            packet_metadata = packet["metadata"]
            assert packet == {
                "type": "tool_end",
                "content": "Shell session closed",
                "metadata": packet_metadata,
            }
            assert packet_metadata["subtype"] == "tool_end"
            assert packet_metadata["step_type"] == "tool_end"
            assert packet_metadata["ind"] == TOOL_PHASE_INDEX
            assert packet_metadata["tool"] == "shell.session"
            assert packet_metadata["tool_name"] == "shell.session"
            assert packet_metadata["tool_call_id"] == "shell-session-shs_public_1"
            assert "tool_batch_id" not in packet_metadata
            assert packet_metadata["status"] == expected_status
            assert packet_metadata["process_status"] == expected_process_status
            assert packet_metadata["session_status"] == "closed"
            assert packet_metadata["interaction_boundary"] == "terminal"
            assert packet_metadata["session_id"] == "shs_public_1"
            assert packet_metadata["close_reason"] == close_reason
            assert packet_metadata["lifecycle_event"] == "shell_session_terminal"
            assert packet_metadata["output_persistence"] == "transient"
            assert packet_metadata["compact_tool_result"] == metadata[
                "compact_tool_result"
            ]
            assert packet_metadata["conversation_id"] == "conv-123"
            assert packet_metadata["conversationId"] == "conv-123"
            assert packet_metadata["id"] == "turn-123"
            assert packet_metadata["turn_id"] == "turn-123"
            assert packet_metadata["turn_sequence"] == 1
            assert packet_metadata["streaming"] is False
            assert packet_metadata["is_streaming"] is False
            assert packet_metadata["in_progress"] is False
            assert packet_metadata["source"] == "shell_session_cleanup"
            assert packet_metadata["timestamp"] == 1234.5
            assert packet_metadata["execution_owner_id"] == "main:turn-123"
            assert packet_metadata["runtime_placement_mode"] == "runner"
            assert packet_metadata["runner_id"] == "runner-1"
            assert packet_metadata["execution_site_id"] == "site-1"
            assert packet_metadata["originating_capability"] == "assessment"
        else:
            assert "publish" not in TrackingSession.events
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_projector_preserves_originating_tool_correlation() -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(session_factory)
    TrackingSession.events = []
    hub = RecordingStreamHub(TrackingSession.events)
    projector = ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        await projector.project_terminal_event(
            _event(
                task_id=task_id,
                origin=ShellSessionOrigin(
                    tool_call_id="call-shell-origin",
                    tool_batch_id="batch-shell-origin",
                    tool_name="shell.utility",
                ),
                capability=ShellCapability.UTILITY,
            )
        )

        rows = _load_rows(session_factory)
        assert len(rows) == 1
        metadata = rows[0].event_metadata
        assert rows[0].tool_call_id == "call-shell-origin"
        assert metadata["tool_call_id"] == "call-shell-origin"
        assert metadata["tool_batch_id"] == "batch-shell-origin"
        assert metadata["tool_name"] == "shell.utility"
        assert metadata["originating_capability"] == "utility"
        assert metadata["shell_lifecycle_event"] is True
        assert metadata["compact_tool_result"]["tool"] == "shell.session"
        packet_metadata = hub.published[0][1]["metadata"]
        assert packet_metadata["tool_call_id"] == "call-shell-origin"
        assert packet_metadata["tool_batch_id"] == "batch-shell-origin"
        assert packet_metadata["tool_name"] == "shell.utility"
        assert packet_metadata["originating_capability"] == "utility"
        assert packet_metadata["shell_lifecycle_event"] is True
        assert packet_metadata["compact_tool_result"]["tool"] == "shell.session"
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_owner_id", "close_reason"),
    [
        ("subagent:turn-123", "task_cleanup"),
        ("malformed-owner", "task_cleanup"),
        ("main:", "task_cleanup"),
        ("main:turn-123", "process_completed"),
        ("main:turn-123", "start_failed"),
    ],
)
async def test_projector_noops_without_main_owner_or_projected_reason(
    execution_owner_id: str,
    close_reason: str,
) -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(session_factory)
    TrackingSession.events = []
    counted_factory = CountingSessionFactory(session_factory)
    hub = RecordingStreamHub(TrackingSession.events)
    projector = ShellSessionLifecycleProjector(
        session_factory=counted_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        await projector.project_terminal_event(
            _event(
                task_id=task_id,
                execution_owner_id=execution_owner_id,
                close_reason=close_reason,
            )
        )

        assert _load_rows(session_factory) == []
        assert hub.published == []
        assert counted_factory.opens == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_projector_noops_when_main_turn_has_no_reserved_message() -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(
        session_factory,
        turn_id="missing-reserved-row",
        reserved_message=False,
    )
    TrackingSession.events = []
    hub = RecordingStreamHub(TrackingSession.events)
    projector = ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        await projector.project_terminal_event(
            _event(
                task_id=task_id,
                execution_owner_id="main:missing-reserved-row",
            )
        )

        assert _load_rows(session_factory) == []
        assert hub.published == []
        assert TrackingSession.events.count("commit") == 1
        assert TrackingSession.events.count("rollback") == 0
        assert TrackingSession.events.count("close") >= 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_projector_persistence_failure_rolls_back_and_suppresses_publish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(
        session_factory,
        turn_id="raw-owner-secret-should-not-log",
    )
    TrackingSession.events = []
    TrackingSession.fail_commits = True
    hub = RecordingStreamHub(TrackingSession.events)
    projector = ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        with caplog.at_level(
            logging.DEBUG,
            logger="backend.services.chat.shell_session_lifecycle_projector",
        ):
            await projector.project_terminal_event(
                _event(
                    task_id=task_id,
                    execution_owner_id="main:raw-owner-secret-should-not-log",
                )
            )

        assert _load_rows(session_factory) == []
        assert hub.published == []
        assert TrackingSession.events.count("commit") == 1
        assert TrackingSession.events.count("rollback") == 1
        assert TrackingSession.events.count("close") >= 1
        log_text = caplog.text
        assert "canonical terminal lifecycle persistence failed" in log_text
        assert "task_id=" in log_text
        assert "owner_fp=" in log_text
        assert "close_reason=task_cleanup" in log_text
        assert "main:raw-owner-secret-should-not-log" not in log_text
        assert "secret-should-not-log" not in log_text
    finally:
        TrackingSession.fail_commits = False
        engine.dispose()


@pytest.mark.asyncio
async def test_projector_publication_failure_preserves_committed_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, session_factory = _build_session_factory()
    task_id, _message_id = _seed_shell_turn(session_factory)
    TrackingSession.events = []
    hub = RecordingStreamHub(TrackingSession.events, fail_publish=True)
    projector = ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=lambda: 1234.5,
    )

    try:
        with caplog.at_level(
            logging.DEBUG,
            logger="backend.services.chat.shell_session_lifecycle_projector",
        ):
            await projector.project_terminal_event(_event(task_id=task_id))

        rows = _load_rows(session_factory)
        assert len(rows) == 1
        assert rows[0].event_metadata["close_reason"] == "task_cleanup"
        assert len(hub.published) == 1
        assert TrackingSession.events.index("commit") < TrackingSession.events.index(
            "publish"
        )
        assert "terminal lifecycle live projection failed" in caplog.text
    finally:
        engine.dispose()
