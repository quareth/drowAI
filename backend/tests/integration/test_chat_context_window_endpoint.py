"""
Router-level tests for GET /tasks/{task_id}/chat/context-window.

These tests verify ownership protection and chat-scoped snapshot semantics
without relying on external database services.
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
from backend.models.hitl import TurnWorkflow
from backend.routers import chat as chat_routes
from backend.routers.chat import history as history_module
from backend.services.tenant.context import TenantRequestContext


class _FakeConversationManager:
    def __init__(self, task_id: int) -> None:
        self._task_id = task_id

    def ensure_default_conversation(self) -> str:
        return f"conv-{self._task_id}"


def _create_context_message(
    db,
    *,
    task: Task,
    conversation_id: str,
    parent_id: int | None,
    message_type: str,
    message: str,
) -> ChatMessage:
    """Create one tenant-scoped message for this endpoint fixture."""

    row = ChatMessage(
        task_id=task.id,
        tenant_id=task.tenant_id,
        conversation_id=conversation_id,
        parent_message_id=parent_id,
        message_type=message_type,
        message=message,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def chat_context_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="context-owner", password="secret")
        other = User(username="context-other", password="secret")
        db.add(owner)
        db.add(other)
        db.flush()
        task = Task(user_id=owner.id, name="context-task")
        db.add(task)
        db.commit()
        seeded = {
            "owner_id": owner.id,
            "other_id": other.id,
            "task_id": task.id,
            "session_factory": session_factory,
            "current_user_id": owner.id,
            "tenant_id": task.tenant_id,
        }

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(
            id=seeded["current_user_id"],
            username=f"user-{seeded['current_user_id']}",
            is_active=True,
        )

    def fake_get_tenant_context():
        current_user_id = seeded["current_user_id"]
        return TenantRequestContext(
            tenant_id=(
                seeded["tenant_id"]
                if current_user_id == seeded["owner_id"]
                else seeded["tenant_id"] + 1
            ),
            user_id=current_user_id,
            role="owner",
            membership_id=current_user_id,
            is_default_tenant=True,
        )

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_routes.get_tenant_request_context] = (
        fake_get_tenant_context
    )
    monkeypatch.setattr(chat_routes, "ConversationManager", _FakeConversationManager)

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_context_window_snapshot_is_conversation_specific(chat_context_client) -> None:
    client, seeded = chat_context_client
    task_id = seeded["task_id"]
    session_factory = seeded["session_factory"]
    empty_conv = "conv-empty"
    full_conv = "conv-full"

    with session_factory() as db:
        task = db.get(Task, task_id)
        u = _create_context_message(
            db,
            task=task,
            conversation_id=full_conv,
            parent_id=None,
            message_type="user",
            message="hello from full conversation",
        )
        _create_context_message(
            db,
            task=task,
            conversation_id=full_conv,
            parent_id=u.id,
            message_type="assistant",
            message="assistant response",
        )

    empty_resp = client.get(f"/tasks/{task_id}/chat/context-window?conversation_id={empty_conv}")
    assert empty_resp.status_code == 200, empty_resp.text
    empty_payload = empty_resp.json()
    assert empty_payload["task_id"] == task_id
    assert empty_payload["conversation_id"] == empty_conv

    full_resp = client.get(f"/tasks/{task_id}/chat/context-window?conversation_id={full_conv}")
    assert full_resp.status_code == 200, full_resp.text
    full_payload = full_resp.json()
    assert full_payload["task_id"] == task_id
    assert full_payload["conversation_id"] == full_conv
    assert full_payload["used_tokens"] > empty_payload["used_tokens"]
    assert full_payload["max_tokens"] >= full_payload["used_tokens"]
    assert full_payload["remaining_tokens"] == full_payload["max_tokens"] - full_payload["used_tokens"]
    assert 0.0 <= full_payload["ratio"] <= 1.0
    assert full_payload["snapshot_kind"] == "bootstrap_estimate"
    assert full_payload["revision"] == -1
    assert full_payload["turn_sequence"] is None


def test_context_window_snapshot_prefers_latest_persisted_measurement(
    chat_context_client,
) -> None:
    """Reloads return the latest full-request measurement, not a history estimate."""

    client, seeded = chat_context_client
    task_id = seeded["task_id"]
    session_factory = seeded["session_factory"]
    conversation_id = "conv-measured"

    def measured(turn_sequence: int, used_tokens: int) -> dict[str, object]:
        max_tokens = 32_768
        return {
            "context_window": {
                "conversation_id": conversation_id,
                "max_tokens": max_tokens,
                "used_tokens": used_tokens,
                "remaining_tokens": max_tokens - used_tokens,
                "ratio": used_tokens / max_tokens,
                "ceiling_reached": False,
                "recommended_next_action": "none",
                "compression_candidate": False,
                "turn_sequence": turn_sequence,
                "revision": turn_sequence,
                "snapshot_kind": "measured",
            }
        }

    with session_factory() as db:
        task = db.get(Task, task_id)
        malformed_latest = measured(3, 30_000)
        malformed_context = malformed_latest["context_window"]
        assert isinstance(malformed_context, dict)
        malformed_context["revision"] = 4
        db.add_all(
            [
                TurnWorkflow(
                    task_id=task_id,
                    tenant_id=task.tenant_id,
                    conversation_id=conversation_id,
                    turn_id="measured-turn-1",
                    turn_sequence=1,
                    state="COMPLETED",
                    workflow_metadata=measured(1, 8_000),
                ),
                TurnWorkflow(
                    task_id=task_id,
                    tenant_id=task.tenant_id,
                    conversation_id=conversation_id,
                    turn_id="measured-turn-2",
                    turn_sequence=2,
                    state="FAILED",
                    workflow_metadata=measured(2, 9_500),
                ),
                TurnWorkflow(
                    task_id=task_id,
                    tenant_id=task.tenant_id,
                    conversation_id=conversation_id,
                    turn_id="malformed-turn-3",
                    turn_sequence=3,
                    state="COMPLETED",
                    workflow_metadata=malformed_latest,
                ),
            ]
        )
        db.commit()

    response = client.get(
        f"/tasks/{task_id}/chat/context-window?conversation_id={conversation_id}"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["used_tokens"] == 9_500
    assert payload["remaining_tokens"] == 23_268
    assert payload["revision"] == 2
    assert payload["turn_sequence"] == 2
    assert payload["snapshot_kind"] == "measured"


def test_context_window_snapshot_defaults_to_active_conversation(chat_context_client) -> None:
    client, seeded = chat_context_client
    task_id = seeded["task_id"]
    session_factory = seeded["session_factory"]
    default_conv = f"conv-{task_id}"

    with session_factory() as db:
        task = db.get(Task, task_id)
        u = _create_context_message(
            db,
            task=task,
            conversation_id=default_conv,
            parent_id=None,
            message_type="user",
            message="default conversation user",
        )
        _create_context_message(
            db,
            task=task,
            conversation_id=default_conv,
            parent_id=u.id,
            message_type="assistant",
            message="default conversation assistant",
        )

    response = client.get(f"/tasks/{task_id}/chat/context-window")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["conversation_id"] == default_conv
    assert payload["used_tokens"] > 0


def test_context_window_snapshot_enforces_task_ownership(chat_context_client) -> None:
    client, seeded = chat_context_client
    seeded["current_user_id"] = seeded["other_id"]
    response = client.get(f"/tasks/{seeded['task_id']}/chat/context-window")
    assert response.status_code == 404, response.text


def test_context_window_snapshot_uses_runtime_provider_model(
    chat_context_client,
    monkeypatch,
) -> None:
    client, seeded = chat_context_client
    task_id = seeded["task_id"]
    observed: dict[str, object] = {}

    class _FakeRuntimeConfigService:
        def __init__(self, _db) -> None:
            pass

        def build_runtime_selection(self, *, user_id: int, require_enabled_credential: bool):
            observed["selection_user_id"] = user_id
            observed["require_enabled_credential"] = require_enabled_credential
            return SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6")

    class _FakeContextWindowManager:
        def __init__(self, *, max_tokens: int) -> None:
            observed["max_tokens"] = max_tokens

        def evaluate_history(self, **kwargs):
            observed["evaluate_kwargs"] = dict(kwargs)
            return SimpleNamespace(
                snapshot=SimpleNamespace(
                    task_id=kwargs["task_id"],
                    conversation_id=kwargs["conversation_id"],
                    max_tokens=observed["max_tokens"],
                    used_tokens=7,
                    remaining_tokens=int(observed["max_tokens"]) - 7,
                    ratio=7 / int(observed["max_tokens"]),
                    ceiling_reached=False,
                ),
                recommended_next_action="none",
                compression_candidate=False,
            )

    def _fake_resolve_context_window_max_tokens(*, provider: str, model: str) -> int:
        observed["resolver_provider"] = provider
        observed["resolver_model"] = model
        return 1_000

    monkeypatch.setattr(
        history_module,
        "LLMRuntimeConfigService",
        _FakeRuntimeConfigService,
    )
    monkeypatch.setattr(
        history_module,
        "ContextWindowManager",
        _FakeContextWindowManager,
    )
    monkeypatch.setattr(
        history_module,
        "resolve_context_window_max_tokens",
        _fake_resolve_context_window_max_tokens,
    )

    response = client.get(f"/tasks/{task_id}/chat/context-window")

    assert response.status_code == 200, response.text
    assert observed["selection_user_id"] == seeded["owner_id"]
    assert observed["require_enabled_credential"] is False
    assert observed["resolver_provider"] == "anthropic"
    assert observed["resolver_model"] == "claude-sonnet-4-6"
    assert observed["evaluate_kwargs"]["provider"] == "anthropic"
    assert observed["evaluate_kwargs"]["model"] == "claude-sonnet-4-6"
