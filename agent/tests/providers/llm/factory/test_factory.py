"""Tests for LLMClientFactory.

Tests cover:
- Provider-aware OpenAI resolution
- Legacy prefix registration and retrieval
- Error handling for unknown models
- Error handling for invalid configuration
- Prefix matching (longest match wins)
- Thread safety
- Registry introspection
"""

from __future__ import annotations

import threading
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import MagicMock, patch

import pytest

import agent.providers.llm.factory.client_factory as factory_module
from agent.providers.llm.core.base import LLMClient, LLMResponse, ToolCallResult
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.core.exceptions import LLMConfigurationError, LLMProviderNotFoundError
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
from agent.providers.llm.profiles.registry import (
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    OPENAI_API_SURFACE_RESPONSES,
    list_model_profiles,
)


# ---------------------------------------------------------------------------
# Mock Provider for Testing
# ---------------------------------------------------------------------------


class MockLLMClient(LLMClient):
    """Mock LLMClient for testing factory behavior."""
    
    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self._api_key = api_key
        self._model = model
        self._extra_kwargs = kwargs
    
    @property
    def model(self) -> str:
        return self._model
    
    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        return f"Mock response for {self._model}"
    
    async def chat_messages(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        return f"Mock messages response for {self._model}"
    
    async def stream_chat_messages(
        self, messages: List[Dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[str]:
        yield f"Mock stream for {self._model}"
    
    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(
            content=f"Mock tool response for {self._model}",
            tool_calls=None,
            raw={},
        )

    async def chat_with_usage(
        self, system_prompt: str, user_prompt: str, **kwargs: Any
    ) -> LLMResponse:
        return LLMResponse(content=f"Mock response for {self._model}")

    async def chat_messages_with_usage(
        self, messages: List[Dict[str, Any]], **kwargs: Any
    ) -> LLMResponse:
        return LLMResponse(content=f"Mock messages response for {self._model}")

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(
            content=f"Mock tool response for {self._model}",
            tool_calls=None,
            raw={},
        )


class AnotherMockClient(MockLLMClient):
    """Another mock client to test different provider routing."""
    pass


def _register_default_providers_for_test() -> None:
    """Restore built-in provider registrations after the autouse registry cleanup."""
    LLMClientFactory.clear_registry()
    factory_module._register_default_providers()


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure clean registry state before and after each test."""
    # Store original registries
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_registry = LLMClientFactory._registry.copy()
    
    # Clear for test
    LLMClientFactory.clear_registry()
    
    yield
    
    # Restore original registries
    LLMClientFactory._provider_registry = original_provider_registry
    LLMClientFactory._registry = original_registry


# ---------------------------------------------------------------------------
# Registration Tests
# ---------------------------------------------------------------------------


class TestLLMClientFactoryRegistration:
    """Tests for provider registration."""
    
    def test_register_provider(self) -> None:
        """Test basic provider registration."""
        LLMClientFactory.register("test-model", MockLLMClient)
        
        assert LLMClientFactory.is_registered("test-model")
        providers = LLMClientFactory.list_prefix_registrations()
        assert "test-model" == list(providers.keys())[0]
        assert providers["test-model"] == "MockLLMClient"
    
    def test_register_multiple_providers(self) -> None:
        """Test registering multiple providers for different prefixes."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        LLMClientFactory.register("claude", AnotherMockClient)
        
        providers = LLMClientFactory.list_prefix_registrations()
        assert len(providers) == 2
        assert providers["gpt-4"] == "MockLLMClient"
        assert providers["claude"] == "AnotherMockClient"
    
    def test_register_overwrites_existing(self) -> None:
        """Test that registering same prefix overwrites previous."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        LLMClientFactory.register("gpt-4", AnotherMockClient)
        
        providers = LLMClientFactory.list_prefix_registrations()
        assert providers["gpt-4"] == "AnotherMockClient"
    
    def test_register_case_insensitive(self) -> None:
        """Test that prefixes are normalized to lowercase."""
        LLMClientFactory.register("GPT-4", MockLLMClient)
        
        assert LLMClientFactory.is_registered("gpt-4")
        assert LLMClientFactory.is_registered("GPT-4")
    
    def test_register_invalid_provider_class(self) -> None:
        """Test that non-LLMClient classes are rejected."""
        with pytest.raises(TypeError, match="must be a subclass of LLMClient"):
            LLMClientFactory.register("invalid", str)  # type: ignore
    
    def test_register_empty_prefix_rejected(self) -> None:
        """Test that empty prefixes are rejected."""
        with pytest.raises(ValueError, match="prefix cannot be empty"):
            LLMClientFactory.register("", MockLLMClient)
        
        with pytest.raises(ValueError, match="prefix cannot be empty"):
            LLMClientFactory.register("   ", MockLLMClient)
    
    def test_unregister_provider(self) -> None:
        """Test unregistering a provider."""
        LLMClientFactory.register("test-model", MockLLMClient)
        assert LLMClientFactory.is_registered("test-model")
        
        result = LLMClientFactory.unregister("test-model")
        assert result is True
        assert not LLMClientFactory.is_registered("test-model")
    
    def test_unregister_nonexistent(self) -> None:
        """Test unregistering a prefix that doesn't exist."""
        result = LLMClientFactory.unregister("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Client Creation Tests
# ---------------------------------------------------------------------------


class TestLLMClientFactoryGetClient:
    """Tests for client creation via get_client()."""
    
    def test_get_client_creates_instance(self) -> None:
        """Test basic client creation."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        client = LLMClientFactory.get_client(model="gpt-4", api_key="test-key")
        
        assert isinstance(client, MockLLMClient)
        assert client.model == "gpt-4"
    
    def test_get_client_passes_kwargs(self) -> None:
        """Test that extra kwargs are passed to provider constructor."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        client = LLMClientFactory.get_client(
            model="gpt-4",
            api_key="test-key",
            temperature=0.5,
            custom_option="value",
        )
        
        assert isinstance(client, MockLLMClient)
        assert client._extra_kwargs["temperature"] == 0.5
        assert client._extra_kwargs["custom_option"] == "value"
    
    def test_get_client_prefix_matching(self) -> None:
        """Test that prefix matching works for model variants."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        # All these should match "gpt-4" prefix
        client1 = LLMClientFactory.get_client(model="gpt-4", api_key="key")
        client2 = LLMClientFactory.get_client(model="gpt-4o", api_key="key")
        client3 = LLMClientFactory.get_client(model="gpt-4o-mini", api_key="key")
        client4 = LLMClientFactory.get_client(model="gpt-4-turbo-preview", api_key="key")
        
        assert all(isinstance(c, MockLLMClient) for c in [client1, client2, client3, client4])
    
    def test_get_client_longest_prefix_wins(self) -> None:
        """Test that longer prefix matches take precedence."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        LLMClientFactory.register("gpt-4o", AnotherMockClient)
        
        # "gpt-4" should match MockLLMClient
        client1 = LLMClientFactory.get_client(model="gpt-4", api_key="key")
        assert isinstance(client1, MockLLMClient)
        
        # "gpt-4-turbo" should match "gpt-4" (shorter prefix)
        client2 = LLMClientFactory.get_client(model="gpt-4-turbo", api_key="key")
        assert isinstance(client2, MockLLMClient)
        
        # "gpt-4o" and "gpt-4o-mini" should match "gpt-4o" (longer prefix)
        client3 = LLMClientFactory.get_client(model="gpt-4o", api_key="key")
        assert isinstance(client3, AnotherMockClient)
        
        client4 = LLMClientFactory.get_client(model="gpt-4o-mini", api_key="key")
        assert isinstance(client4, AnotherMockClient)
    
    def test_get_client_case_insensitive_model(self) -> None:
        """Test that model matching is case-insensitive."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        client = LLMClientFactory.get_client(model="GPT-4", api_key="key")
        assert isinstance(client, MockLLMClient)


class TestLLMClientFactoryDefaultOpenAIRouting:
    """Characterization tests for built-in OpenAI model-only routing."""

    @pytest.mark.parametrize(
        ("model", "expected_client_cls"),
        [
            ("gpt-5", OpenAIResponsesClient),
            ("gpt-5-mini", OpenAIResponsesClient),
            ("gpt-5-nano", OpenAIResponsesClient),
            ("gpt-5-pro", OpenAIResponsesClient),
            ("gpt-5.1", OpenAIResponsesClient),
            ("gpt-5.2", OpenAIResponsesClient),
            ("gpt-5.2-pro", OpenAIResponsesClient),
            ("gpt-4o", OpenAIChatClient),
            ("gpt-4o-mini", OpenAIChatClient),
            ("gpt-4", OpenAIChatClient),
            ("gpt-3.5-turbo", OpenAIChatClient),
        ],
    )
    def test_default_openai_models_resolve_to_current_adapter(
        self,
        model: str,
        expected_client_cls: type[LLMClient],
    ) -> None:
        """Default model-only compatibility keeps today's OpenAI adapter selection."""
        _register_default_providers_for_test()

        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_chat_openai, patch(
            "agent.providers.llm.adapters.openai.responses.client.openai"
        ) as mock_responses_openai:
            mock_chat_openai.AsyncOpenAI.return_value = MagicMock()
            mock_responses_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model=model, api_key="key")

        assert isinstance(client, expected_client_cls)
        assert client.model == model

    @pytest.mark.parametrize("model", ["gpt-5.2", "GPT-5.2", "gpt-5-preview"])
    def test_default_openai_routing_preserves_requested_model_string(
        self,
        model: str,
    ) -> None:
        """Lookup is case-insensitive, but adapter construction keeps the raw model."""
        _register_default_providers_for_test()

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_responses_openai:
            mock_responses_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model=model, api_key="key")

        assert isinstance(client, OpenAIResponsesClient)
        assert client.model == model

    def test_default_openai_family_compatibility_routes_non_catalog_gpt5_model(
        self,
    ) -> None:
        """A non-registered GPT-5 family model still routes through legacy prefix fallback."""
        _register_default_providers_for_test()
        assert "gpt-5-preview" not in LLMClientFactory.list_prefix_registrations()

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_responses_openai:
            mock_responses_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model="gpt-5-preview", api_key="key")

        assert isinstance(client, OpenAIResponsesClient)
        assert client.model == "gpt-5-preview"

    def test_default_openai_prefix_registrations_match_exact_profiles(self) -> None:
        """Legacy OpenAI prefixes are derived from exact model profiles."""
        _register_default_providers_for_test()

        registrations = LLMClientFactory.list_prefix_registrations()
        exact_profiles = list_model_profiles(provider_id=OPENAI_PROVIDER_ID)

        assert set(registrations) == {profile.ref.model for profile in exact_profiles}
        for profile in exact_profiles:
            if profile.api_surface == OPENAI_API_SURFACE_RESPONSES:
                expected_client_cls = OpenAIResponsesClient
            elif profile.api_surface == OPENAI_API_SURFACE_CHAT_COMPLETIONS:
                expected_client_cls = OpenAIChatClient
            else:
                raise AssertionError(f"Unexpected API surface: {profile.api_surface}")
            assert registrations[profile.ref.model] == expected_client_cls.__name__

    def test_default_openai_unknown_model_uses_provider_not_found_error(self) -> None:
        """Unknown model-only resolution outside OpenAI families keeps current error type."""
        _register_default_providers_for_test()

        with pytest.raises(LLMProviderNotFoundError) as exc_info:
            LLMClientFactory.get_client(model="claude-3-opus", api_key="key")

        assert exc_info.value.model == "claude-3-opus"
        assert "gpt-5" in exc_info.value.available_prefixes
        assert "gpt-4" in exc_info.value.available_prefixes

    def test_empty_api_key_fails_before_default_adapter_construction(self) -> None:
        """API-key validation happens before the selected adapter can be constructed."""
        _register_default_providers_for_test()

        with patch.object(OpenAIResponsesClient, "__init__") as mock_init:
            with pytest.raises(LLMConfigurationError, match="API key is required"):
                LLMClientFactory.get_client(model="gpt-5", api_key="")

        mock_init.assert_not_called()


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestLLMClientFactoryErrors:
    """Tests for error handling."""
    
    def test_raises_for_unknown_model(self) -> None:
        """Test that unknown models raise LLMProviderNotFoundError."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        with pytest.raises(LLMProviderNotFoundError) as exc_info:
            LLMClientFactory.get_client(model="claude-3", api_key="key")
        
        assert "claude-3" in str(exc_info.value)
        assert exc_info.value.model == "claude-3"
        assert "gpt-4" in exc_info.value.available_prefixes
    
    def test_raises_for_empty_registry(self) -> None:
        """Test that empty registry raises LLMProviderNotFoundError."""
        # Registry is already cleared by fixture
        
        with pytest.raises(LLMProviderNotFoundError) as exc_info:
            LLMClientFactory.get_client(model="any-model", api_key="key")
        
        assert exc_info.value.available_prefixes == []
    
    def test_raises_for_empty_api_key(self) -> None:
        """Test that empty API key raises LLMConfigurationError."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        with pytest.raises(LLMConfigurationError, match="API key is required"):
            LLMClientFactory.get_client(model="gpt-4", api_key="")
        
        with pytest.raises(LLMConfigurationError, match="API key is required"):
            LLMClientFactory.get_client(model="gpt-4", api_key="   ")
    
    def test_raises_for_none_api_key(self) -> None:
        """Test that None API key raises LLMConfigurationError."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        with pytest.raises(LLMConfigurationError, match="API key is required"):
            LLMClientFactory.get_client(model="gpt-4", api_key=None)  # type: ignore
    
    def test_raises_for_empty_model(self) -> None:
        """Test that empty model raises LLMConfigurationError."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        with pytest.raises(LLMConfigurationError, match="Model identifier is required"):
            LLMClientFactory.get_client(model="", api_key="key")
        
        with pytest.raises(LLMConfigurationError, match="Model identifier is required"):
            LLMClientFactory.get_client(model="   ", api_key="key")
    
    def test_raises_for_none_model(self) -> None:
        """Test that None model raises LLMConfigurationError."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        with pytest.raises(LLMConfigurationError, match="Model identifier is required"):
            LLMClientFactory.get_client(model=None, api_key="key")  # type: ignore


# ---------------------------------------------------------------------------
# Thread Safety Tests
# ---------------------------------------------------------------------------


class TestLLMClientFactoryThreadSafety:
    """Tests for thread safety."""
    
    def test_concurrent_registration(self) -> None:
        """Test that concurrent registrations don't cause data corruption."""
        errors: List[Exception] = []
        
        def register_provider(prefix: str) -> None:
            try:
                for _ in range(100):
                    LLMClientFactory.register(prefix, MockLLMClient)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=register_provider, args=(f"prefix-{i}",))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(LLMClientFactory.list_prefix_registrations()) == 10
    
    def test_concurrent_get_client(self) -> None:
        """Test that concurrent client creation works correctly."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        errors: List[Exception] = []
        clients: List[LLMClient] = []
        lock = threading.Lock()
        
        def create_client() -> None:
            try:
                client = LLMClientFactory.get_client(model="gpt-4", api_key="key")
                with lock:
                    clients.append(client)
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [threading.Thread(target=create_client) for _ in range(50)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(clients) == 50
        assert all(isinstance(c, MockLLMClient) for c in clients)


# ---------------------------------------------------------------------------
# Introspection Tests
# ---------------------------------------------------------------------------


class TestLLMClientFactoryIntrospection:
    """Tests for registry introspection methods."""
    
    def test_list_providers_empty(self) -> None:
        """Test list_providers with empty registry."""
        providers = LLMClientFactory.list_providers()
        assert providers == {}
    
    def test_list_prefix_registrations_with_entries(self) -> None:
        """Test legacy prefix introspection returns all registered prefixes."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        LLMClientFactory.register("gpt-3.5", MockLLMClient)
        LLMClientFactory.register("claude", AnotherMockClient)
        
        providers = LLMClientFactory.list_prefix_registrations()
        
        assert len(providers) == 3
        assert providers["gpt-4"] == "MockLLMClient"
        assert providers["gpt-3.5"] == "MockLLMClient"
        assert providers["claude"] == "AnotherMockClient"
    
    def test_is_registered(self) -> None:
        """Test is_registered method."""
        assert not LLMClientFactory.is_registered("gpt-4")
        
        LLMClientFactory.register("gpt-4", MockLLMClient)
        
        assert LLMClientFactory.is_registered("gpt-4")
        assert LLMClientFactory.is_registered("GPT-4")  # Case insensitive
        assert not LLMClientFactory.is_registered("gpt-3")
    
    def test_clear_registry(self) -> None:
        """Test clearing the registry."""
        LLMClientFactory.register("gpt-4", MockLLMClient)
        LLMClientFactory.register("claude", AnotherMockClient)
        
        assert len(LLMClientFactory.list_prefix_registrations()) == 2
        
        LLMClientFactory.clear_registry()
        
        assert len(LLMClientFactory.list_prefix_registrations()) == 0
        assert not LLMClientFactory.is_registered("gpt-4")
