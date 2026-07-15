"""
Router tests for chat prewarm/readiness warmup integration.

These tests verify `chat/prewarm` and `chat/ready` are both wired to the
shared RuntimeWarmupService authority and surface the unified warmup status in
response payloads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.chat import ChatMessage
from backend.models.core import Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import chat as chat_routes


class _FakeConversationManager:
    ensure_calls: list[int] = []

    def __init__(self, task_id: int) -> None:
        self._task_id = task_id

    def ensure_default_conversation(self) -> str:
        type(self).ensure_calls.append(self._task_id)
        return f"conv-{self._task_id}"


class _FakeWarmupService:
    def __init__(self, status: dict[str, dict[str, object]]) -> None:
        self._status = status
        self.warm_calls: list[int] = []

    async def warm_task_runtime(self, task_id: int, graph_name=None, workspace_path=None) -> dict[str, dict[str, object]]:
        self.warm_calls.append(task_id)
        return self._status

    def get_warmup_status(self, task_id: int) -> dict[str, dict[str, object]]:
        return self._status


class _FakeHub:
    def __init__(self) -> None:
        self._metadata: dict[int, dict[str, object]] = {}
        self._running: dict[int, bool] = {}

    def update_chat_metadata(self, task_id: int, conversation_id: str | None, checkpointer_ready: bool) -> None:
        self._metadata[task_id] = {
            "conversation_id": conversation_id,
            "checkpointer_ready": checkpointer_ready,
            "sse_connected": False,
        }

    def set_task_running(self, task_id: int, running: bool) -> None:
        self._running[task_id] = running

    def get_chat_ready_payload(self, task_id: int) -> dict[str, object]:
        data = self._metadata.get(task_id, {})
        conversation_id = data.get("conversation_id")
        running = bool(self._running.get(task_id, False))
        return {
            "conversation_id": conversation_id,
            "task_running": running,
            "sse_connected": bool(data.get("sse_connected", False)),
            "checkpointer_ready": bool(data.get("checkpointer_ready", False)),
            "chat_ready": running and bool(conversation_id),
        }


class _FakeRunLifecycleService:
    def __init__(self, state: str = "idle") -> None:
        self._state = state

    def get_active_run(self, task_id: int, db_session=None):  # noqa: D401 - test double
        if self._state == "idle":
            return None
        return SimpleNamespace(
            task_id=task_id,
            state=self._state,
            turn_id="turn-1",
            cancel_requested=False,
            cancel_reason=None,
            conversation_id=f"conv-{task_id}",
        )


@pytest.fixture
def chat_test_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        user = User(username="chat-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="chat-test", name="Chat Test")
        db.add(tenant)
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
        task = Task(
            user_id=user.id,
            tenant_id=tenant.id,
            name="chat-task",
            status="running",
        )
        db.add(task)
        db.commit()
        seeded = {
            "user_id": user.id,
            "tenant_id": tenant.id,
            "task_id": task.id,
            "session_factory": session_factory,
        }

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        seeded["request_db"] = db
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=seeded["user_id"], username="chat-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user

    monkeypatch.setattr(chat_routes, "ConversationManager", _FakeConversationManager)
    _FakeConversationManager.ensure_calls = []
    hub = _FakeHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_chat_prewarm_triggers_runtime_warmup(chat_test_client, monkeypatch) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    response = client.post(f"/tasks/{seeded['task_id']}/chat/prewarm")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert fake_service.warm_calls == [seeded["task_id"]]
    assert payload["checkpointer_ready"] is True
    assert payload["tool_catalog_ready"] is True
    assert payload["pty_session_ready"] is False
    assert payload["pty_warmup_required"] is False
    assert payload["runtime_warm"] is True


def test_chat_ready_reflects_unified_warmup_status(chat_test_client, monkeypatch) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": False, "error": "cp failed", "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    response = client.get(f"/tasks/{seeded['task_id']}/chat/ready")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert fake_service.warm_calls == [seeded["task_id"]]
    assert payload["checkpointer_ready"] is False
    assert payload["tool_catalog_ready"] is True
    assert payload["pty_session_ready"] is False
    assert payload["pty_warmup_required"] is False
    assert payload["runtime_warm"] is False


def test_chat_ready_uses_run_lifecycle_when_task_row_is_not_running(chat_test_client, monkeypatch) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )
    monkeypatch.setattr(
        chat_routes,
        "get_run_lifecycle_service",
        lambda: _FakeRunLifecycleService(state="running"),
    )

    session_factory = seeded["session_factory"]
    with session_factory() as db:
        task = db.query(Task).filter(Task.id == seeded["task_id"]).first()
        assert task is not None
        task.status = "completed"
        db.commit()

    response = client.get(f"/tasks/{seeded['task_id']}/chat/ready")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_running"] is True
    assert payload["chat_ready"] is True


def test_chat_history_initial_returns_readiness_and_transcript_page(chat_test_client, monkeypatch) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    session_factory = seeded["session_factory"]
    task_id = seeded["task_id"]
    conversation_id = f"conv-{task_id}"
    with session_factory() as db:
        db.add_all(
            [
                ChatMessage(
                    task_id=task_id,
                    tenant_id=seeded["tenant_id"],
                    conversation_id=conversation_id,
                    message_type="user",
                    message="hello",
                    token_count=1,
                ),
                ChatMessage(
                    task_id=task_id,
                    tenant_id=seeded["tenant_id"],
                    conversation_id=conversation_id,
                    message_type="assistant",
                    message="world",
                    token_count=1,
                ),
            ]
        )
        db.commit()

    response = client.get(f"/tasks/{task_id}/chat/history?initial=true")
    assert response.status_code == 200, response.text
    payload = response.json()
    startup = payload["startup"]
    assert startup["task_id"] == task_id
    assert startup["conversation_id"] == conversation_id
    assert startup["chat_ready"] is True
    assert startup["checkpointer_ready"] is True
    assert startup["tool_catalog_ready"] is True
    assert startup["runtime_warm"] is True
    assert payload["contractVersion"] == "2026-03-01.chat-history.v2"
    assert len(payload["items"]) == 2
    assert payload["items"][0]["kind"] == "user"
    assert payload["items"][0]["content"] == "hello"
    assert payload["items"][1]["kind"] == "assistant"
    assert payload["items"][1]["content"] == "world"
    assert payload["hasMoreOlder"] is False
    assert payload["nextBeforeTurn"] is None
    assert "events" not in payload


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/tasks/{task_id}/chat/history?initial=true"),
        ("POST", "/tasks/{task_id}/chat/prewarm"),
        ("GET", "/tasks/{task_id}/chat/ready"),
    ],
)
def test_chat_readiness_releases_request_transaction_before_runtime_warmup(
    chat_test_client,
    monkeypatch,
    method: str,
    path: str,
) -> None:
    """Chat readiness paths must not retain ORM transactions across warmup awaits."""
    client, seeded = chat_test_client

    class _TransactionInspectingWarmupService(_FakeWarmupService):
        async def warm_task_runtime(self, task_id: int, graph_name=None, workspace_path=None):
            request_db = seeded["request_db"]
            assert request_db.in_transaction() is False
            return await super().warm_task_runtime(task_id, graph_name, workspace_path)

    fake_service = _TransactionInspectingWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    response = client.request(method, path.format(task_id=seeded["task_id"]))

    assert response.status_code == 200, response.text


def test_chat_history_initial_returns_transcript_paging_fields_only(chat_test_client, monkeypatch) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    session_factory = seeded["session_factory"]
    task_id = seeded["task_id"]
    conversation_id = f"conv-{task_id}"
    with session_factory() as db:
        messages = [
            ChatMessage(
                task_id=task_id,
                tenant_id=seeded["tenant_id"],
                conversation_id=conversation_id,
                message_type="user",
                message=f"user-{idx}",
                token_count=1,
            )
            for idx in range(1, 5)
        ]
        db.add_all(messages)
        db.commit()

    response = client.get(f"/tasks/{task_id}/chat/history?initial=true&limit=2")
    assert response.status_code == 200, response.text
    payload = response.json()
    startup = payload["startup"]
    assert startup["task_id"] == task_id
    assert startup["conversation_id"] == conversation_id
    assert len(payload["items"]) == 2
    assert payload["hasMoreOlder"] is True
    assert isinstance(payload["nextBeforeTurn"], int)
    assert "events" not in payload


def test_chat_history_initial_stopped_task_uses_db_conversation_without_workspace_creation(
    chat_test_client,
    monkeypatch,
) -> None:
    client, seeded = chat_test_client
    fake_service = _FakeWarmupService(
        status={
            "checkpointer": {"ready": True, "error": None, "skipped": False},
            "tool_catalog": {"ready": True, "error": None, "skipped": False},
            "pty_session": {"ready": False, "error": None, "skipped": True},
        }
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        lambda: fake_service,
    )

    session_factory = seeded["session_factory"]
    task_id = seeded["task_id"]
    conversation_id = "conv-stopped-db"
    with session_factory() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        assert task is not None
        task.status = "stopped"
        db.add_all(
            [
                ChatMessage(
                    task_id=task_id,
                    tenant_id=seeded["tenant_id"],
                    conversation_id=conversation_id,
                    message_type="user",
                    message="persisted-user",
                    token_count=1,
                ),
                ChatMessage(
                    task_id=task_id,
                    tenant_id=seeded["tenant_id"],
                    conversation_id=conversation_id,
                    message_type="assistant",
                    message="persisted-assistant",
                    token_count=1,
                ),
            ]
        )
        db.commit()

    _FakeConversationManager.ensure_calls = []
    response = client.get(f"/tasks/{task_id}/chat/history?initial=true")
    assert response.status_code == 200, response.text
    payload = response.json()
    startup = payload["startup"]
    assert startup["conversation_id"] == conversation_id
    assert startup["task_running"] is False
    assert startup["chat_ready"] is False
    assert [item["content"] for item in payload["items"]] == [
        "persisted-user",
        "persisted-assistant",
    ]
    assert _FakeConversationManager.ensure_calls == []
