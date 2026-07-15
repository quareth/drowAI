"""Provider-neutral LLM core contract tests using a fake provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import agent.providers.llm.factory.client_factory as factory_module
from agent.providers.llm.core.base import StructuredOutputSpec
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import LLMCapabilityNotSupportedError
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.core.identity import ProviderModelRef

from ..fakes import (
    FAKE_MODEL_ID,
    FAKE_PROVIDER_ID,
    FakeProviderClient,
    build_fake_profile_registry,
    fake_specific_tool_choice,
    fake_tool_spec,
)


@pytest.fixture(autouse=True)
def isolated_factory_registry():
    """Restore global factory registrations after each fake-provider test."""
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()

    LLMClientFactory.clear_registry()
    FakeProviderClient.reset_instances()

    yield

    LLMClientFactory._provider_registry = original_provider_registry
    LLMClientFactory._registry = original_prefix_registry
    FakeProviderClient.reset_instances()


def _register_fake_provider(monkeypatch) -> list[ProviderModelRef]:
    """Install the fake provider and isolated fake profile lookup."""
    registry = build_fake_profile_registry()
    lookups: list[ProviderModelRef] = []

    def require_model_profile(ref: ProviderModelRef):
        lookups.append(ref.normalized())
        return registry.require_model_profile(ref)

    monkeypatch.setattr(factory_module, "require_model_profile", require_model_profile)
    LLMClientFactory.register_provider(
        FAKE_PROVIDER_ID,
        FakeProviderClient,
        adapter_names=(FakeProviderClient.__name__,),
    )
    return lookups


def test_fake_provider_registers_with_explicit_provider_model_ref(monkeypatch) -> None:
    lookups = _register_fake_provider(monkeypatch)

    client = LLMClientFactory.get_client(
        provider_model=ProviderModelRef("Fake", "Fake-Chat"),
        api_key="fake-key",
        trace_id="trace-1",
    )

    assert isinstance(client, FakeProviderClient)
    assert client.api_key == "fake-key"
    assert client.model == "Fake-Chat"
    assert client.init_kwargs == {"trace_id": "trace-1"}
    assert lookups == [ProviderModelRef(FAKE_PROVIDER_ID, FAKE_MODEL_ID)]
    assert LLMClientFactory.list_providers() == {
        FAKE_PROVIDER_ID: FakeProviderClient.__name__,
    }


def test_fake_profiles_support_capability_required_and_failure_behavior() -> None:
    registry = build_fake_profile_registry()
    ref = ProviderModelRef(FAKE_PROVIDER_ID, FAKE_MODEL_ID)

    provider_profile = registry.require_provider_capability(
        FAKE_PROVIDER_ID,
        LLMCapability.CHAT,
    )
    model_profile = registry.require_model_capability(ref, LLMCapability.TOOLS)

    assert provider_profile.id == FAKE_PROVIDER_ID
    assert model_profile.ref == ref
    assert registry.resolve_context_window_tokens(ref) > 0
    assert registry.resolve_max_output_tokens(ref) > 0

    with pytest.raises(LLMCapabilityNotSupportedError):
        registry.require_provider_capability(
            FAKE_PROVIDER_ID,
            LLMCapability.REMOTE_CONVERSATION_LIFECYCLE,
        )
    with pytest.raises(LLMCapabilityNotSupportedError):
        registry.require_model_capability(ref, LLMCapability.PARALLEL_TOOLS)


@pytest.mark.asyncio
async def test_fake_provider_records_neutral_tool_contracts(monkeypatch) -> None:
    _register_fake_provider(monkeypatch)
    client = LLMClientFactory.get_client(
        provider=FAKE_PROVIDER_ID,
        model=FAKE_MODEL_ID,
        api_key="fake-key",
    )
    tool_spec = fake_tool_spec()
    tool_choice = fake_specific_tool_choice()

    result = await client.chat_with_tools(
        "system",
        "use a tool",
        tools=[tool_spec],
        tool_choice=tool_choice,
    )

    call = _last_call(client)
    assert call["method"] == "chat_with_tools"
    assert call["tools"] == [tool_spec]
    assert call["tool_choice"] == tool_choice
    assert not any(isinstance(tool, dict) for tool in call["tools"])
    assert result.tool_calls is not None
    assert result.tool_calls[0].name == tool_spec.name
    assert result.raw == {"provider": FAKE_PROVIDER_ID, "tool_count": 1}


@pytest.mark.asyncio
async def test_fake_provider_records_structured_output_contract(monkeypatch) -> None:
    _register_fake_provider(monkeypatch)
    client = LLMClientFactory.get_client(
        provider=FAKE_PROVIDER_ID,
        model=FAKE_MODEL_ID,
        api_key="fake-key",
    )
    spec = StructuredOutputSpec(
        name="fake_answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    response = await client.chat_with_usage(
        "system",
        "return structured data",
        structured_output=spec,
    )

    call = _last_call(client)
    assert call["method"] == "chat_with_usage"
    assert call["kwargs"]["structured_output"] is spec
    assert "response_format" not in call["kwargs"]
    assert "text" not in call["kwargs"]
    assert response.content == '{"answer":"fake"}'
    assert response.structured_output == {"answer": "fake"}


def test_graph_resolver_hands_fake_provider_ref_to_runtime_services() -> None:
    from agent.graph.utils import llm_resolver

    fake_client = FakeProviderClient(api_key="fake-key", model=FAKE_MODEL_ID)
    resolver_calls: list[dict[str, Any]] = []

    class FakeRuntimeClientResolver:
        def get_client(self, selection: Any, **kwargs: Any) -> FakeProviderClient:
            resolver_calls.append({"selection": selection, **kwargs})
            return fake_client

    runtime_services = SimpleNamespace(client_resolver=FakeRuntimeClientResolver())
    selection = SimpleNamespace(provider=FAKE_PROVIDER_ID, model=FAKE_MODEL_ID)

    resolved = llm_resolver.resolve_llm_client(
        {
            "provider": FAKE_PROVIDER_ID,
            "model": FAKE_MODEL_ID,
        },
        config={
            "configurable": {
                "runtime_services": runtime_services,
                "llm_runtime_selection": selection,
                "runtime_projection": {
                    "user_id": 7,
                    "task_id": 11,
                },
            }
        },
    )

    assert resolved is fake_client
    assert resolver_calls[0]["selection"] is selection
    target = resolver_calls[0]["target"]
    assert target.provider == FAKE_PROVIDER_ID
    assert target.model == FAKE_MODEL_ID
    assert resolver_calls[0]["runtime_user_id"] == 7
    assert resolver_calls[0]["task_id"] == 11
    assert "api_key" not in resolver_calls[0]


def _last_call(client: Any) -> dict[str, Any]:
    """Return the latest recorded fake-provider call."""
    assert isinstance(client, FakeProviderClient)
    assert client.calls
    return client.calls[-1]
