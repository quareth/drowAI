"""Tests for run lifecycle metadata exposed by streaming-status API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import chat as chat_routes
from backend.services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
    TurnWorkflowState,
)


class _StubHub:
    def is_task_streaming(self, task_id: int) -> bool:
        return True

    def get_queued_count(self, task_id: int) -> int:
        return 2


def test_streaming_status_includes_run_metadata(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="status-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="status-tenant", name="Status Tenant")
        db.add(tenant)
        db.flush()
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active")
        task = Task(user_id=user.id, tenant_id=tenant.id, name="status-task", status="running")
        db.add_all([membership, task])
        db.commit()
        seeded = {"user_id": user.id, "task_id": task.id}

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=seeded["user_id"], username="status-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{seeded['task_id']}-turn-5"
    with session_factory() as lifecycle_db:
        lifecycle.start_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            conversation_id="conv-status",
            db_session=lifecycle_db,
        )
        lifecycle.request_cancel(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            reason="manual_stop",
            db_session=lifecycle_db,
        )

    client = TestClient(app)
    try:
        response = client.get(f"/tasks/{seeded['task_id']}/streaming-status")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["is_streaming"] is True
        assert payload["queued_count"] == 2
        assert payload["run"]["state"] == "running"
        assert payload["run"]["turn_id"] == turn_id
        assert payload["run"]["cancel_requested"] is True
        assert payload["run"]["cancel_reason"] == "manual_stop"
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=seeded["task_id"],
                turn_id=turn_id,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def test_batch_streaming_statuses_returns_terminal_and_running_states(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="status-batch-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="status-batch-tenant", name="Status Batch Tenant")
        db.add(tenant)
        db.flush()
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active")
        task_a = Task(user_id=user.id, tenant_id=tenant.id, name="A", status="running")
        task_b = Task(user_id=user.id, tenant_id=tenant.id, name="B", status="running")
        db.add_all([membership, task_a, task_b])
        db.commit()
        seeded = {"user_id": user.id, "task_a_id": task_a.id, "task_b_id": task_b.id}

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=seeded["user_id"], username="status-batch-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )

    lifecycle = get_run_lifecycle_service()
    turn_a = f"task-{seeded['task_a_id']}-turn-2"
    turn_b = f"task-{seeded['task_b_id']}-turn-9"
    with session_factory() as lifecycle_db:
        workflow_service = TurnWorkflowService(lifecycle_db)
        workflow_a = workflow_service.start_turn(
            task_id=seeded["task_a_id"],
            conversation_id="conv-a",
            turn_id=turn_a,
            turn_sequence=2,
            graph_name="simple_tool",
        )
        workflow_service.mark_completed(workflow_id=workflow_a.id)
        workflow_service.start_turn(
            task_id=seeded["task_b_id"],
            conversation_id="conv-b",
            turn_id=turn_b,
            turn_sequence=9,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=seeded["task_b_id"],
            turn_id=turn_b,
            conversation_id="conv-b",
            db_session=lifecycle_db,
        )

    client = TestClient(app)
    try:
        response = client.get(
            f"/interactive-runs/statuses?task_ids={seeded['task_a_id']}&task_ids={seeded['task_b_id']}"
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        entries = {item["task_id"]: item for item in payload["tasks"]}
        assert entries[seeded["task_a_id"]]["run"]["state"] == "completed"
        assert entries[seeded["task_b_id"]]["run"]["state"] == "running"
    finally:
        with session_factory() as lifecycle_db:
            lifecycle.end_run(
                task_id=seeded["task_b_id"],
                turn_id=turn_b,
                status="cancelled",
                db_session=lifecycle_db,
            )
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def test_end_run_projects_terminal_status_for_active_workflow() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="terminal-projection-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="terminal-projection-tenant", name="Terminal Projection Tenant")
        db.add(tenant)
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        task = Task(user_id=user.id, tenant_id=tenant.id, name="terminal-task", status="running")
        db.add(task)
        db.commit()
        seeded = {"task_id": task.id}

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{seeded['task_id']}-turn-11"
    with session_factory() as lifecycle_db:
        workflow = TurnWorkflowService(lifecycle_db).start_turn(
            task_id=seeded["task_id"],
            conversation_id="conv-terminal",
            turn_id=turn_id,
            turn_sequence=11,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            conversation_id="conv-terminal",
            db_session=lifecycle_db,
        )
        lifecycle.end_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            status="completed",
            db_session=lifecycle_db,
        )

        record = lifecycle.get_active_run(seeded["task_id"], db_session=lifecycle_db)
        refreshed = lifecycle_db.get(type(workflow), workflow.id)

    try:
        assert record is not None
        assert record.state == "completed"
        assert refreshed is not None
        assert refreshed.state == TurnWorkflowState.COMPLETED.value
        assert refreshed.completed_at is not None
        assert refreshed.workflow_metadata["terminal_status"] == "completed"
    finally:
        lifecycle._registry.finish(task_id=seeded["task_id"], turn_id=turn_id, state="completed")
        engine.dispose()


@pytest.mark.parametrize("flow_status", ("failed", "completed"))
def test_end_run_preserves_provider_refusal_terminal_projection(
    flow_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle finalization cannot rewrite a durable refusal as completed."""
    run_state_events: list[dict[str, object]] = []
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.run_lifecycle.emit_run_state_event",
        lambda **kwargs: run_state_events.append(kwargs),
    )
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username=f"refusal-{flow_status}", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(
            slug=f"refusal-{flow_status}",
            name=f"Refusal {flow_status}",
        )
        db.add(tenant)
        db.flush()
        db.add(
            TenantMembership(
                tenant_id=tenant.id,
                user_id=user.id,
                role="owner",
                status="active",
            )
        )
        task = Task(
            user_id=user.id,
            tenant_id=tenant.id,
            name="refusal-task",
            status="running",
        )
        db.add(task)
        db.commit()
        task_id = task.id

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{task_id}-turn-1"
    with session_factory() as db:
        workflow_service = TurnWorkflowService(db)
        workflow = workflow_service.start_turn(
            task_id=task_id,
            conversation_id="conv-refusal",
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
        )
        workflow_service.mark_failed(
            workflow_id=workflow.id,
            metadata={
                "outcome_type": "provider_refusal",
                "retryable": False,
                "refusal": {"provider": "openai", "model": "gpt-4o-mini"},
            },
            replace_metadata=True,
        )
        lifecycle.end_run(
            task_id=task_id,
            turn_id=turn_id,
            status=flow_status,
            db_session=db,
        )
        refreshed = db.get(type(workflow), workflow.id)
        record = lifecycle.get_active_run(task_id, db_session=db)

    try:
        assert refreshed is not None
        assert refreshed.state == TurnWorkflowState.FAILED.value
        assert refreshed.workflow_metadata["outcome_type"] == "provider_refusal"
        assert refreshed.workflow_metadata["terminal_status"] == "declined"
        assert record is not None
        assert record.state == "declined"
        assert chat_routes._build_run_payload(record)["state"] == "declined"
        assert run_state_events[-1]["state"] == "declined"
    finally:
        lifecycle._registry.finish(
            task_id=task_id,
            turn_id=turn_id,
            state="failed",
        )
        engine.dispose()


def test_cancelled_terminal_status_wins_over_late_completion() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="cancel-wins-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="cancel-wins-tenant", name="Cancel Wins Tenant")
        db.add(tenant)
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        task = Task(user_id=user.id, tenant_id=tenant.id, name="cancel-wins-task", status="running")
        db.add(task)
        db.commit()
        seeded = {"task_id": task.id}

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{seeded['task_id']}-turn-12"
    with session_factory() as lifecycle_db:
        workflow = TurnWorkflowService(lifecycle_db).start_turn(
            task_id=seeded["task_id"],
            conversation_id="conv-cancel-wins",
            turn_id=turn_id,
            turn_sequence=12,
            graph_name="simple_tool",
        )
        lifecycle.start_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            conversation_id="conv-cancel-wins",
            db_session=lifecycle_db,
        )
        lifecycle.request_cancel(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            reason="user_stop",
            db_session=lifecycle_db,
        )
        lifecycle.end_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            status="cancelled",
            db_session=lifecycle_db,
        )
        assert lifecycle.is_cancel_requested(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            db_session=lifecycle_db,
        ) is True

        lifecycle.end_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            status="completed",
            db_session=lifecycle_db,
        )
        record = lifecycle.get_active_run(seeded["task_id"], db_session=lifecycle_db)
        refreshed = lifecycle_db.get(type(workflow), workflow.id)

    try:
        assert record is not None
        assert record.state == "cancelled"
        assert refreshed is not None
        assert refreshed.workflow_metadata["terminal_status"] == "cancelled"
        assert refreshed.workflow_metadata["cancel_requested"] is True
        assert refreshed.state == TurnWorkflowState.FAILED.value
    finally:
        lifecycle._registry.finish(task_id=seeded["task_id"], turn_id=turn_id, state="cancelled")
        engine.dispose()


def test_resumed_workflow_projects_running_over_stale_waiting_terminal_status() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="resume-projection-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="resume-projection-tenant", name="Resume Projection Tenant")
        db.add(tenant)
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        task = Task(user_id=user.id, tenant_id=tenant.id, name="resume-projection-task", status="running")
        db.add(task)
        db.commit()
        seeded = {"task_id": task.id}

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{seeded['task_id']}-turn-13"
    with session_factory() as lifecycle_db:
        workflow_service = TurnWorkflowService(lifecycle_db)
        workflow = workflow_service.start_turn(
            task_id=seeded["task_id"],
            conversation_id="conv-resume-projection",
            turn_id=turn_id,
            turn_sequence=13,
            graph_name="simple_tool",
        )
        workflow_service.mark_waiting_for_human(
            workflow_id=workflow.id,
            checkpoint_id="cp-resume-projection",
            interrupt_type="tool_approval",
            graph_name="simple_tool",
            resume_key="cp-resume-projection",
        )
        lifecycle.end_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            status="waiting_for_human",
            db_session=lifecycle_db,
        )
        workflow_service.try_begin_resume(
            task_id=seeded["task_id"],
            resume_key="cp-resume-projection",
            checkpoint_id="cp-resume-projection",
            graph_name="simple_tool",
        )

        record = lifecycle.start_run(
            task_id=seeded["task_id"],
            turn_id=turn_id,
            conversation_id="conv-resume-projection",
            db_session=lifecycle_db,
        )
        refreshed = lifecycle_db.get(type(workflow), workflow.id)

    try:
        assert record.state == "running"
        assert refreshed is not None
        assert refreshed.state == TurnWorkflowState.RESUMED.value
        assert refreshed.workflow_metadata["terminal_status"] == "waiting_for_human"
    finally:
        lifecycle._registry.finish(task_id=seeded["task_id"], turn_id=turn_id, state="completed")
        engine.dispose()


def test_streaming_status_falls_back_to_registry_when_no_workflow_row(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        user = User(username="status-fallback-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="status-fallback-tenant", name="Status Fallback Tenant")
        db.add(tenant)
        db.flush()
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active")
        task = Task(user_id=user.id, tenant_id=tenant.id, name="fallback-task", status="running")
        db.add_all([membership, task])
        db.commit()
        seeded = {"user_id": user.id, "task_id": task.id}

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=seeded["user_id"], username="status-fallback-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )

    lifecycle = get_run_lifecycle_service()
    turn_id = f"task-{seeded['task_id']}-turn-99"
    lifecycle._registry.start(
        task_id=seeded["task_id"],
        turn_id=turn_id,
        conversation_id="conv-fallback",
    )

    client = TestClient(app)
    try:
        response = client.get(f"/tasks/{seeded['task_id']}/streaming-status")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["run"]["state"] == "running"
        assert payload["run"]["turn_id"] == turn_id
    finally:
        lifecycle._registry.finish(task_id=seeded["task_id"], turn_id=turn_id, state="cancelled")
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()
