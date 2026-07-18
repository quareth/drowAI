"""Baseline tests for context compression target selection and budget fit checks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.services.langgraph_chat.compression.context_models import (
    CompressionPolicy,
    CompressionRequiredError,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.context_service import (
    ContextCompressionService,
)


def test_context_compression_preserves_selected_provider_model_and_budget_targets() -> None:
    captured_settings: list[Any] = []
    fake_manager = _FakeManager(prompt_token_estimate=100)

    async def _capturing_compressor(
        system_prompt: str,
        user_prompt: str,
        call_settings: Any,
    ) -> str:
        assert "Minimum tokens: 20" in user_prompt
        assert "Maximum tokens: 30" in user_prompt
        captured_settings.append(call_settings)
        return "x" * 25

    service = ContextCompressionService(
        compressor=_capturing_compressor,
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )

    outcome = _run(service.compress(_request(provider="anthropic", model="claude-sonnet-5")))

    assert outcome.pass_count == 1
    assert outcome.final_tokens == 25
    assert [(item.provider, item.model, item.source) for item in captured_settings] == [
        ("anthropic", "claude-sonnet-5", "user_selected")
    ]
    # Original context, rendered request hard-fit, then pass output verification.
    assert [call["provider"] for call in fake_manager.calls] == [
        "anthropic",
        "anthropic",
        "anthropic",
    ]
    assert [call["model"] for call in fake_manager.calls] == [
        "claude-sonnet-5",
        "claude-sonnet-5",
        "claude-sonnet-5",
    ]


def test_context_compression_fails_before_sdk_call_when_rendered_request_cannot_fit() -> None:
    calls = 0

    async def _unexpected_compressor(*_args: Any) -> str:
        nonlocal calls
        calls += 1
        return "x" * 25

    service = ContextCompressionService(
        compressor=_unexpected_compressor,
        context_window_manager_factory=lambda max_tokens: _FakeManager(  # noqa: ARG005
            prompt_token_estimate=1_000_000
        ),
    )

    with pytest.raises(CompressionRequiredError) as exc_info:
        _run(
            service.compress(
                _request(provider="anthropic", model="claude-haiku-4-5-20251001")
            )
        )

    assert calls == 0
    assert exc_info.value.reason == "compressor_request_exceeds_context"
    assert "provider=anthropic" in str(exc_info.value)
    assert "model=claude-haiku-4-5-20251001" in str(exc_info.value)


def test_context_compression_thresholds_remain_percentage_based() -> None:
    thresholds = ContextCompressionService.compute_thresholds(
        max_tokens=128_000,
        policy=CompressionPolicy(
            trigger_percent=100,
            target_min_percent=20,
            target_max_percent=30,
        ),
    )

    assert thresholds.trigger_tokens == 128_000
    assert thresholds.target_min_tokens == 25_600
    assert thresholds.target_max_tokens == 38_400


class _FakeManager:
    def __init__(self, *, prompt_token_estimate: int) -> None:
        self._prompt_token_estimate = prompt_token_estimate
        self.calls: list[dict[str, Any]] = []

    def estimate_tokens_from_history(
        self,
        *,
        history: list[dict[str, Any]],
        provider: str = "openai",
        model: str,
        projected_user_message: str | None = None,
    ) -> int:
        self.calls.append(
            {
                "history": history,
                "provider": provider,
                "model": model,
                "projected_user_message": projected_user_message,
            }
        )
        if projected_user_message is not None:
            return 1000
        if history and history[0].get("role") == "system":
            return self._prompt_token_estimate
        return len(str(history[0].get("content", ""))) if history else 0

    def estimate_tokens_from_openai_history(self, **kwargs: Any) -> int:
        return self.estimate_tokens_from_history(provider="openai", **kwargs)


def _request(*, provider: str = "openai", model: str = "gpt-4o-mini") -> ContextCompressionRequest:
    return ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=100,
        provider=provider,
        model=model,
        conversation_history=[{"role": "user", "content": "hello"}],
        projected_user_message="summarize this",
        policy=CompressionPolicy(
            trigger_percent=100,
            target_min_percent=20,
            target_max_percent=30,
        ),
    )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
