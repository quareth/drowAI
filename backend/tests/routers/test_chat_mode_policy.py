"""Router tests for chat execution-mode policy.

These tests verify that ordinary HTTP callers cannot force LangGraph execution
branches through `POST /tasks/{task_id}/chat`; backend routing and the
server-owned E2E gate remain authoritative.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

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
from backend.services.llm_provider import LLMCredentialService, LLMProviderSelectionService
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID
from agent.providers.llm.profiles import ANTHROPIC_LISTABLE_MODEL_IDS


class _FakeConversationManager:
    def __init__(self, task_id: int) -> None:
        self._task_id = task_id

    def ensure_default_conversation(self) -> str:
        return f"conv-{self._task_id}"


class _FakeHub:
    def __init__(self, *, streaming: bool = False, queued_count: int = 0) -> None:
        self._streaming = streaming
        self._queued_count = queued_count
        self.published: list[tuple[int, dict[str, Any]]] = []
        self.queued_payload: dict[str, Any] | None = None

    async def publish(self, task_id: int, event: dict[str, Any]) -> None:
        self.published.append((task_id, event))

    def is_task_streaming(self, task_id: int) -> bool:
        return self._streaming

    def get_queued_count(self, task_id: int) -> int:
        return self._queued_count

    def queue_message(self, *args: Any, **kwargs: Any) -> None:
        self.queued_payload = {"args": args, "kwargs": kwargs}


def _run_immediately(coro: Any) -> SimpleNamespace:
    """Execute await-free test coroutines synchronously."""
    try:
        coro.send(None)
    except StopIteration:
        pass
    return SimpleNamespace(done=lambda: True, cancel=lambda: False)


def _build_client(
    *,
    hub: _FakeHub,
    monkeypatch,
    selected_provider: str = "openai",
    selected_model: str = "gpt-5.2",
) -> tuple[TestClient, dict[str, Any], list[dict[str, Any]]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        user = User(username="mode-policy-owner", password="secret")
        db.add(user)
        db.flush()
        tenant = Tenant(slug="mode-policy-tenant", name="Mode Policy Tenant")
        db.add(tenant)
        db.flush()
        membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active")
        task = Task(user_id=user.id, tenant_id=tenant.id, name="mode-policy-task", status="running")
        db.add_all([membership, task])
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="test-key",
        )
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="test-anthropic-key",
        )
        LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        ).set_selection(
            user_id=user.id,
            provider=selected_provider,
            model=selected_model,
        )
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
        return SimpleNamespace(id=seeded["user_id"], username="mode-policy-owner", is_active=True)

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = fake_get_current_user

    captured_calls: list[dict[str, Any]] = []

    async def _fake_run_langgraph_generation(**kwargs: Any) -> None:
        captured_calls.append(kwargs)

    monkeypatch.setattr(chat_routes, "ConversationManager", _FakeConversationManager)
    monkeypatch.setattr(chat_routes, "_build_conversation_history", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        chat_routes,
        "_reserve_chat_turn",
        lambda db, **kwargs: (11, 12, f"task-{seeded['task_id']}-turn-1", 1),
    )
    monkeypatch.setattr(chat_routes, "run_langgraph_generation", _fake_run_langgraph_generation)
    monkeypatch.setattr(chat_routes, "_schedule_background_task", _run_immediately)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    client = TestClient(app)

    def _cleanup() -> None:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()

    seeded["_cleanup"] = _cleanup
    return client, seeded, captured_calls


def test_chat_route_ignores_client_mode_for_generation(monkeypatch) -> None:
    original_create_task = asyncio.create_task
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Scan 10.0.0.5",
                "mode": "deep_reasoning",
            },
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["success"] is True
        assert payload["turn_id"] == f"task-{seeded['task_id']}-turn-1"
        assert payload.get("queued") is not True

        assert len(captured_calls) == 1
        generation_call = captured_calls[0]
        assert generation_call["requested_mode"] is None
        assert generation_call["provider"] == "openai"
        assert generation_call["runtime_selection"]["provider"] == "openai"
        assert generation_call["runtime_selection"]["model"] == "gpt-5.2"
        assert generation_call["runtime_selection"]["credential_ref"] == {
            "user_id": seeded["user_id"],
            "provider": "openai",
        }
        assert "api_key" not in generation_call["runtime_selection"]
        assert asyncio.create_task is original_create_task
    finally:
        seeded["_cleanup"]()


def test_chat_route_ignores_client_mode_for_queueing(monkeypatch) -> None:
    hub = _FakeHub(streaming=True, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Scan 10.0.0.5",
                "mode": "simple_tool_execution",
            },
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["success"] is True
        assert payload["queued"] is True
        assert payload["turn_id"] == f"task-{seeded['task_id']}-turn-1"

        assert captured_calls == []
        assert hub.queued_payload is not None
        assert hub.queued_payload["kwargs"]["requested_mode"] is None
        assert hub.queued_payload["kwargs"]["provider"] == "openai"
        assert hub.queued_payload["kwargs"]["model"] == "gpt-5.2"
        assert hub.queued_payload["kwargs"]["credential_ref"] == {
            "user_id": seeded["user_id"],
            "provider": "openai",
        }
        assert "api_key" not in hub.queued_payload["kwargs"]
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_provider_model_override_for_generation(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run provider-aware check",
                "provider": "openai",
                "model": "gpt-5-mini",
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["provider"] == "openai"
        assert call["model"] == "gpt-5-mini"
        assert call["runtime_selection"]["provider"] == "openai"
        assert call["runtime_selection"]["model"] == "gpt-5-mini"
        assert "api_key" not in call["runtime_selection"]
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_explicit_anthropic_provider_model(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run provider-aware check",
                "provider": ANTHROPIC_PROVIDER_ID,
                "model": model,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["provider"] == ANTHROPIC_PROVIDER_ID
        assert call["model"] == model
        assert call["runtime_selection"]["provider"] == ANTHROPIC_PROVIDER_ID
        assert call["runtime_selection"]["model"] == model
        assert call["runtime_selection"]["credential_ref"] == {
            "user_id": seeded["user_id"],
            "provider": ANTHROPIC_PROVIDER_ID,
        }
        assert "api_key" not in call["runtime_selection"]
    finally:
        seeded["_cleanup"]()


def test_chat_route_uses_saved_anthropic_provider_when_provider_omitted(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
    client, seeded, captured_calls = _build_client(
        hub=hub,
        monkeypatch=monkeypatch,
        selected_provider=ANTHROPIC_PROVIDER_ID,
        selected_model=model,
    )
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run saved-provider check",
                "model": model,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["provider"] == ANTHROPIC_PROVIDER_ID
        assert call["model"] == model
        assert call["runtime_selection"]["provider"] == ANTHROPIC_PROVIDER_ID
        assert call["runtime_selection"]["model"] == model
        assert call["runtime_selection"]["credential_ref"] == {
            "user_id": seeded["user_id"],
            "provider": ANTHROPIC_PROVIDER_ID,
        }
        assert "api_key" not in call["runtime_selection"]
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_anthropic_reasoning_effort(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
    client, seeded, captured_calls = _build_client(
        hub=hub,
        monkeypatch=monkeypatch,
        selected_provider=ANTHROPIC_PROVIDER_ID,
        selected_model=model,
    )
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run Anthropic reasoning check",
                "reasoning_effort": "medium",
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        assert captured_calls[0]["reasoning_effort"] == "medium"
        assert captured_calls[0]["runtime_selection"]["reasoning_effort"] == "medium"
    finally:
        seeded["_cleanup"]()


def test_chat_route_rejects_unknown_reasoning_effort(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Scan 10.0.0.5",
                "reasoning_effort": "invalid-effort",
            },
        )
        assert response.status_code == 422, response.text
        assert "Allowed values" in response.text
        assert captured_calls == []
    finally:
        seeded["_cleanup"]()


def test_chat_route_rejects_xhigh_for_non_pro_model(monkeypatch) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(
        hub=hub,
        monkeypatch=monkeypatch,
        selected_model="gpt-5.2",
    )
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Scan 10.0.0.5",
                "reasoning_effort": "xhigh",
            },
        )
        assert response.status_code == 422, response.text
        assert "models that support xhigh" in response.text
        assert captured_calls == []
    finally:
        seeded["_cleanup"]()


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_chat_route_accepts_gpt5_effort_values_and_forwards_to_generation(
    monkeypatch,
    effort: str,
) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(
        hub=hub,
        monkeypatch=monkeypatch,
        selected_model="gpt-5.2",
    )
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run matrix check",
                "reasoning_effort": effort,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        assert captured_calls[0]["reasoning_effort"] == effort
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_xhigh_for_gpt52_pro_and_forwards_to_generation(
    monkeypatch,
) -> None:
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(
        hub=hub,
        monkeypatch=monkeypatch,
        selected_model="gpt-5.2-pro",
    )
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "Run matrix check",
                "reasoning_effort": "xhigh",
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        assert captured_calls[0]["reasoning_effort"] == "xhigh"
    finally:
        seeded["_cleanup"]()


def test_chat_route_normalizes_legacy_agent_mode_plan_into_agent_plus_plan_mode(
    monkeypatch,
) -> None:
    """Phase 6 Task 6.2: legacy ``agent_mode=plan`` normalizes at the boundary.

    Legacy UI clients continue to submit ``agent_mode=plan`` during the
    migration. The backend must collapse this into
    ``agent_mode=agent`` + ``plan_mode=true`` before scheduling the
    turn so the rest of the stack reads a single canonical shape.
    """
    from backend.services.langgraph_chat.contracts import AgentMode

    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "plan me a route",
                "agent_mode": "plan",
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["agent_mode"] == AgentMode.AGENT
        assert call["plan_mode"] is True
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_agent_plus_plan_mode_overlay(monkeypatch) -> None:
    """Phase 6: ``agent`` + ``plan_mode=true`` forwards both to generation."""
    from backend.services.langgraph_chat.contracts import AgentMode

    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "plan and ask",
                "agent_mode": "agent",
                "plan_mode": True,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["agent_mode"] == AgentMode.AGENT
        assert call["plan_mode"] is True
    finally:
        seeded["_cleanup"]()


def test_chat_route_accepts_full_access_plus_plan_mode_overlay(monkeypatch) -> None:
    """Phase 6: ``full_access`` + ``plan_mode=true`` forwards both to generation.

    ``Full Access + Plan`` is explicitly supported — deep-reasoning
    routing with no tool-use approval prompts.
    """
    from backend.services.langgraph_chat.contracts import AgentMode

    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "plan and go",
                "agent_mode": "full_access",
                "plan_mode": True,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["agent_mode"] == AgentMode.FULL_ACCESS
        assert call["plan_mode"] is True
    finally:
        seeded["_cleanup"]()


@pytest.mark.parametrize(
    ("plan_mode", "expected_mode"),
    [(False, "simple_tool_execution"), (True, "deep_reasoning")],
)
def test_deterministic_chat_maps_ui_plan_state_to_scenario_branch(
    monkeypatch,
    plan_mode: bool,
    expected_mode: str,
) -> None:
    """Deterministic E2E turns exercise the UI-selected scenario without an LLM."""
    monkeypatch.setattr("backend.routers.chat.submit.E2E_DETERMINISTIC_MODE", True)
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "exercise deterministic UI mode",
                "agent_mode": "full_access",
                "plan_mode": plan_mode,
                "deterministic": True,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        assert captured_calls[0]["requested_mode"].value == expected_mode
    finally:
        seeded["_cleanup"]()


def test_client_deterministic_flag_cannot_activate_e2e_routing(monkeypatch) -> None:
    """A production request field cannot bypass the server-owned E2E gate."""
    monkeypatch.setattr("backend.routers.chat.submit.E2E_DETERMINISTIC_MODE", False)
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "do not activate deterministic routing",
                "agent_mode": "full_access",
                "plan_mode": True,
                "deterministic": True,
            },
        )
        assert response.status_code == 202, response.text
        assert len(captured_calls) == 1
        assert captured_calls[0]["requested_mode"] is None
        assert captured_calls[0]["deterministic_mode"] is False
    finally:
        seeded["_cleanup"]()


def test_chat_route_rejects_chat_plus_plan_mode(monkeypatch) -> None:
    """Phase 6 Task 6.4: ``chat`` + ``plan_mode=true`` returns 422.

    Chat and Plan are mutually exclusive in the new UX contract; the
    backend rejects the combination before scheduling any work.
    """
    hub = _FakeHub(streaming=False, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "plan and chat?",
                "agent_mode": "chat",
                "plan_mode": True,
            },
        )
        assert response.status_code == 422, response.text
        assert "mutually exclusive" in response.text
        assert captured_calls == []
    finally:
        seeded["_cleanup"]()


def test_chat_route_forwards_plan_mode_to_queued_message(monkeypatch) -> None:
    """Phase 6: queued turns preserve the normalized ``plan_mode``."""
    from backend.services.langgraph_chat.contracts import AgentMode

    hub = _FakeHub(streaming=True, queued_count=0)
    client, seeded, captured_calls = _build_client(hub=hub, monkeypatch=monkeypatch)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={
                "message": "plan and queue",
                "agent_mode": "agent",
                "plan_mode": True,
            },
        )
        assert response.status_code == 202, response.text
        assert hub.queued_payload is not None
        kwargs = hub.queued_payload["kwargs"]
        assert kwargs["agent_mode"] == AgentMode.AGENT
        assert kwargs["plan_mode"] is True
    finally:
        seeded["_cleanup"]()
