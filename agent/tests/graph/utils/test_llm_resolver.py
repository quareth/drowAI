"""Tests for LLMClient resolver utility.

Tests cover:
- API key resolution from various sources
- Model resolution with fallback chain
- Error handling for missing configuration
- Factory integration
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from agent.graph.utils import llm_resolver as _resolver
from agent.providers.llm.core.base import LLMClient
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProviderNotFoundError,
)

resolve_llm_client = _resolver.resolve_llm_client
supports_usage_aware_streaming = _resolver.supports_usage_aware_streaming
_resolve_model = _resolver._resolve_model
_is_valid_string = _resolver._is_valid_string
DEFAULT_MODEL = _resolver.DEFAULT_MODEL
DEFAULT_ROLE = _resolver.DEFAULT_ROLE
ROLE_POST_TOOL_ARTICULATOR = _resolver.ROLE_POST_TOOL_ARTICULATOR
ProviderModelRef = _resolver.ProviderModelRef


# ---------------------------------------------------------------------------
# Mock Context
# ---------------------------------------------------------------------------


class MockGraphRuntimeContext:
    """Mock GraphRuntimeContext for testing."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.provider = provider
        self.model = model
        self.task_id = 1


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------


class TestIsValidString:
    """Tests for _is_valid_string helper."""
    
    def test_valid_string(self) -> None:
        """Test valid non-empty string."""
        assert _is_valid_string("test") is True
        assert _is_valid_string("a") is True
        assert _is_valid_string("test value") is True
    
    def test_empty_string(self) -> None:
        """Test empty string returns False."""
        assert _is_valid_string("") is False
    
    def test_whitespace_only(self) -> None:
        """Test whitespace-only string returns False."""
        assert _is_valid_string("   ") is False
        assert _is_valid_string("\t\n") is False
    
    def test_none(self) -> None:
        """Test None returns False."""
        assert _is_valid_string(None) is False
    
    def test_non_string_types(self) -> None:
        """Test non-string types return False."""
        assert _is_valid_string(123) is False
        assert _is_valid_string(["test"]) is False
        assert _is_valid_string({"key": "value"}) is False


class TestSupportsUsageAwareStreaming:
    """Tests for profile-gated usage-aware streaming support."""

    def test_true_when_client_method_and_profile_capability_exist(self) -> None:
        class StreamingClient:
            async def stream_chat_messages_with_usage(self) -> None:
                return None

        call_settings = SimpleNamespace(provider="openai", model="gpt-5.2")

        assert supports_usage_aware_streaming(StreamingClient(), call_settings) is True

    def test_false_when_client_method_is_missing(self) -> None:
        call_settings = SimpleNamespace(provider="openai", model="gpt-5.2")

        assert supports_usage_aware_streaming(object(), call_settings) is False

    def test_false_when_profile_capability_is_missing_or_unknown(self) -> None:
        class StreamingClient:
            async def stream_chat_messages_with_usage(self) -> None:
                return None

        call_settings = SimpleNamespace(provider="unknown", model="unknown")

        assert supports_usage_aware_streaming(StreamingClient(), call_settings) is False


# ---------------------------------------------------------------------------
# Model Resolution Tests
# ---------------------------------------------------------------------------


class TestResolveModel:
    """Tests for model resolution."""
    
    def test_resolves_from_metadata_model(self) -> None:
        """Test model resolved from metadata['model']."""
        metadata = {"model": "gpt-5.2"}
        
        result = _resolve_model(metadata, None, DEFAULT_MODEL)
        
        assert result == "gpt-5.2"
    
    def test_resolves_from_runtime_model(self) -> None:
        """Test model resolved from metadata['runtime_model']."""
        metadata = {"runtime_model": "gpt-5-mini"}
        
        result = _resolve_model(metadata, None, DEFAULT_MODEL)
        
        assert result == "gpt-5-mini"
    
    def test_resolves_from_context(self) -> None:
        """Test model resolved from context.model."""
        metadata: Dict[str, Any] = {}
        context = MockGraphRuntimeContext(model="gpt-5.2-pro")
        
        result = _resolve_model(metadata, context, DEFAULT_MODEL)
        
        assert result == "gpt-5.2-pro"
    
    def test_uses_default_when_not_found(self) -> None:
        """Test uses default model when none specified."""
        metadata: Dict[str, Any] = {}
        
        result = _resolve_model(metadata, None, "my-default")
        
        assert result == "my-default"
    
    def test_metadata_model_takes_precedence(self) -> None:
        """Test metadata['model'] takes precedence over others."""
        metadata = {
            "model": "gpt-5.2",
            "runtime_model": "gpt-5-mini",
        }
        context = MockGraphRuntimeContext(model="gpt-5.2-pro")
        
        result = _resolve_model(metadata, context, DEFAULT_MODEL)
        
        assert result == "gpt-5.2"
    
    def test_skips_empty_values(self) -> None:
        """Test skips empty/whitespace values."""
        metadata = {
            "model": "",
            "runtime_model": "   ",
        }
        context = MockGraphRuntimeContext(model="gpt-5")
        
        result = _resolve_model(metadata, context, DEFAULT_MODEL)
        
        assert result == "gpt-5"


# ---------------------------------------------------------------------------
# Runtime Services Resolution Tests
# ---------------------------------------------------------------------------


class FakeRuntimeClientResolver:
    """Capture resolver calls without constructing provider SDK clients."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def get_client(self, selection: Any, **kwargs: Any) -> Any:
        self.calls.append({"selection": selection, **kwargs})
        return self.client


def _runtime_config(
    resolver: FakeRuntimeClientResolver,
    *,
    selection: Optional[dict[str, Any]] = None,
    user_id: int = 7,
    task_id: int = 42,
) -> dict[str, Any]:
    selection_payload = selection or {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": user_id, "provider": "openai"},
        "reasoning_effort": None,
    }
    return {
        "configurable": {
            "runtime_services": SimpleNamespace(client_resolver=resolver),
            "llm_runtime_selection": selection_payload,
            "runtime_projection": {"user_id": user_id, "task_id": task_id},
        }
    }


class TestResolveLLMClient:
    """Tests for runtime-service-backed resolve_llm_client."""

    def test_resolves_client_from_runtime_services(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)

        result = resolve_llm_client(
            {"provider": "openai", "model": "gpt-5.2"},
            config=_runtime_config(resolver),
        )

        assert result is mock_client
        call = resolver.calls[0]
        assert call["selection"]["model"] == "gpt-5.2"
        assert call["runtime_user_id"] == 7
        assert call["task_id"] == 42
        assert call["purpose"] == "graph:conversation_main"
        assert call["target"].provider == "openai"
        assert call["target"].model == "gpt-5.2"

    def test_resolves_anthropic_conversation_target_from_runtime_services(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)
        selection = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "credential_ref": {"user_id": 7, "provider": "anthropic"},
            "reasoning_effort": None,
        }

        result = resolve_llm_client(
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            config=_runtime_config(resolver, selection=selection),
        )

        assert result is mock_client
        call = resolver.calls[0]
        assert call["selection"] == selection
        assert call["purpose"] == "graph:conversation_main"
        assert call["target"].provider == "anthropic"
        assert call["target"].model == "claude-sonnet-4-6"
        assert call["target"].reasoning_effort == "high"

    def test_articulation_role_inherits_selected_model_with_low_effort(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)

        resolve_llm_client(
            {"provider": "openai", "model": "gpt-5.2"},
            config=_runtime_config(resolver),
            role=ROLE_POST_TOOL_ARTICULATOR,
        )

        call = resolver.calls[0]
        assert call["target"].model == "gpt-5.2"
        assert call["target"].reasoning_effort == "low"
        assert call["purpose"] == "graph:post_tool_articulator"

    def test_raw_api_key_metadata_is_rejected_without_runtime_services(self) -> None:
        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client({"api_key": "sk-test", "model": "gpt-5.2"})

        assert "runtime services" in str(exc_info.value)
        assert "raw API keys" in str(exc_info.value)

    def test_runtime_services_require_runtime_selection(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        config = {"configurable": {"runtime_services": SimpleNamespace(client_resolver=FakeRuntimeClientResolver(mock_client))}}

        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client({"model": "gpt-5.2"}, config=config)

        assert "runtime selection" in str(exc_info.value)

    def test_credential_ref_user_id_is_not_runtime_authorization(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)
        config = {
            "configurable": {
                "runtime_services": SimpleNamespace(client_resolver=resolver),
                "llm_runtime_selection": {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "credential_ref": {"user_id": 7, "provider": "openai"},
                },
            }
        }

        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client({"model": "gpt-5.2"}, config=config)

        assert "Runtime user id is required" in str(exc_info.value)
        assert resolver.calls == []

    def test_checkpoint_metadata_runtime_selection_is_not_authority(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)
        config = {
            "configurable": {
                "runtime_services": SimpleNamespace(client_resolver=resolver),
                "runtime_projection": {"user_id": 7, "task_id": 42},
            }
        }

        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client(
                {
                    "model": "gpt-5.2",
                    "llm_runtime_selection": {
                        "provider": "openai",
                        "model": "gpt-5.2",
                        "credential_ref": {"user_id": 7, "provider": "openai"},
                    },
                },
                config=config,
            )

        assert "runtime selection" in str(exc_info.value)
        assert resolver.calls == []

    def test_checkpoint_metadata_user_id_is_not_runtime_authorization(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)
        config = {
            "configurable": {
                "runtime_services": SimpleNamespace(client_resolver=resolver),
                "llm_runtime_selection": {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "credential_ref": {"user_id": 7, "provider": "openai"},
                },
            }
        }

        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client({"model": "gpt-5.2", "user_id": 7}, config=config)

        assert "Runtime user id is required" in str(exc_info.value)
        assert resolver.calls == []

    def test_checkpoint_metadata_task_id_is_not_runtime_context(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)
        config = {
            "configurable": {
                "runtime_services": SimpleNamespace(client_resolver=resolver),
                "llm_runtime_selection": {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "credential_ref": {"user_id": 7, "provider": "openai"},
                },
                "runtime_projection": {"user_id": 7},
            }
        }

        resolve_llm_client({"model": "gpt-5.2", "task_id": 999}, config=config)

        assert resolver.calls[0]["task_id"] is None

    def test_unknown_role_fails_fast(self) -> None:
        mock_client = MagicMock(spec=LLMClient)
        resolver = FakeRuntimeClientResolver(mock_client)

        with pytest.raises(LLMConfigurationError) as exc_info:
            resolve_llm_client(
                {"model": "frontend-model"},
                config=_runtime_config(resolver),
                role="unknown-role",
            )

        assert "Unknown role" in str(exc_info.value)
        assert "conversation_main" in str(exc_info.value)
