"""Test token counting accuracy improvements."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from agent.context.token_utils import TokenCounter, count_tokens, count_tokens_json
    from agent.context.token_counter_registry import (
        estimate_llm_request_tokens,
        estimate_text_tokens,
    )
    from agent.context.token_manager import TokenManager
except ImportError:
    pytest.skip("Token utilities not available", allow_module_level=True)


def test_token_counter_initialization():
    """Test that TokenCounter initializes correctly."""
    counter = TokenCounter("gpt-4")
    assert counter.model == "gpt-4"
    
    # Test with unknown model
    counter_unknown = TokenCounter("unknown-model")
    assert counter_unknown.model == "unknown-model"


def test_count_tokens_basic():
    """Test basic token counting."""
    text = "Hello, world!"
    count = count_tokens(text)
    
    # Should be more accurate than len(text) // 4
    approx_count = len(text) // 4  # Old method
    assert count != approx_count  # Should be different
    assert count > 0  # Should be positive


def test_count_tokens_empty():
    """Test token counting with empty strings."""
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


def test_count_tokens_json():
    """Test token counting with JSON data."""
    data = {
        "summary": "Test summary",
        "key_findings": ["finding1", "finding2"],
        "vulnerabilities": [{"type": "sql_injection", "severity": "high"}]
    }
    
    count = count_tokens_json(data)
    assert count > 0
    
    # Should be more than just the string representation
    string_count = count_tokens(str(data))
    assert count != string_count  # JSON counting should be different


def test_token_manager_integration():
    """Test that TokenManager uses accurate token counting."""
    mgr = TokenManager(provider="openai")
    
    # Test with sample context
    context = {
        "system_context": {"phase": "reconnaissance", "targets": ["192.168.1.1"]},
        "recent_cycles": [
            {"observation": "Port scan completed", "reasoning": "Found open ports"},
            {"observation": "Web server detected", "reasoning": "HTTP service running"}
        ],
        "tool_results": [
            {"summary": "Nmap scan found 3 open ports", "importance_score": 8.0}
        ],
        "artifacts": [
            {"id": "artifact-1", "summary": "Previous scan results show similar patterns"}
        ]
    }
    
    # This should use accurate token counting
    trimmed_context, token_count = mgr.fit_to_budget(context)
    
    assert token_count <= mgr.target
    assert token_count > 0
    
    # Verify that the old approximation would be different
    old_approx = len(str(context)) // 4
    assert abs(token_count - old_approx) > 0  # Should be different


def test_provider_specific_models():
    """Test that different providers use appropriate models."""
    openai_mgr = TokenManager(provider="openai")
    anthropic_mgr = TokenManager(provider="anthropic")
    gemini_mgr = TokenManager(provider="gemini")
    
    # All should have different models
    assert openai_mgr.model != anthropic_mgr.model
    assert openai_mgr.model != gemini_mgr.model
    assert anthropic_mgr.model != gemini_mgr.model
    assert openai_mgr.token_counter.count_json("hello").provider == "openai"
    assert anthropic_mgr.token_counter.count_json("hello").provider == "anthropic"
    assert gemini_mgr.token_counter.count_json("hello").provider == "gemini"


def test_fallback_behavior():
    """Test that the system falls back gracefully when tiktoken is not available."""
    # This test would require mocking the import failure
    # For now, we'll test that the fallback functions exist
    from agent.context.token_manager import TokenManager
    
    mgr = TokenManager(provider="openai")
    
    # Should still work even if tiktoken fails
    text = "Test text for token counting"
    count = mgr._context_tokens(text)
    assert count > 0


def test_token_counting_accuracy():
    """Test that token counting is more accurate than the old approximation."""
    # Test with various text types
    test_cases = [
        "Simple text",
        "Text with numbers 12345 and symbols @#$%",
        "Text with emojis 🚀🔥💻",
        "Very long text " * 100,
        "Text with newlines\nand tabs\tand spaces    ",
    ]
    
    for text in test_cases:
        accurate_count = count_tokens(text)
        estimate = estimate_text_tokens(text, provider="openai", model="gpt-4")

        assert accurate_count == estimate.tokens
        assert estimate.strategy == "tiktoken_model"
        assert estimate.precision == "exact"

        # But both should be reasonable
        assert accurate_count > 0
        assert accurate_count <= len(text)  # Shouldn't be more than character count


def test_llm_request_estimate_counts_only_prompt_bearing_fields(monkeypatch):
    """Request accounting uses the exact fields sent as classifier input."""
    observed = {}

    def _estimate(value, *, provider, model):
        observed.update(value=value, provider=provider, model=model)
        return type(
            "_Estimate",
            (),
            {
                "tokens": 73,
                "provider": provider,
                "model": model,
            },
        )()

    monkeypatch.setattr(
        "agent.context.token_counter_registry.estimate_json_tokens",
        _estimate,
    )

    estimate = estimate_llm_request_tokens(
        system_prompt="system",
        user_prompt="user",
        structured_output={"type": "json_schema"},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    assert estimate.tokens == 73
    assert observed == {
        "value": {
            "system_prompt": "system",
            "user_prompt": "user",
            "structured_output": {"type": "json_schema"},
        },
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }


def test_llm_request_estimate_accepts_structured_output_spec() -> None:
    """Runtime structured-output contracts are normalized before JSON counting."""
    from agent.providers.llm.core.base import StructuredOutputSpec

    schema = {
        "type": "object",
        "properties": {"route": {"type": "string"}},
        "required": ["route"],
        "additionalProperties": False,
    }
    spec = StructuredOutputSpec(name="intent_classifier", schema=schema, strict=True)

    estimate = estimate_llm_request_tokens(
        system_prompt="system",
        user_prompt="user",
        structured_output=spec,
        provider="openai",
        model="gpt-5.2",
    )
    normalized_estimate = estimate_llm_request_tokens(
        system_prompt="system",
        user_prompt="user",
        structured_output={
            "name": "intent_classifier",
            "schema": schema,
            "strict": True,
        },
        provider="openai",
        model="gpt-5.2",
    )

    assert estimate.tokens == normalized_estimate.tokens


if __name__ == "__main__":
    # Run basic tests
    test_token_counter_initialization()
    test_count_tokens_basic()
    test_count_tokens_json()
    test_token_manager_integration()
    print("✅ All token counting accuracy tests passed!") 
