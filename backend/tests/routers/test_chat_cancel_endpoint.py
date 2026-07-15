"""Router tests for explicit interactive run cancellation endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.provenance import ToolExecution
from backend.models.streaming import StreamEvent
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import chat as chat_routes
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowService
from backend.services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service


def _build_client() -> tuple[TestClient, dict, object, object]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        user = User(username="cancel-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="cancel-tenant", name="Cancel Tenant")
        db.add(tenant)
        db.flush()
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active")
        task = Task(user_id=user.id, tenant_id=tenant.id, name="cancel-task", status="running")
        db.add_all([membership, task])
        db.commit()
        seeded = {"user_id": user.id, "tenant_id": tenant.id, "task_id": task.id}

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=seeded["user_id"], username="cancel-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user
    client = TestClient(app)
    return client, seeded, engine, session_factory


def _stream_event(
    *,
    seeded: dict,
    task_id: int,
    turn_id: str,
    sequence: int,
    event_type: str,
    metadata: dict,
) -> StreamEvent:
    conversation_id = f"conv-{task_id}"
    return StreamEvent(
        tenant_id=seeded["tenant_id"],
        task_id=task_id,
        sequence=sequence,
        event_type=event_type,
        conversation_id=conversation_id,
        turn_id=turn_id,
        payload={
            "task_id": task_id,
            "sequence": sequence,
            "obj": {
                "type": event_type,
                "content": event_type,
                "metadata": {
                    "conversation_id": conversation_id,
                    "id": turn_id,
                    "turn_id": turn_id,
                    "turn_sequence": 1,
                    "ind": 1,
                    **metadata,
                },
            },
        },
    )


def _install_recording_hub(monkeypatch) -> tuple[list[tuple[int, dict]], list[tuple[int, bool]]]:
    published: list[tuple[int, dict]] = []
    streaming_states: list[tuple[int, bool]] = []

    class RecordingHub:
        def set_streaming_state(self, hub_task_id: int, is_streaming: bool) -> None:
            streaming_states.append((hub_task_id, is_streaming))

        async def publish(self, hub_task_id: int, event: dict) -> None:
            published.append((hub_task_id, event))

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: RecordingHub(),
    )
    return published, streaming_states


def test_cancel_endpoint_requests_lifecycle_cancel() -> None:
    client, seeded, engine, session_factory = _build_client()
    lifecycle = get_run_lifecycle_service()
    task_id = seeded["task_id"]
    turn_id = f"task-{task_id}-turn-1"
    with session_factory() as lifecycle_db:
        TurnWorkflowService(lifecycle_db).start_turn(
            task_id=task_id,
            conversation_id=f"conv-{task_id}",
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=task_id,
            turn_id=turn_id,
            conversation_id=f"conv-{task_id}",
            db_session=lifecycle_db,
        )
        with session_factory() as tool_db:
            tool_db.add(
                ToolExecution(
                    tenant_id=seeded["tenant_id"],
                    task_id=task_id,
                    command_id="cmd-stop-1",
                    workspace_id=f"task-{task_id}",
                    tool_call_id="tool-call-stop-1",
                    conversation_id=f"conv-{task_id}",
                    turn_id=turn_id,
                    tool_name="shell.exec",
                    tool_arguments={"command": "sleep 60"},
                    agent_path="langgraph",
                    execution_transport="file_comm",
                    status="started",
                    started_at=datetime.now(timezone.utc),
                )
            )
            tool_db.commit()
    try:
        response = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": turn_id, "reason": "user_stop"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["cancelled"] is True
        assert payload["status"] == "cancelled"
        assert payload["terminalized"] is True
        assert payload["turn_id"] == turn_id
        assert payload["active"] is True
        assert payload["already_cancelled"] is False
        assert payload["tool_cancellation"]["marked_count"] == 1
        assert payload["tool_cancellation"]["tool_call_ids"] == ["tool-call-stop-1"]
        assert payload["tool_cancellation"]["command_ids"] == ["cmd-stop-1"]
        assert payload["tool_cancellation"]["process_state"] == "cancel_requested"
        assert payload["tool_cancellation"]["runtime_kill_attempted"] is True
        assert payload["tool_cancellation"]["runtime_kill_supported"] is True

        with session_factory() as verify_db:
            assert lifecycle.is_cancel_requested(
                task_id=task_id,
                turn_id=turn_id,
                db_session=verify_db,
            ) is True
            record = lifecycle.get_active_run(task_id, db_session=verify_db)
            assert record is not None
            assert record.state == "cancelled"
            tool_row = (
                verify_db.query(ToolExecution)
                .filter(ToolExecution.task_id == task_id, ToolExecution.turn_id == turn_id)
                .one()
            )
            assert tool_row.status == "cancel_requested"
            assert tool_row.execution_metadata["cancellation"]["cancel_requested"] is True
            assert tool_row.execution_metadata["cancellation"]["process_state"] == "cancel_requested"
            assert tool_row.execution_metadata["cancellation"]["runtime_kill_attempted"] is True

        second = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": turn_id, "reason": "user_stop"},
        )
        assert second.status_code == 200, second.text
        second_payload = second.json()
        assert second_payload["cancelled"] is False
        assert second_payload["already_cancelled"] is True
        assert second_payload["status"] == "cancelled"
        assert second_payload["terminalized"] is True
        assert second_payload["tool_cancellation"]["marked_count"] == 0
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=task_id,
                turn_id=turn_id,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        engine.dispose()


def test_cancel_endpoint_publishes_live_tool_stop_projection(monkeypatch) -> None:
    client, seeded, engine, session_factory = _build_client()
    lifecycle = get_run_lifecycle_service()
    task_id = seeded["task_id"]
    turn_id = f"task-{task_id}-turn-stream"
    published, streaming_states = _install_recording_hub(monkeypatch)
    with session_factory() as lifecycle_db:
        TurnWorkflowService(lifecycle_db).start_turn(
            task_id=task_id,
            conversation_id=f"conv-{task_id}",
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=task_id,
            turn_id=turn_id,
            conversation_id=f"conv-{task_id}",
            db_session=lifecycle_db,
        )
        lifecycle_db.add(
            ToolExecution(
                tenant_id=seeded["tenant_id"],
                task_id=task_id,
                command_id="cmd-stop-stream-1",
                workspace_id=f"task-{task_id}",
                tool_call_id="tool-call-stop-stream-1",
                conversation_id=f"conv-{task_id}",
                turn_id=turn_id,
                tool_name="shell.exec",
                tool_arguments={"command": "sleep 60"},
                agent_path="langgraph",
                execution_transport="file_comm",
                status="started",
                started_at=datetime.now(timezone.utc),
            )
        )
        lifecycle_db.add_all(
            [
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=10,
                    event_type="tool_batch_start",
                    metadata={
                        "step_type": "tool_batch_start",
                        "tool_batch_id": "batch-stop-stream-1",
                        "tool_calls": [
                            {
                                "tool_call_id": "tool-call-stop-stream-1",
                                "tool": "shell.exec",
                            }
                        ],
                    },
                ),
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=11,
                    event_type="tool_start",
                    metadata={
                        "step_type": "tool_start",
                        "tool_batch_id": "batch-stop-stream-1",
                        "tool_call_id": "tool-call-stop-stream-1",
                        "tool": "shell.exec",
                    },
                ),
            ]
        )
        lifecycle_db.commit()
    try:
        response = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": turn_id, "reason": "user_stop"},
        )
        assert response.status_code == 200, response.text
        assert (task_id, False) in streaming_states
        tool_end_events = [event for _, event in published if event.get("type") == "tool_end"]
        assert len(tool_end_events) == 1
        tool_end_metadata = tool_end_events[0]["metadata"]
        assert tool_end_events[0]["content"] == "Tool stopped"
        assert tool_end_metadata["tool_call_id"] == "tool-call-stop-stream-1"
        assert tool_end_metadata["tool_batch_id"] == "batch-stop-stream-1"
        assert tool_end_metadata["status"] == "cancelled"
        assert tool_end_metadata["streaming"] is False
        assert tool_end_metadata["cancellation_source"] == "chat_stop"
        assert tool_end_metadata["process_state"] == "cancel_requested"

        batch_end_events = [event for _, event in published if event.get("type") == "tool_batch_end"]
        assert len(batch_end_events) == 1
        batch_end_metadata = batch_end_events[0]["metadata"]
        assert batch_end_metadata["tool_batch_id"] == "batch-stop-stream-1"
        assert batch_end_metadata["status"] == "cancelled"
        assert batch_end_metadata["streaming"] is False
        assert batch_end_metadata["results"][0]["tool_call_id"] == "tool-call-stop-stream-1"
        assert batch_end_metadata["results"][0]["status"] == "cancelled"
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=task_id,
                turn_id=turn_id,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        engine.dispose()


def test_cancel_endpoint_does_not_duplicate_existing_terminal_tool_stream_events(monkeypatch) -> None:
    client, seeded, engine, session_factory = _build_client()
    lifecycle = get_run_lifecycle_service()
    task_id = seeded["task_id"]
    turn_id = f"task-{task_id}-turn-terminal-stream"
    published, streaming_states = _install_recording_hub(monkeypatch)
    with session_factory() as lifecycle_db:
        TurnWorkflowService(lifecycle_db).start_turn(
            task_id=task_id,
            conversation_id=f"conv-{task_id}",
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=task_id,
            turn_id=turn_id,
            conversation_id=f"conv-{task_id}",
            db_session=lifecycle_db,
        )
        lifecycle_db.add(
            ToolExecution(
                tenant_id=seeded["tenant_id"],
                task_id=task_id,
                command_id="cmd-terminal-stream-1",
                workspace_id=f"task-{task_id}",
                tool_call_id="tool-call-terminal-stream-1",
                conversation_id=f"conv-{task_id}",
                turn_id=turn_id,
                tool_name="shell.exec",
                tool_arguments={"command": "sleep 60"},
                agent_path="langgraph",
                execution_transport="file_comm",
                status="started",
                started_at=datetime.now(timezone.utc),
            )
        )
        lifecycle_db.add_all(
            [
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=10,
                    event_type="tool_batch_start",
                    metadata={
                        "step_type": "tool_batch_start",
                        "tool_batch_id": "batch-terminal-stream-1",
                        "tool_calls": [
                            {
                                "tool_call_id": "tool-call-terminal-stream-1",
                                "tool": "shell.exec",
                            }
                        ],
                    },
                ),
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=11,
                    event_type="tool_start",
                    metadata={
                        "step_type": "tool_start",
                        "tool_batch_id": "batch-terminal-stream-1",
                        "tool_call_id": "tool-call-terminal-stream-1",
                        "tool": "shell.exec",
                    },
                ),
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=12,
                    event_type="tool_end",
                    metadata={
                        "step_type": "tool_end",
                        "tool_batch_id": "batch-terminal-stream-1",
                        "tool_call_id": "tool-call-terminal-stream-1",
                        "tool": "shell.exec",
                        "status": "cancelled",
                        "cancellation_source": "chat_stop",
                    },
                ),
                _stream_event(
                    seeded=seeded,
                    task_id=task_id,
                    turn_id=turn_id,
                    sequence=13,
                    event_type="tool_batch_end",
                    metadata={
                        "step_type": "tool_batch_end",
                        "tool_batch_id": "batch-terminal-stream-1",
                        "status": "cancelled",
                        "results": [
                            {
                                "tool_call_id": "tool-call-terminal-stream-1",
                                "tool": "shell.exec",
                                "status": "cancelled",
                            }
                        ],
                    },
                ),
            ]
        )
        lifecycle_db.commit()
    try:
        response = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": turn_id, "reason": "user_stop"},
        )
        assert response.status_code == 200, response.text
        assert (task_id, False) in streaming_states
        assert [event for _, event in published if event.get("type") == "tool_end"] == []
        assert [event for _, event in published if event.get("type") == "tool_batch_end"] == []
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=task_id,
                turn_id=turn_id,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        engine.dispose()


def test_cancel_endpoint_reports_turn_id_mismatch() -> None:
    client, seeded, engine, session_factory = _build_client()
    lifecycle = get_run_lifecycle_service()
    task_id = seeded["task_id"]
    active_turn_id = f"task-{task_id}-turn-2"
    with session_factory() as lifecycle_db:
        lifecycle.start_run(
            task_id=task_id,
            turn_id=active_turn_id,
            conversation_id=f"conv-{task_id}",
            db_session=lifecycle_db,
        )
        lifecycle_db.add(
            ToolExecution(
                tenant_id=seeded["tenant_id"],
                task_id=task_id,
                command_id="cmd-active-mismatch",
                workspace_id=f"task-{task_id}",
                tool_call_id="tool-call-active-mismatch",
                conversation_id=f"conv-{task_id}",
                turn_id=active_turn_id,
                tool_name="shell.exec",
                tool_arguments={"command": "sleep 60"},
                agent_path="langgraph",
                execution_transport="file_comm",
                status="started",
                started_at=datetime.now(timezone.utc),
            )
        )
        lifecycle_db.commit()
    try:
        response = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": "task-999-turn-1", "reason": "user_stop"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["cancelled"] is False
        assert payload["already_cancelled"] is False
        assert payload["active"] is True
        assert payload["status"] == "turn_id_mismatch"
        assert payload["reason"] == "turn_id_mismatch"
        assert payload["tool_cancellation"]["marked_count"] == 0
        with session_factory() as verify_db:
            tool_row = (
                verify_db.query(ToolExecution)
                .filter(ToolExecution.task_id == task_id, ToolExecution.turn_id == active_turn_id)
                .one()
            )
            assert tool_row.status == "started"
            assert tool_row.execution_metadata in (None, {})
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=task_id,
                turn_id=active_turn_id,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        engine.dispose()


def test_cancel_endpoint_uses_registry_fallback_when_durable_row_missing() -> None:
    client, seeded, engine, _session_factory = _build_client()
    lifecycle = get_run_lifecycle_service()
    task_id = seeded["task_id"]
    turn_id = f"task-{task_id}-turn-77"
    # Simulate a run tracked only by in-memory fallback authority.
    lifecycle._registry.start(task_id=task_id, turn_id=turn_id, conversation_id=f"conv-{task_id}")
    try:
        response = client.post(
            f"/tasks/{task_id}/chat/cancel",
            json={"turn_id": turn_id, "reason": "fallback_stop"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "cancelled"
        assert payload["terminalized"] is True
        assert payload["cancelled"] is True
        assert payload["active"] is True
    finally:
        lifecycle._registry.finish(task_id=task_id, turn_id=turn_id, state="cancelled")
        client.close()
        engine.dispose()
