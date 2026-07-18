"""Characterization tests for current token, context, and budget policy behavior."""

from __future__ import annotations

from agent.context.context_window_policy import (
    estimate_chat_history_tokens,
    evaluate_context_fit,
)
from agent.context.token_counter_registry import estimate_text_tokens
from agent.providers.llm.core.budget_policy import decide_output_budget
from core.llm.role_contracts import ROLE_CONTEXT_COMPRESSOR, ROLE_CONVERSATION_MAIN


def test_token_estimates_carry_provider_model_and_precision_provenance() -> None:
    openai_estimate = estimate_text_tokens(
        "Deployment-aware LLM routing baseline",
        provider="openai",
        model="gpt-5.2",
    )
    anthropic_estimate = estimate_text_tokens(
        "Deployment-aware Anthropic heuristic baseline",
        provider="anthropic",
        model="claude-sonnet-5",
    )

    assert openai_estimate.provider == "openai"
    assert openai_estimate.model == "gpt-5.2"
    assert openai_estimate.precision in {"exact", "approximate", "heuristic"}
    assert openai_estimate.strategy in {
        "tiktoken_model",
        "tiktoken_base_compatibility",
        "tiktoken_unavailable_heuristic",
    }
    assert anthropic_estimate.provider == "anthropic"
    assert anthropic_estimate.model == "claude-sonnet-5"
    assert anthropic_estimate.precision == "heuristic"
    assert anthropic_estimate.strategy == "anthropic_char_heuristic"
    assert anthropic_estimate.safety_margin_applied == 0.15


def test_chat_history_estimate_is_distinct_from_actual_usage_shape() -> None:
    estimate = estimate_chat_history_tokens(
        provider="anthropic",
        model="claude-sonnet-5",
        history=[
            {"role": "system", "content": "Keep the baseline exact."},
            {"role": "user", "content": "What changes for deployment targets?"},
        ],
        projected_user_message="Continue with guarded egress.",
    )

    assert estimate.provider == "anthropic"
    assert estimate.model == "claude-sonnet-5"
    assert estimate.strategy == "anthropic_char_heuristic"
    assert estimate.precision == "heuristic"
    assert not hasattr(estimate, "prompt_tokens")
    assert not hasattr(estimate, "completion_tokens")
    assert not hasattr(estimate, "total_tokens")


def test_context_fit_decision_recommends_compression_when_projected_total_overflows() -> None:
    decision = evaluate_context_fit(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        context_estimate_tokens=199_500,
        requested_output_tokens=1_000,
    )

    assert decision.provider == "anthropic"
    assert decision.model == "claude-haiku-4-5-20251001"
    assert decision.context_window_tokens == 200_000
    assert decision.fits is False
    assert decision.overflow_tokens == 500
    assert decision.recommended_action == "compress"


def test_openai_output_budget_clamps_while_anthropic_over_budget_fails() -> None:
    openai_decision = decide_output_budget(
        provider="openai",
        model="gpt-5.2",
        role=ROLE_CONVERSATION_MAIN,
        requested_max_output_tokens=64_000,
    )
    anthropic_decision = decide_output_budget(
        provider="anthropic",
        model="claude-sonnet-5",
        role=ROLE_CONVERSATION_MAIN,
        requested_max_output_tokens=999_999,
    )

    assert openai_decision.should_fail is False
    assert openai_decision.clamped is True
    assert openai_decision.accepted_max_tokens == openai_decision.model_max_output_tokens
    assert openai_decision.reason == "clamped_to_model_max_output"
    assert anthropic_decision.should_fail is True
    assert anthropic_decision.clamped is False
    assert anthropic_decision.accepted_max_tokens is None
    assert anthropic_decision.reason == "exceeds_model_max_output"


def test_context_budget_failure_happens_before_provider_call_budget_acceptance() -> None:
    decision = decide_output_budget(
        provider="openai",
        model="gpt-5.2",
        role=ROLE_CONTEXT_COMPRESSOR,
        requested_max_output_tokens=1_000,
        context_estimate_tokens=200_000,
    )

    assert decision.should_fail is True
    assert decision.reason == "context_window_exceeded"
    assert decision.context_fit is not None
    assert decision.context_fit.recommended_action == "compress"
