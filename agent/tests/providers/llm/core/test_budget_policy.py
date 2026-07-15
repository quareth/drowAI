"""Tests for provider/model output and context budget policy decisions."""

from agent.context.context_window_policy import evaluate_context_fit
from agent.providers.llm.core.budget_policy import decide_output_budget
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID
from agent.providers.llm.profiles import ANTHROPIC_LISTABLE_MODEL_IDS


def test_openai_intent_classifier_budget_preserves_existing_default() -> None:
    decision = decide_output_budget(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        role="intent_classifier",
        requested_max_output_tokens=32_000,
    )

    assert decision.should_fail is False
    assert decision.clamped is False
    assert decision.accepted_max_tokens == 32_000
    assert decision.reason == "accepted"


def test_openai_legacy_budget_clamps_above_profile_max_output() -> None:
    decision = decide_output_budget(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        role="intent_classifier",
        requested_max_output_tokens=64_000,
    )

    assert decision.should_fail is False
    assert decision.clamped is True
    assert decision.accepted_max_tokens == 32_000
    assert decision.reason == "clamped_to_model_max_output"


def test_openai_chat_completions_budget_clamps_to_legacy_adapter_ceiling() -> None:
    decision = decide_output_budget(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-4o",
        role="conversation_main",
        requested_max_output_tokens=32_000,
    )

    assert decision.should_fail is False
    assert decision.clamped is True
    assert decision.accepted_max_tokens == 10_000
    assert decision.reason == "clamped_to_model_max_output"


def test_anthropic_budget_exceeding_profile_max_output_fails() -> None:
    decision = decide_output_budget(
        provider=ANTHROPIC_PROVIDER_ID,
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
        role="conversation_main",
        requested_max_output_tokens=999_999,
    )

    assert decision.should_fail is True
    assert decision.clamped is False
    assert decision.accepted_max_tokens is None
    assert decision.reason == "exceeds_model_max_output"


def test_context_fit_uses_selected_model_profile_window() -> None:
    decision = evaluate_context_fit(
        provider=ANTHROPIC_PROVIDER_ID,
        model="claude-haiku-4-5-20251001",
        context_estimate_tokens=199_500,
        requested_output_tokens=1_000,
    )

    assert decision.context_window_tokens == 200_000
    assert decision.fits is False
    assert decision.overflow_tokens == 500
    assert decision.recommended_action == "compress"
