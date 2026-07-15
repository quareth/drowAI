"""Provider-aware factory resolution tests for tenant_baseline provider wiring."""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

import agent.providers.llm.factory.client_factory as factory_module
from agent.providers.llm.core.base import LLMClient, LLMResponse, ToolCallResult
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError, LLMProviderNotFoundError
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.adapters.anthropic.client import AnthropicMessagesClient
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient


class _PrefixFallbackClient(LLMClient):
    """Minimal client used to detect accidental legacy prefix routing."""

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        return self._model

    async def chat_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return self._model

    async def stream_chat_messages(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield self._model

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content=self._model)

    async def chat_messages_with_usage(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content=self._model)

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content=self._model, tool_calls=None, raw={})

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content=self._model, tool_calls=None, raw={})


class _ProviderPathClient(_PrefixFallbackClient):
    """Minimal client returned by provider-aware resolution."""


@pytest.fixture(autouse=True)
def clean_registries():
    """Restore factory state after each provider-resolution test."""
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()

    LLMClientFactory.clear_registry()

    yield

    LLMClientFactory._provider_registry = original_provider_registry
    LLMClientFactory._registry = original_prefix_registry


def _register_default_openai() -> None:
    LLMClientFactory.clear_registry()
    factory_module._register_default_providers()


def test_explicit_provider_model_ref_resolves_openai_responses_adapter() -> None:
    _register_default_openai()

    with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
        mock_openai.AsyncOpenAI.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            provider_model=ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2"),
            api_key="key",
        )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.model == "gpt-5.2"


def test_explicit_provider_and_model_resolves_openai_chat_adapter() -> None:
    _register_default_openai()

    with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
        mock_openai.AsyncOpenAI.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            provider="OpenAI",
            model="GPT-4O",
            api_key="key",
        )

    assert isinstance(client, OpenAIChatClient)
    assert client.model == "GPT-4O"


def test_explicit_openai_family_compatibility_preserves_raw_model() -> None:
    _register_default_openai()

    with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
        mock_openai.AsyncOpenAI.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5-preview",
            api_key="key",
        )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.model == "gpt-5-preview"


def test_explicit_provider_path_bypasses_legacy_prefix_matching() -> None:
    LLMClientFactory.register("gpt-5", _PrefixFallbackClient)
    LLMClientFactory.register_provider(
        OPENAI_PROVIDER_ID,
        lambda _profile: _ProviderPathClient,
        adapter_names=("_ProviderPathClient",),
    )

    client = LLMClientFactory.get_client(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        api_key="key",
    )

    assert type(client) is _ProviderPathClient
    assert client.model == "gpt-5.2"


def test_legacy_model_only_prefix_fallback_still_works_without_provider_registry() -> None:
    LLMClientFactory.register("gpt-4", _PrefixFallbackClient)

    client = LLMClientFactory.get_client(model="gpt-4o-mini", api_key="key")

    assert isinstance(client, _PrefixFallbackClient)
    assert client.model == "gpt-4o-mini"


def test_unknown_explicit_provider_raises_provider_not_found() -> None:
    _register_default_openai()

    with pytest.raises(LLMProviderNotFoundError, match="No provider registered"):
        LLMClientFactory.get_client(
            provider="mistral",
            model="mistral-large",
            api_key="key",
        )


@pytest.mark.parametrize(
    "model",
    ("claude-sonnet-5", "claude-fable-5", "claude-mythos-5"),
)
def test_explicit_anthropic_model_ref_resolves_messages_adapter(model: str) -> None:
    _register_default_openai()

    with patch("agent.providers.llm.adapters.anthropic.client.anthropic") as mock_anthropic:
        mock_anthropic.AsyncAnthropic.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            provider_model=ProviderModelRef(
                ANTHROPIC_PROVIDER_ID,
                model,
            ),
            api_key="key",
        )

    assert isinstance(client, AnthropicMessagesClient)
    assert client.model == model


def test_unknown_explicit_model_for_known_provider_raises_profile_not_found() -> None:
    _register_default_openai()

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        LLMClientFactory.get_client(
            provider=OPENAI_PROVIDER_ID,
            model="text-davinci-003",
            api_key="key",
        )


def test_unknown_anthropic_model_raises_profile_not_found() -> None:
    _register_default_openai()

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        LLMClientFactory.get_client(
            provider=ANTHROPIC_PROVIDER_ID,
            model="claude-unknown",
            api_key="key",
        )


def test_provider_aware_introspection_lists_providers_models_and_prefixes() -> None:
    _register_default_openai()

    providers = LLMClientFactory.list_providers()
    models = LLMClientFactory.list_models(OPENAI_PROVIDER_ID, listable=True)
    prefixes = LLMClientFactory.list_prefix_registrations()

    assert providers == {
        ANTHROPIC_PROVIDER_ID: "AnthropicMessagesClient",
        OPENAI_PROVIDER_ID: "OpenAIChatClient, OpenAIResponsesClient",
    }
    assert "gpt-5.2" in models
    assert "gpt-4o" not in models
    assert prefixes["gpt-5"] == "OpenAIResponsesClient"
    assert "gpt-5-preview" not in prefixes
