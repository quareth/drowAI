"""Smoke tests for Anthropic chat route runtime wiring.

This module verifies that the public chat route can carry Anthropic runtime
metadata into the facade, through the pre-graph intent classifier, and into the
graph LLM resolver without decrypted secrets or OpenAI reasoning kwargs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.graph.utils.llm_resolver import resolve_llm_client
from agent.providers.llm.core.base import LLMResponse
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles import ANTHROPIC_LISTABLE_MODEL_IDS
from backend.database import Base
from backend.models.core import Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import chat as chat_routes
from backend.services.langgraph_chat import facade_helpers
from backend.services.langgraph_chat.contracts import AgentMode, ChatInputs, LangGraphChatResult
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.routing.selectors import ChatBranch
from backend.services.llm_provider import LLMCredentialService, LLMProviderSelectionService
from backend.services.llm_provider.runtime_client_resolver import LLMRuntimeClientResolver
from backend.services.llm_provider.types import LLMCredentialRef, ProviderSecret


class _FakeConversationManager:
    def __init__(self, task_id: int) -> None:
        self._task_id = task_id

    def ensure_default_conversation(self) -> str:
        return f"conv-{self._task_id}"


class _FakeHub:
    async def publish(self, task_id: int, event: dict[str, Any]) -> None:
        return None

    def is_task_streaming(self, task_id: int) -> bool:
        return False

    def get_queued_count(self, task_id: int) -> int:
        return 0


class _FakeAnthropicClient:
    def __init__(self, model: str, calls: list[dict[str, Any]]) -> None:
        self._model = model
        self._calls = calls

    @property
    def model(self) -> str:
        return self._model

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        self._calls.append(
            {
                "method": "chat_with_usage",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        return LLMResponse(
            content='{"label":"simple_chat","confidence":0.9,"reasoning":"simple"}',
            structured_output={
                "label": "simple_chat",
                "confidence": 0.9,
                "reasoning": "simple",
            },
        )

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        self._calls.append(
            {
                "method": "chat",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        return "Anthropic graph response"


class _CredentialService:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def resolve_secret(
        self,
        credential_ref: LLMCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
    ) -> ProviderSecret:
        self._calls.append(
            {
                "credential_ref": credential_ref,
                "runtime_user_id": runtime_user_id,
                "task_id": task_id,
                "purpose": purpose,
            }
        )
        return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        return LLMCredentialRef(user_id=user_id, provider=provider)


class _GraphResolverHandler:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def handle(self, runtime_config: Any) -> LangGraphChatResult:
        metadata = facade_helpers.build_metadata(
            runtime_config.chat_inputs,
            runtime_config,
        )
        graph_config = facade_helpers.build_thread_config(
            runtime_config,
            runtime_config.chat_inputs.task_id,
        )
        client = resolve_llm_client(metadata, config=graph_config)
        await client.chat("graph system", "graph user")
        self._calls.append({"metadata": metadata, "graph_config": graph_config})
        return LangGraphChatResult(
            final_text="Anthropic graph response",
            conversation_id=runtime_config.chat_inputs.conversation_id,
            metadata=metadata,
        )


def _run_immediately(coro: Any) -> SimpleNamespace:
    try:
        coro.send(None)
    except StopIteration:
        pass
    return SimpleNamespace(done=lambda: True, cancel=lambda: False)


async def _no_timeout(awaitable: Any, **_kwargs: Any) -> Any:
    return await awaitable


def test_chat_route_reaches_anthropic_facade_and_graph_resolver(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
    with session_factory() as db:
        user = User(username="anthropic-runtime-owner", password="secret")
        tenant = Tenant(slug="anthropic-runtime", name="Anthropic Runtime")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        task = Task(
            user_id=user.id,
            tenant_id=tenant.id,
            name="anthropic-runtime-task",
            status="running",
        )
        db.add(task)
        credential_service = LLMCredentialService(db)
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
            provider=ANTHROPIC_PROVIDER_ID,
            model=model,
        )
        db.commit()
        seeded = {
            "user_id": user.id,
            "task_id": task.id,
            "tenant_id": tenant.id,
            "graph_thread_id": task.graph_thread_id,
        }

    credential_calls: list[dict[str, Any]] = []
    factory_calls: list[dict[str, Any]] = []
    client_calls: list[dict[str, Any]] = []
    graph_calls: list[dict[str, Any]] = []

    def fake_get_client(
        *,
        provider_model: ProviderModelRef,
        api_key: str,
        **kwargs: Any,
    ) -> _FakeAnthropicClient:
        factory_calls.append(
            {
                "provider_model": provider_model,
                "api_key": api_key,
                "kwargs": kwargs,
            }
        )
        return _FakeAnthropicClient(provider_model.model, client_calls)

    async def fake_run_langgraph_generation(**kwargs: Any) -> None:
        runtime_selection = dict(kwargs["runtime_selection"])
        resolver = LLMRuntimeClientResolver(_CredentialService(credential_calls))
        facade = LangGraphChatFacade(
            prior_turn_reference_materializer=SimpleNamespace(
                materialize_for_runtime_config=lambda *args, **kwargs: None
            ),
        )
        facade._handlers[ChatBranch.NORMAL_CHAT] = _GraphResolverHandler(graph_calls)
        chat_inputs = ChatInputs(
            task_id=kwargs["task_id"],
            user_id=kwargs["user_id"],
            provider=kwargs["provider"],
            model=kwargs["model"],
            credential_ref=runtime_selection["credential_ref"],
            llm_runtime_selection=runtime_selection,
            reasoning_effort=kwargs.get("reasoning_effort"),
            message=kwargs["message"],
            conversation_id=kwargs["conversation_id"],
            history=kwargs["history"],
            anchor_sequence=kwargs.get("anchor_sequence"),
            requested_mode=kwargs.get("requested_mode"),
            agent_mode=kwargs.get("agent_mode") or AgentMode.FULL_ACCESS,
            plan_mode=bool(kwargs.get("plan_mode")),
        )
        await facade.handle_turn(
            chat_inputs,
            metadata={
                "tenant_id": seeded["tenant_id"],
                "graph_thread_id": seeded["graph_thread_id"],
            },
            runtime_services=SimpleNamespace(client_resolver=resolver),
        )

    app = FastAPI()
    app.include_router(chat_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[chat_routes.get_db] = fake_get_db
    app.dependency_overrides[chat_routes.get_current_user] = lambda: SimpleNamespace(
        id=seeded["user_id"],
        username="anthropic-runtime-owner",
        is_active=True,
    )

    monkeypatch.setattr(chat_routes, "ConversationManager", _FakeConversationManager)
    monkeypatch.setattr(chat_routes, "_build_conversation_history", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        chat_routes,
        "_reserve_chat_turn",
        lambda db, **kwargs: (11, 12, f"task-{seeded['task_id']}-turn-1", 1),
    )
    monkeypatch.setattr(chat_routes, "run_langgraph_generation", fake_run_langgraph_generation)
    monkeypatch.setattr(chat_routes, "_schedule_background_task", _run_immediately)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _FakeHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.intent.classifier.wait_for_with_timeout",
        _no_timeout,
    )
    monkeypatch.setattr(
        "backend.services.llm_provider.runtime_client_builder.LLMClientFactory.get_client",
        fake_get_client,
    )

    client = TestClient(app)
    try:
        response = client.post(
            f"/tasks/{seeded['task_id']}/chat",
            json={"message": "Use Anthropic through the runtime path"},
        )
        assert response.status_code == 202, response.text
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()

    assert [call["purpose"] for call in credential_calls] == [
        "intent_classifier",
        "graph:conversation_main",
    ]
    assert [call["provider_model"] for call in factory_calls] == [
        ProviderModelRef(ANTHROPIC_PROVIDER_ID, model),
        ProviderModelRef(ANTHROPIC_PROVIDER_ID, model),
    ]
    assert all(call["api_key"] == "sk-anthropic" for call in factory_calls)
    assert all(call["kwargs"]["reasoning_effort"] == "high" for call in factory_calls)
    assert [call["method"] for call in client_calls] == ["chat_with_usage", "chat"]
    assert graph_calls
    runtime_selection = graph_calls[0]["graph_config"]["configurable"]["llm_runtime_selection"]
    assert runtime_selection == {
        "provider": ANTHROPIC_PROVIDER_ID,
        "model": model,
        "credential_ref": {"user_id": seeded["user_id"], "provider": ANTHROPIC_PROVIDER_ID},
        "reasoning_effort": None,
    }
    assert "sk-anthropic" not in repr(graph_calls)
