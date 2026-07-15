"""Unit tests for ContextWindowManager provider-aware history token estimation."""

import pytest

from agent.context.token_utils import count_tokens_json
from backend.services.langgraph_chat.compression.window_manager import (
    DEFAULT_CONTEXT_WINDOW_MAX_TOKENS,
    MINIMUM_COMPACTION_RETAINED_TURNS,
    TARGET_COMPACTION_RETAINED_TURNS,
    ContextWindowManager,
    resolve_classifier_prompt_budget,
    resolve_context_window_max_tokens,
)


def test_default_context_window_ceiling_remains_128000() -> None:
    """Default construction preserves the global context ceiling constant."""
    manager = ContextWindowManager()

    assert resolve_context_window_max_tokens() == DEFAULT_CONTEXT_WINDOW_MAX_TOKENS
    assert manager.max_tokens == 128_000


def test_compaction_retained_tail_policy_is_five_with_minimum_three() -> None:
    """The compression policy authority owns the complete-turn tail bounds."""
    assert TARGET_COMPACTION_RETAINED_TURNS == 5
    assert MINIMUM_COMPACTION_RETAINED_TURNS == 3


def test_profile_context_window_limit_used_when_no_explicit_ceiling(monkeypatch) -> None:
    """Provider/model profile limit is used when no explicit ceiling is set."""
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.window_manager.resolve_context_window_tokens",
        lambda _ref: 32_000,
    )

    assert (
        resolve_context_window_max_tokens(provider="openai", model="gpt-5-mini")
        == 32_000
    )


def test_evaluate_history_uses_profile_limit_when_manager_has_no_explicit_ceiling(monkeypatch) -> None:
    """Direct provider-aware evaluation resolves the selected model ceiling."""
    observed = {}

    def _fake_resolve_context_window_tokens(ref):
        observed["provider"] = ref.provider
        observed["model"] = ref.model
        return 4_096

    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.window_manager.resolve_context_window_tokens",
        _fake_resolve_context_window_tokens,
    )

    manager = ContextWindowManager()
    decision = manager.evaluate_history(
        task_id=42,
        conversation_id="conv-anthropic",
        history=[{"role": "user", "content": "hello from claude"}],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    assert manager.max_tokens == DEFAULT_CONTEXT_WINDOW_MAX_TOKENS
    assert decision.snapshot.max_tokens == 4_096
    assert observed == {"provider": "anthropic", "model": "claude-sonnet-4-6"}


def test_explicit_context_window_limit_precedes_profile(monkeypatch) -> None:
    """An explicitly supplied max_tokens value remains authoritative."""
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.window_manager.resolve_context_window_tokens",
        lambda _ref: 32_000,
    )

    assert (
        resolve_context_window_max_tokens(
            explicit_max_tokens=12_000,
            provider="openai",
            model="gpt-5-mini",
        )
        == 12_000
    )


def test_estimate_tokens_from_openai_history_uses_existing_token_counter() -> None:
    """History token estimation should match the shared token utility totals."""
    manager = ContextWindowManager(max_tokens=128_000)
    history = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize this file."},
        {"role": "assistant", "content": "Sure, share it with me."},
    ]
    expected = sum(count_tokens_json(message, model="gpt-4o-mini") for message in history)

    observed = manager.estimate_tokens_from_openai_history(
        history=history,
        model="gpt-4o-mini",
    )

    assert observed == expected


def test_openai_known_model_uses_tiktoken_strategy() -> None:
    """Provider-aware estimates keep tiktoken for known OpenAI models."""
    manager = ContextWindowManager(max_tokens=128_000)

    estimate = manager.estimate_history_tokens(
        history=[{"role": "user", "content": "hello"}],
        provider="openai",
        model="gpt-4",
    )

    assert estimate.provider == "openai"
    assert estimate.model == "gpt-4"
    assert estimate.strategy == "tiktoken_model"
    assert estimate.precision == "exact"
    assert estimate.safety_margin_applied == 0.0


def test_anthropic_model_uses_heuristic_not_openai_tokenizer() -> None:
    """Anthropic estimates are explicitly heuristic and never OpenAI-tokenized."""
    manager = ContextWindowManager(max_tokens=128_000)

    estimate = manager.estimate_history_tokens(
        history=[{"role": "user", "content": "hello from claude"}],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    assert estimate.provider == "anthropic"
    assert estimate.model == "claude-sonnet-4-6"
    assert estimate.strategy == "anthropic_char_heuristic"
    assert estimate.precision == "heuristic"
    assert estimate.safety_margin_applied == 0.15
    assert estimate.tokens > 0


def test_unknown_provider_model_uses_explicit_heuristic() -> None:
    """Unknown providers use a named heuristic instead of an OpenAI fallback."""
    manager = ContextWindowManager(max_tokens=128_000)

    estimate = manager.estimate_history_tokens(
        history=[{"role": "user", "content": "hello"}],
        provider="unknown-provider",
        model="unknown-model",
    )

    assert estimate.provider == "unknown-provider"
    assert estimate.model == "unknown-model"
    assert estimate.strategy == "provider_agnostic_char_heuristic"
    assert estimate.precision == "heuristic"
    assert estimate.safety_margin_applied == 0.25


def test_evaluate_openai_history_includes_projected_user_message() -> None:
    """Projected user message should be included in used_tokens computation."""
    manager = ContextWindowManager(max_tokens=128_000)
    history = [{"role": "assistant", "content": "How can I help?"}]
    projection = "Please explain context-window policy."
    expected = count_tokens_json(history[0], model="gpt-4o-mini") + count_tokens_json(
        {"role": "user", "content": projection},
        model="gpt-4o-mini",
    )

    decision = manager.evaluate_openai_history(
        task_id=42,
        conversation_id="conv-1",
        history=history,
        model="gpt-4o-mini",
        projected_user_message=projection,
    )

    assert decision.snapshot.used_tokens == expected
    assert decision.snapshot.max_tokens == 128_000
    assert decision.ceiling_reached is False
    assert decision.recommended_next_action == "none"
    assert decision.compression_candidate is False


def test_evaluate_history_accepts_selected_provider_and_model() -> None:
    """Provider-aware evaluation can make decisions for non-OpenAI selections."""
    manager = ContextWindowManager(max_tokens=1)

    decision = manager.evaluate_history(
        task_id=42,
        conversation_id="conv-anthropic",
        history=[{"role": "user", "content": "hello from claude"}],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    assert decision.snapshot.used_tokens >= 1
    assert decision.ceiling_reached is True
    assert decision.snapshot.max_tokens == 1


def test_evaluate_openai_history_sets_ceiling_reached_at_or_over_limit() -> None:
    """Decision should mark ceiling_reached when used tokens meet configured max."""
    manager = ContextWindowManager(max_tokens=1)

    decision = manager.evaluate_openai_history(
        task_id=7,
        conversation_id="conv-7",
        history=[{"role": "user", "content": "hello"}],
        model="gpt-4o-mini",
    )

    assert decision.snapshot.used_tokens >= 1
    assert decision.ceiling_reached is True
    assert decision.recommended_next_action == "compress"
    assert decision.compression_candidate is True


def test_classifier_prompt_budget_reserves_output_once_before_soft_trigger() -> None:
    """Default trigger is 80% of context after one output reservation."""
    budget = resolve_classifier_prompt_budget(
        context_limit_tokens=128_000,
        reserved_output_tokens=32_000,
        env_getter=lambda _name: None,
    )

    assert budget.context_limit_tokens == 128_000
    assert budget.reserved_output_tokens == 32_000
    assert budget.usable_prompt_tokens == 96_000
    assert budget.trigger_tokens == 76_800
    assert budget.override_active is False


def test_manual_override_changes_only_classifier_soft_trigger() -> None:
    """Manual override lowers the decision trigger but not the hard limit."""
    manager = ContextWindowManager(max_tokens=128_000)

    decision, budget = manager.evaluate_classifier_prompt(
        task_id=7,
        conversation_id="conv-7",
        prompt_tokens=4_096,
        reserved_output_tokens=32_000,
        env_getter=lambda _name: "4096",
    )

    assert budget.trigger_tokens == 4_096
    assert budget.override_active is True
    assert budget.usable_prompt_tokens == 96_000
    assert decision.ceiling_reached is True
    assert decision.snapshot.max_tokens == 128_000
    assert decision.snapshot.ceiling_reached is False
    assert decision.snapshot.remaining_tokens == 123_904


@pytest.mark.parametrize("override", ["0", "-1", "96000", "invalid"])
def test_invalid_manual_override_is_inactive(override: str) -> None:
    """Only a positive override below the actual usable budget is accepted."""
    budget = resolve_classifier_prompt_budget(
        context_limit_tokens=128_000,
        reserved_output_tokens=32_000,
        env_getter=lambda _name: override,
    )

    assert budget.trigger_tokens == 76_800
    assert budget.override_active is False
