"""Deployment baseline tests for LLM factory routing and resolver delegation."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

import agent.providers.llm.factory.client_factory as factory_module
import backend.services.llm_provider.runtime_client_builder as runtime_builder_module
import backend.services.llm_provider.runtime_client_resolver as runtime_resolver_module
from agent.providers.llm.adapters.anthropic.client import AnthropicMessagesClient
from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.core.exceptions import (
    LLMProfileNotFoundError,
    LLMProviderNotFoundError,
)
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.factory.client_factory import LLMClientFactory


@pytest.fixture(autouse=True)
def restore_factory_registries() -> Iterator[None]:
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()

    LLMClientFactory.clear_registry()

    yield

    LLMClientFactory._provider_registry = original_provider_registry
    LLMClientFactory._registry = original_prefix_registry


def _register_builtin_providers() -> None:
    LLMClientFactory.clear_registry()
    factory_module._register_default_providers()


@pytest.mark.parametrize(
    ("provider_model", "mock_path", "expected_client_cls"),
    (
        (
            ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2"),
            "agent.providers.llm.adapters.openai.responses.client.openai",
            OpenAIResponsesClient,
        ),
        (
            ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-4o"),
            "agent.providers.llm.adapters.openai.chat.openai",
            OpenAIChatClient,
        ),
        (
            ProviderModelRef(ANTHROPIC_PROVIDER_ID, "claude-sonnet-5"),
            "agent.providers.llm.adapters.anthropic.client.anthropic",
            AnthropicMessagesClient,
        ),
    ),
)
def test_provider_model_refs_route_to_current_registered_adapters(
    provider_model: ProviderModelRef,
    mock_path: str,
    expected_client_cls: type[LLMClient],
) -> None:
    _register_builtin_providers()

    with patch(mock_path) as mock_sdk_module:
        mock_sdk_module.AsyncOpenAI.return_value = MagicMock()
        mock_sdk_module.AsyncAnthropic.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            provider_model=provider_model,
            api_key="key",
        )

    assert isinstance(client, expected_client_cls)
    assert client.model == provider_model.model


def test_factory_provider_registrations_are_the_current_adapter_authority() -> None:
    _register_builtin_providers()

    providers = LLMClientFactory.list_providers()

    assert providers == {
        ANTHROPIC_PROVIDER_ID: "AnthropicMessagesClient",
        OPENAI_PROVIDER_ID: "OpenAIChatClient, OpenAIResponsesClient",
    }
    assert LLMClientFactory.list_models(OPENAI_PROVIDER_ID, listable=True)
    assert LLMClientFactory.list_models(ANTHROPIC_PROVIDER_ID, listable=True)


def test_runtime_client_builder_delegates_adapter_construction_to_factory() -> None:
    resolver_source = inspect.getsource(
        runtime_resolver_module.LLMRuntimeClientResolver.get_client
    )
    resolver_module_source = inspect.getsource(runtime_resolver_module)
    builder_source = inspect.getsource(runtime_builder_module.LLMRuntimeClientBuilder.build)
    builder_module_source = inspect.getsource(runtime_builder_module)

    assert runtime_builder_module.LLMClientFactory is LLMClientFactory
    assert "self._client_builder.build" in resolver_source
    assert "LLMClientFactory.get_client" not in resolver_source
    assert "LLMClientFactory.get_client" in builder_source
    assert "provider_model=call_ref" in builder_source
    assert "agent.providers.llm.adapters." not in resolver_module_source
    assert "OpenAICompatibleChatClient" not in builder_module_source
    assert "AsyncOpenAI" not in resolver_module_source
    assert "AsyncOpenAI" not in builder_module_source
    assert "AsyncAnthropic" not in resolver_module_source
    assert "AsyncAnthropic" not in builder_module_source


def test_legacy_openai_model_only_prefix_fallback_is_compatibility_only() -> None:
    _register_builtin_providers()

    prefixes = LLMClientFactory.list_prefix_registrations()
    assert "gpt-5-preview" not in prefixes

    with patch(
        "agent.providers.llm.adapters.openai.responses.client.openai"
    ) as mock_openai:
        mock_openai.AsyncOpenAI.return_value = MagicMock()

        client = LLMClientFactory.get_client(
            model="gpt-5-preview",
            api_key="key",
        )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.model == "gpt-5-preview"


@pytest.mark.parametrize(
    ("provider", "model", "expected_error"),
    (
        ("mistral", "mistral-large", LLMProviderNotFoundError),
        (OPENAI_PROVIDER_ID, "text-davinci-003", LLMProfileNotFoundError),
        (ANTHROPIC_PROVIDER_ID, "claude-unknown", LLMProfileNotFoundError),
    ),
)
def test_unsupported_explicit_provider_or_model_failures_remain_loud(
    provider: str,
    model: str,
    expected_error: type[Exception],
) -> None:
    _register_builtin_providers()

    with pytest.raises(expected_error):
        LLMClientFactory.get_client(
            provider=provider,
            model=model,
            api_key="key",
        )
