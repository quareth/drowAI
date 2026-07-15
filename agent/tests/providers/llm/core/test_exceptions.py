"""Tests for LLM provider exception classes.

Tests cover:
- Exception hierarchy
- Exception attributes
- String representations
"""

from __future__ import annotations

import pytest

from agent.providers.llm.core.exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMProviderError,
    LLMProviderNotFoundError,
    LLMRefusalError,
    LLMRefusalOutcome,
    LLMResponseError,
    LLMStructuredOutputParseError,
)


class TestLLMProviderError:
    """Tests for base LLMProviderError."""
    
    def test_create_with_message_only(self) -> None:
        """Test creating error with just message."""
        err = LLMProviderError("Something went wrong")
        
        assert err.message == "Something went wrong"
        assert err.provider is None
        assert str(err) == "Something went wrong"
    
    def test_create_with_provider(self) -> None:
        """Test creating error with provider name."""
        err = LLMProviderError("API failed", provider="OpenAI")
        
        assert err.message == "API failed"
        assert err.provider == "OpenAI"
        assert str(err) == "[OpenAI] API failed"
    
    def test_exception_hierarchy(self) -> None:
        """Test that all exceptions inherit from LLMProviderError."""
        assert issubclass(LLMConfigurationError, LLMProviderError)
        assert issubclass(LLMAPIError, LLMProviderError)
        assert issubclass(LLMResponseError, LLMProviderError)
        assert issubclass(LLMStructuredOutputParseError, LLMResponseError)
        assert issubclass(LLMProviderNotFoundError, LLMProviderError)
    
    def test_can_catch_as_base_exception(self) -> None:
        """Test that specific exceptions can be caught as base."""
        
        def raise_config_error() -> None:
            raise LLMConfigurationError("Bad config")
        
        # Should be catchable as LLMProviderError
        with pytest.raises(LLMProviderError):
            raise_config_error()
        
        # Should also be catchable as Exception
        with pytest.raises(Exception):
            raise_config_error()


class TestLLMConfigurationError:
    """Tests for LLMConfigurationError."""
    
    def test_create_basic(self) -> None:
        """Test creating configuration error."""
        err = LLMConfigurationError("Missing API key")
        
        assert err.message == "Missing API key"
        assert isinstance(err, LLMProviderError)
    
    def test_with_provider(self) -> None:
        """Test with provider name."""
        err = LLMConfigurationError("Invalid model", provider="Anthropic")
        
        assert str(err) == "[Anthropic] Invalid model"


class TestLLMAPIError:
    """Tests for LLMAPIError."""
    
    def test_create_basic(self) -> None:
        """Test creating API error."""
        err = LLMAPIError("Request failed")
        
        assert err.message == "Request failed"
        assert err.status_code is None
    
    def test_with_status_code(self) -> None:
        """Test with HTTP status code."""
        err = LLMAPIError("Rate limited", status_code=429)
        
        assert err.status_code == 429
    
    def test_with_all_attributes(self) -> None:
        """Test with all attributes."""
        err = LLMAPIError(
            "Server error",
            provider="OpenAI",
            status_code=500,
        )
        
        assert err.message == "Server error"
        assert err.provider == "OpenAI"
        assert err.status_code == 500
        assert str(err) == "[OpenAI] Server error"
    
    def test_preserves_cause(self) -> None:
        """Test that exception cause is preserved."""
        original = ValueError("Original error")
        
        try:
            raise LLMAPIError("Wrapped error") from original
        except LLMAPIError as e:
            assert e.__cause__ is original


class TestLLMResponseError:
    """Tests for LLMResponseError."""
    
    def test_create_basic(self) -> None:
        """Test creating response error."""
        err = LLMResponseError("Empty response")
        
        assert err.message == "Empty response"
        assert isinstance(err, LLMProviderError)
    
    def test_with_provider(self) -> None:
        """Test with provider name."""
        err = LLMResponseError("Malformed JSON", provider="OpenAI")
        
        assert str(err) == "[OpenAI] Malformed JSON"


class TestLLMRefusalError:
    """Tests for provider-neutral structured refusal compatibility."""

    def test_carries_outcome_and_legacy_attributes(self) -> None:
        usage = object()
        outcome = LLMRefusalOutcome(
            provider="anthropic",
            model="claude-fable-5",
            category="cyber",
            explanation="Request blocked.",
            response_id="msg_123",
            usage=usage,
            partial_content="Partial answer",
        )

        err = LLMRefusalError(
            "Anthropic declined the request",
            outcome=outcome,
            stop_details={"category": "cyber"},
        )

        assert err.outcome is outcome
        assert err.provider == "anthropic"
        assert err.model == "claude-fable-5"
        assert err.category == "cyber"
        assert err.explanation == "Request blocked."
        assert err.response_id == "msg_123"
        assert err.usage is usage
        assert err.partial_content == "Partial answer"
        assert err.stop_details == {"category": "cyber"}

    def test_legacy_constructor_builds_outcome(self) -> None:
        err = LLMRefusalError(
            "declined",
            provider="openai",
            model="gpt-5.6",
            category="content_filter",
        )

        assert err.outcome.provider == "openai"
        assert err.outcome.model == "gpt-5.6"
        assert err.category == "content_filter"


class TestLLMStructuredOutputParseError:
    """Tests for structured-output parse diagnostics."""

    def test_create_with_diagnostics(self) -> None:
        err = LLMStructuredOutputParseError(
            "Structured output is not valid JSON",
            provider="OpenAI",
            schema_name="post_tool_decision",
            parse_reason="json_decode_error",
            raw_content='{"next_action":"call_tool"',
            diagnostics={"response_id": "resp_123", "status": "incomplete"},
        )

        assert err.provider == "OpenAI"
        assert err.schema_name == "post_tool_decision"
        assert err.parse_reason == "json_decode_error"
        assert err.raw_content == '{"next_action":"call_tool"'
        assert err.diagnostics == {"response_id": "resp_123", "status": "incomplete"}


class TestLLMProviderNotFoundError:
    """Tests for LLMProviderNotFoundError."""
    
    def test_create_basic(self) -> None:
        """Test creating provider not found error."""
        err = LLMProviderNotFoundError("No provider for model")
        
        assert err.message == "No provider for model"
        assert err.model is None
        assert err.available_prefixes == []
    
    def test_with_model(self) -> None:
        """Test with model name."""
        err = LLMProviderNotFoundError(
            "Unknown model",
            model="claude-3-opus",
        )
        
        assert err.model == "claude-3-opus"
        assert "claude-3-opus" in str(err)
    
    def test_with_available_prefixes(self) -> None:
        """Test with available prefixes."""
        err = LLMProviderNotFoundError(
            "Unknown model",
            model="claude-3",
            available_prefixes=["gpt-4", "gpt-3.5"],
        )
        
        assert err.available_prefixes == ["gpt-4", "gpt-3.5"]
        error_str = str(err)
        assert "gpt-4" in error_str
        assert "gpt-3.5" in error_str
    
    def test_str_format(self) -> None:
        """Test string representation format."""
        err = LLMProviderNotFoundError(
            "No provider registered",
            model="test-model",
            available_prefixes=["a", "b"],
        )
        
        error_str = str(err)
        assert "No provider registered" in error_str
        assert "test-model" in error_str
        assert "a, b" in error_str
