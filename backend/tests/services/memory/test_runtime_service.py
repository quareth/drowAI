"""Tests for backend-owned memory runtime provider boundaries."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.services.embeddings.base import EmbeddingModelRef, EmbeddingProfile
from backend.services.embeddings.selection_service import MemoryLLMRuntimeSelection
from backend.services.llm_provider.types import (
    CredentialNotFoundError,
    DeploymentRef,
    LLMCredentialRef,
)
from backend.services.memory.runtime_service import (
    MEMORY_EXTRACTION_GATE_ROLE,
    MEMORY_EXTRACTION_ROLE,
    MemoryRuntimeService,
)


class _FakeDB:
    def query(self, _model: Any) -> Any:
        class _Query:
            def filter(self, *_args: Any, **_kwargs: Any) -> "_Query":
                return self

            def first(self) -> Any:
                return SimpleNamespace(tenant_id=11, engagement_id=77)

        return _Query()


class _Resolver:
    def __init__(self) -> None:
        self.secret_calls: list[dict[str, Any]] = []
        self.client_calls: list[dict[str, Any]] = []

    def resolve_secret(self, selection: Any, **kwargs: Any) -> Any:
        self.secret_calls.append({"selection": selection, **kwargs})
        return SimpleNamespace(value="runtime-secret")

    def get_client(self, selection: Any, **kwargs: Any) -> str:
        self.client_calls.append({"selection": selection, **kwargs})
        return f"client:{kwargs['target'].role}"


class _EmbeddingProvider:
    profile = EmbeddingProfile(
        ref=EmbeddingModelRef(provider="openai", model="text-embedding-3-small"),
        dimensions=1536,
        vector_family="openai:text-embedding-3-small:1536",
    )
    dimensions = 1536

    async def embed(self, text: str) -> list[float]:
        return [float(len(text))] * self.dimensions

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] * self.dimensions for text in texts]


class _EmbeddingFactory:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    def create(self, selection: Any, *, api_key: str) -> _EmbeddingProvider:
        self._state["embedding_keys"].append(api_key)
        self._state["embedding_selections"].append(selection)
        return _EmbeddingProvider()


@pytest.mark.asyncio
async def test_run_extraction_uses_explicit_memory_targets_with_anthropic_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {
        "embedding_keys": [],
        "embedding_selections": [],
        "extract_kwargs": [],
        "service_clients": [],
    }

    mod_core = types.ModuleType("backend.models.core")

    class Task:
        id = 2

    mod_core.Task = Task
    monkeypatch.setitem(sys.modules, "backend.models.core", mod_core)

    class _MemoryStore:
        def __init__(self, db: Any, embedding_service: Any) -> None:
            self.db = db
            self.embedding_service = embedding_service

    monkeypatch.setattr("backend.services.memory.memory_store.MemoryStore", _MemoryStore)

    class _MemoryExtractionService:
        def __init__(self, _memory_store: Any, gate_client: Any, extraction_client: Any) -> None:
            state["service_clients"].append((gate_client, extraction_client))

        async def extract_if_needed(self, **kwargs: Any) -> list[Any]:
            state["extract_kwargs"].append(kwargs)
            return []

    monkeypatch.setattr(
        "backend.services.memory.memory_extraction.MemoryExtractionService",
        _MemoryExtractionService,
    )

    resolver = _Resolver()
    service = MemoryRuntimeService(
        client_resolver=resolver,
        embedding_factory=_EmbeddingFactory(state),
        env_getter=lambda key, default=None: {
            "MEMORY_EXTRACTION_GATE_MODEL": "gpt-5-nano",
            "MEMORY_EXTRACTION_MODEL": "gpt-5-mini",
        }.get(key, default),
    )
    selection = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "credential_ref": {"user_id": 1, "provider": "anthropic"},
        "reasoning_effort": "medium",
    }

    await service.run_extraction(
        db=_FakeDB(),
        selection=selection,
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert state["embedding_keys"] == ["runtime-secret"]
    assert state["embedding_selections"][0].provider == "openai"
    assert state["service_clients"] == [
        (f"client:{MEMORY_EXTRACTION_GATE_ROLE}", f"client:{MEMORY_EXTRACTION_ROLE}")
    ]
    targets = [call["target"] for call in resolver.client_calls]
    assert [(target.role, target.provider, target.model) for target in targets] == [
        (MEMORY_EXTRACTION_GATE_ROLE, "openai", "gpt-5-nano"),
        (MEMORY_EXTRACTION_ROLE, "openai", "gpt-5-mini"),
    ]
    assert resolver.client_calls[0]["selection"].to_dict() == {
        "provider": "openai",
        "model": "gpt-5-mini",
        "credential_ref": {"user_id": 1, "provider": "openai"},
        "reasoning_effort": None,
    }
    assert state["extract_kwargs"][0]["engagement_id"] == 77
    assert state["extract_kwargs"][0]["tenant_id"] == 11
    assert state["extract_kwargs"][0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_run_extraction_uses_memory_deployment_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {
        "embedding_keys": [],
        "embedding_selections": [],
        "extract_kwargs": [],
        "service_clients": [],
    }
    gate_deployment_id = str(uuid4())
    extraction_deployment_id = str(uuid4())

    mod_core = types.ModuleType("backend.models.core")

    class Task:
        id = 2

    mod_core.Task = Task
    monkeypatch.setitem(sys.modules, "backend.models.core", mod_core)

    class _MemoryStore:
        def __init__(self, db: Any, embedding_service: Any) -> None:
            self.db = db
            self.embedding_service = embedding_service

    monkeypatch.setattr("backend.services.memory.memory_store.MemoryStore", _MemoryStore)

    class _MemoryExtractionService:
        def __init__(self, _memory_store: Any, gate_client: Any, extraction_client: Any) -> None:
            state["service_clients"].append((gate_client, extraction_client))

        async def extract_if_needed(self, **kwargs: Any) -> list[Any]:
            state["extract_kwargs"].append(kwargs)
            return []

    monkeypatch.setattr(
        "backend.services.memory.memory_extraction.MemoryExtractionService",
        _MemoryExtractionService,
    )

    class _SelectionService:
        def resolve_embedding_selection(self, *, user_id: int) -> Any:
            return SimpleNamespace(
                provider="openai",
                model="text-embedding-3-small",
                credential_ref=LLMCredentialRef(user_id=user_id, provider="openai"),
                dimensions=1536,
                vector_family="openai:text-embedding-3-small:1536",
            )

        def resolve_memory_llm_selection(
            self,
            *,
            user_id: int,
        ) -> MemoryLLMRuntimeSelection:
            return MemoryLLMRuntimeSelection(
                provider="openai",
                gate_model="gpt-5-nano",
                extraction_model="gpt-5-mini",
                credential_ref=LLMCredentialRef(user_id=user_id, provider="openai"),
                gate_deployment_ref=DeploymentRef(gate_deployment_id, 2),
                extraction_deployment_ref=DeploymentRef(extraction_deployment_id, 3),
            )

    resolver = _Resolver()
    service = MemoryRuntimeService(
        client_resolver=resolver,
        embedding_factory=_EmbeddingFactory(state),
        selection_service=_SelectionService(),  # type: ignore[arg-type]
    )

    await service.run_extraction(
        db=_FakeDB(),
        selection={
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": str(uuid4()),
                "expected_revision": 1,
            },
            "preferred_route_id": None,
            "reasoning_effort": None,
            "legacy_provider": "openai",
            "legacy_model": "gpt-5.2",
        },
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    runtime_payloads = [call["selection"].to_dict() for call in resolver.client_calls]
    assert runtime_payloads == [
        {
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": gate_deployment_id,
                "expected_revision": 2,
            },
            "preferred_route_id": None,
            "reasoning_effort": None,
            "legacy_provider": "openai",
            "legacy_model": "gpt-5-nano",
        },
        {
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": extraction_deployment_id,
                "expected_revision": 3,
            },
            "preferred_route_id": None,
            "reasoning_effort": None,
            "legacy_provider": "openai",
            "legacy_model": "gpt-5-mini",
        },
    ]
    assert [(call["target"].role, call["target"].model) for call in resolver.client_calls] == [
        (MEMORY_EXTRACTION_GATE_ROLE, "gpt-5-nano"),
        (MEMORY_EXTRACTION_ROLE, "gpt-5-mini"),
    ]


@pytest.mark.asyncio
async def test_retrieve_summary_refuses_user_scope_mismatch() -> None:
    resolver = _Resolver()
    service = MemoryRuntimeService(client_resolver=resolver)
    selection = {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 1, "provider": "openai"},
        "reasoning_effort": "medium",
    }

    summary = await service.retrieve_summary(
        selection=selection,
        runtime_user_id=1,
        task_id=2,
        user_id=2,
        query="preferences",
        max_results=5,
        max_chars=200,
    )

    assert summary == ""
    assert resolver.secret_calls == []


@pytest.mark.asyncio
async def test_run_extraction_refuses_credential_user_mismatch() -> None:
    resolver = _Resolver()
    service = MemoryRuntimeService(client_resolver=resolver)
    selection = {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 2, "provider": "openai"},
        "reasoning_effort": "medium",
    }

    await service.run_extraction(
        db=_FakeDB(),
        selection=selection,
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert resolver.secret_calls == []
    assert resolver.client_calls == []


@pytest.mark.asyncio
async def test_anthropic_chat_noops_when_openai_memory_credential_is_missing() -> None:
    class _MissingCredentialResolver(_Resolver):
        def get_credential_ref(self, _user_id: int, provider: str) -> Any:
            raise CredentialNotFoundError(f"{provider} credential is not configured")

        def resolve_secret(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("resolve_secret should not be called")

        def get_client(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("get_client should not be called")

    resolver = _MissingCredentialResolver()
    service = MemoryRuntimeService(client_resolver=resolver)
    selection = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "credential_ref": {"user_id": 1, "provider": "anthropic"},
        "reasoning_effort": "medium",
    }

    await service.run_extraction(
        db=_FakeDB(),
        selection=selection,
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert resolver.secret_calls == []
    assert resolver.client_calls == []
