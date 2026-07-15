"""Contract tests for context-compression prompt loading and pass selection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.providers.llm.core.exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMResponseError,
)
from core.prompts.registry import PromptRegistry
from core.llm.timeout_runtime import LLMTimeoutError
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
    CompressionPolicy,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.context_service import ContextCompressionService


def _should_run_pass2(*, pass1_tokens: int, target_min_tokens: int, target_max_tokens: int) -> bool:
    """Contract: pass2 runs only when pass1 output is outside target min/max."""
    return not (target_min_tokens <= pass1_tokens <= target_max_tokens)


def test_context_compression_prompts_load_via_registry() -> None:
    registry = PromptRegistry()

    system_pass1 = registry.get_template("context_compression_system_pass1")
    user_pass1 = registry.get_template("context_compression_user_pass1")
    system_pass2 = registry.get_template("context_compression_system_pass2")
    user_pass2 = registry.get_template("context_compression_user_pass2")

    assert "Facts" in system_pass1
    assert "{conversation_history}" in user_pass1
    assert "{target_min_tokens}" in user_pass1
    assert "{target_max_tokens}" in user_pass1
    assert "strict fallback compression pass" in system_pass2
    assert "{previous_pass_output}" in user_pass2
    assert "{previous_pass_tokens}" in user_pass2
    assert "{budget_direction}" in user_pass2


def test_pass2_selected_only_when_pass1_is_out_of_target_band() -> None:
    assert _should_run_pass2(pass1_tokens=1000, target_min_tokens=600, target_max_tokens=900) is True
    assert _should_run_pass2(pass1_tokens=900, target_min_tokens=600, target_max_tokens=900) is False
    assert _should_run_pass2(pass1_tokens=700, target_min_tokens=600, target_max_tokens=900) is False
    assert _should_run_pass2(pass1_tokens=500, target_min_tokens=600, target_max_tokens=900) is True


@pytest.mark.parametrize(
    "transient_error",
    [
        LLMAPIError("temporary upstream", status_code=503),
        LLMAPIError("rate limited", status_code=429),
        LLMTimeoutError(
            task_id=1,
            component="CONTEXT_COMPRESSOR",
            operation="test",
            timeout_sec=1.0,
            outcome="timeout",
        ),
    ],
)
def test_transient_provider_failure_retries_once(transient_error: Exception) -> None:
    """A transient provider failure receives exactly two total attempts."""
    calls = 0

    async def _compressor(*_args: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise transient_error
        return "x" * 25

    service = ContextCompressionService(
        compressor=_compressor,
        context_window_manager_factory=lambda _max_tokens: _FakeManager(),
    )

    outcome = _run(service.compress(_request()))

    assert outcome.pass_count == 1
    assert calls == 2


@pytest.mark.parametrize(
    "terminal_error",
    [
        LLMConfigurationError("bad configuration"),
        LLMResponseError("invalid output"),
        LLMAPIError("unauthorized", status_code=401),
    ],
)
def test_non_transient_provider_failure_is_not_retried(
    terminal_error: Exception,
) -> None:
    """Configuration, invalid-output, and authorization-shaped failures stop once."""
    calls = 0

    async def _compressor(*_args: Any) -> str:
        nonlocal calls
        calls += 1
        raise terminal_error

    service = ContextCompressionService(
        compressor=_compressor,
        context_window_manager_factory=lambda _max_tokens: _FakeManager(),
    )

    with pytest.raises(type(terminal_error)):
        _run(service.compress(_request()))

    assert calls == 1


def test_dynamic_threshold_math_from_max_tokens() -> None:
    thresholds = ContextCompressionService.compute_thresholds(
        max_tokens=128_000,
        policy=CompressionPolicy(trigger_percent=100, target_min_percent=20, target_max_percent=30),
    )
    assert thresholds.trigger_tokens == 128_000
    assert thresholds.target_min_tokens == 25_600
    assert thresholds.target_max_tokens == 38_400


def test_pass1_prompt_receives_numeric_budget_targets() -> None:
    captured: dict[str, str] = {}

    async def _capturing_compressor(system_prompt: str, user_prompt: str, call_settings: Any) -> str:  # noqa: ARG001
        captured["user_prompt"] = user_prompt
        return "x" * 25

    service = ContextCompressionService(
        compressor=_capturing_compressor,
        context_window_manager_factory=lambda max_tokens: _FakeManager(),  # noqa: ARG005
    )
    _run(service.compress(_request()))

    assert "Minimum tokens: 20" in captured["user_prompt"]
    assert "Maximum tokens: 30" in captured["user_prompt"]


def test_pass2_prompt_receives_direction_and_previous_tokens() -> None:
    prompts: list[str] = []
    settings: list[Any] = []

    async def _capturing_compressor(system_prompt: str, user_prompt: str, call_settings: Any) -> str:  # noqa: ARG001
        prompts.append(user_prompt)
        settings.append(call_settings)
        if len(prompts) == 1:
            return "x" * 50
        return "x" * 25

    service = ContextCompressionService(
        compressor=_capturing_compressor,
        context_window_manager_factory=lambda max_tokens: _FakeManager(),  # noqa: ARG005
    )
    _run(service.compress(_request()))

    assert len(prompts) == 2
    assert "Correction direction: shorter" in prompts[1]
    assert "Previous pass token estimate: 50" in prompts[1]
    assert "Required target min tokens: 20" in prompts[1]
    assert "Required target max tokens: 30" in prompts[1]
    assert [(item.provider, item.model, item.source) for item in settings] == [
        ("openai", "gpt-4o-mini", "user_selected"),
        ("openai", "gpt-4o-mini", "user_selected"),
    ]


class _FakeManager:
    def __init__(self) -> None:
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
        text = str(history[0].get("content", ""))
        return len(text)

    def estimate_tokens_from_openai_history(self, **kwargs: Any) -> int:
        return self.estimate_tokens_from_history(provider="openai", **kwargs)


async def _fake_compressor(system_prompt: str, user_prompt: str, call_settings: Any) -> str:
    if "strict fallback compression pass" in system_prompt:
        return "x" * 25
    return "x" * 50


async def _always_long_compressor(system_prompt: str, user_prompt: str, call_settings: Any) -> str:  # noqa: ARG001
    return "x" * 80


async def _always_short_compressor(system_prompt: str, user_prompt: str, call_settings: Any) -> str:  # noqa: ARG001
    return "x" * 10


def _request() -> ContextCompressionRequest:
    return ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=100,
        model="gpt-4o-mini",
        conversation_history=[{"role": "user", "content": "hello"}],
        projected_user_message="summarize this",
        policy=CompressionPolicy(trigger_percent=100, target_min_percent=20, target_max_percent=30),
    )


def test_pass1_success_path_and_token_estimation_used() -> None:
    fake_manager = _FakeManager()
    request = _request()
    service = ContextCompressionService(
        compressor=lambda s, u, m: _async_return("x" * 25),
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )
    with patch("backend.services.langgraph_chat.compression.context_service.safe_inc") as mock_inc, patch(
        "backend.services.langgraph_chat.compression.context_service.safe_gauge"
    ) as mock_gauge:
        outcome = _run(service.compress(request))

    assert outcome.pass_count == 1
    assert outcome.pass_results[0].pass_name == "pass1"
    assert outcome.pass_results[0].within_target is True
    # Original context, rendered request hard-fit, then pass output verification.
    assert len(fake_manager.calls) == 3
    assert fake_manager.calls[1]["history"][0]["role"] == "system"
    assert fake_manager.calls[2]["history"][0]["role"] == "assistant"
    assert fake_manager.calls[1]["provider"] == request.provider
    assert fake_manager.calls[1]["model"] == request.model
    assert fake_manager.calls[2]["provider"] == request.provider
    assert fake_manager.calls[2]["model"] == request.model
    metric_names = [call.args[0] for call in mock_inc.call_args_list]
    assert "compression_pass1_success_total" in metric_names
    mock_gauge.assert_called_once()
    assert mock_gauge.call_args.args[0] == "compression_ratio_before_after"


def test_pass2_fallback_when_pass1_misses_target() -> None:
    fake_manager = _FakeManager()
    service = ContextCompressionService(
        compressor=_fake_compressor,
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )
    with patch("backend.services.langgraph_chat.compression.context_service.safe_inc") as mock_inc, patch(
        "backend.services.langgraph_chat.compression.context_service.safe_gauge"
    ) as mock_gauge:
        outcome = _run(service.compress(_request()))

    assert outcome.pass_count == 2
    assert [item.pass_name for item in outcome.pass_results] == ["pass1", "pass2"]
    assert outcome.fallback_reason == "pass1_above_target"
    assert outcome.pass_results[0].within_target is False
    assert outcome.pass_results[1].within_target is True
    metric_names = [call.args[0] for call in mock_inc.call_args_list]
    assert "compression_pass2_used_total" in metric_names
    assert "compression_degraded_total" not in metric_names
    mock_gauge.assert_called_once()


def test_degraded_fallback_flagged_when_pass2_still_misses_target() -> None:
    fake_manager = _FakeManager()
    service = ContextCompressionService(
        compressor=_always_long_compressor,
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )
    with patch("backend.services.langgraph_chat.compression.context_service.safe_inc") as mock_inc, patch(
        "backend.services.langgraph_chat.compression.context_service.safe_gauge"
    ) as mock_gauge:
        outcome = _run(service.compress(_request()))

    assert outcome.degraded is True
    assert outcome.fallback_reason == "pass2_above_target_degraded"
    assert outcome.pass_count == 3
    assert [item.pass_name for item in outcome.pass_results] == ["pass1", "pass2", "degraded"]
    assert outcome.pass_results[-1].within_target is True
    thresholds = ContextCompressionService.compute_thresholds(max_tokens=100, policy=_request().policy)
    assert outcome.final_tokens >= thresholds.target_min_tokens
    assert outcome.final_tokens <= outcome.pass_results[-1].target_max_tokens
    metric_names = [call.args[0] for call in mock_inc.call_args_list]
    assert "compression_pass2_used_total" in metric_names
    assert "compression_degraded_total" in metric_names
    mock_gauge.assert_called_once()


def test_under_min_passes_are_corrected_into_target_band() -> None:
    fake_manager = _FakeManager()
    request = _request()
    service = ContextCompressionService(
        compressor=_always_short_compressor,
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )
    with patch("backend.services.langgraph_chat.compression.context_service.safe_inc"), patch(
        "backend.services.langgraph_chat.compression.context_service.safe_gauge"
    ):
        outcome = _run(service.compress(request))

    thresholds = ContextCompressionService.compute_thresholds(max_tokens=request.max_tokens, policy=request.policy)
    assert outcome.pass_count == 3
    assert outcome.fallback_reason == "pass2_below_target_degraded"
    assert outcome.final_tokens >= thresholds.target_min_tokens
    assert outcome.final_tokens <= thresholds.target_max_tokens


def test_context_compression_rollout_flag_gates_service(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.context_service.ENABLE_CONTEXT_COMPRESSION",
        False,
    )
    assert ContextCompressionService.is_enabled() is False

    monkeypatch.setattr(
        "backend.services.langgraph_chat.compression.context_service.ENABLE_CONTEXT_COMPRESSION",
        True,
    )
    assert ContextCompressionService.is_enabled() is True


def test_default_compressor_requires_runtime_resolver() -> None:
    service = ContextCompressionService()

    with pytest.raises(ValueError, match="runtime_services.client_resolver"):
        _run(
            service._invoke_compressor(
                system_prompt="system",
                user_prompt="user",
                call_settings=type(
                    "_Settings",
                    (),
                    {
                        "model": "gpt-5-mini",
                        "provider": "openai",
                        "reasoning_effort": "minimal",
                        "source": "internal_fixed",
                    },
                )(),
            )
        )


def test_default_compressor_uses_task_selected_target_for_non_openai_request() -> None:
    fake_client = type(
        "_FakeClient",
        (),
        {"chat": AsyncMock(return_value="x" * 25)},
    )()
    calls: list[dict[str, Any]] = []

    class _Resolver:
        def get_client(self, selection: Any, **kwargs: Any) -> Any:
            calls.append({"selection": selection, **kwargs})
            return fake_client

    fake_manager = _FakeManager()
    service = ContextCompressionService(
        context_window_manager_factory=lambda max_tokens: fake_manager,  # noqa: ARG005
    )
    request = ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=100,
        model="claude-sonnet-4-6",
        provider="anthropic",
        conversation_history=[{"role": "user", "content": "hello"}],
        projected_user_message="summarize this",
        policy=CompressionPolicy(trigger_percent=100, target_min_percent=20, target_max_percent=30),
    )
    runtime_selection = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "credential_ref": {"user_id": 7, "provider": "anthropic"},
        "reasoning_effort": "medium",
    }

    _run(
        service.compress(
            request,
            runtime_selection=runtime_selection,
            runtime_services=type("_Services", (), {"client_resolver": _Resolver()})(),
            runtime_user_id=7,
        )
    )

    assert fake_manager.calls[0]["provider"] == "anthropic"
    assert fake_manager.calls[1]["provider"] == "anthropic"
    assert calls[0]["selection"] == runtime_selection
    assert calls[0]["target"].model == "claude-sonnet-4-6"
    assert calls[0]["target"].provider == "anthropic"
    assert calls[0]["target"].source == "user_selected"


def test_compressor_uses_runtime_resolver_with_context_compressor_target() -> None:
    fake_client = type(
        "_FakeClient",
        (),
        {"chat": AsyncMock(return_value="x" * 25)},
    )()
    calls: list[dict[str, Any]] = []

    class _Resolver:
        def get_client(self, selection: Any, **kwargs: Any) -> Any:
            calls.append({"selection": selection, **kwargs})
            return fake_client

    service = ContextCompressionService(
        context_window_manager_factory=lambda max_tokens: _FakeManager(),  # noqa: ARG005
    )
    request = ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=100,
        model="gpt-5.2",
        provider="openai",
        credential_ref={"user_id": 7, "provider": "openai"},
        conversation_history=[{"role": "user", "content": "hello"}],
        projected_user_message="summarize this",
        policy=CompressionPolicy(trigger_percent=100, target_min_percent=20, target_max_percent=30),
    )
    runtime_selection = {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "medium",
    }

    outcome = _run(
        service.compress(
            request,
            runtime_selection=runtime_selection,
            runtime_services=type("_Services", (), {"client_resolver": _Resolver()})(),
            runtime_user_id=7,
        )
    )

    assert outcome.pass_count == 1
    assert calls[0]["selection"] == runtime_selection
    assert calls[0]["runtime_user_id"] == 7
    assert calls[0]["purpose"] == "context_compression"
    assert calls[0]["target"].model == "gpt-5.2"
    assert calls[0]["target"].provider == "openai"
    assert calls[0]["target"].source == "user_selected"
    assert fake_client.chat.await_args.kwargs["max_tokens"] == 30


def test_oversized_compressor_request_fails_before_provider_send() -> None:
    class _OversizedPromptManager(_FakeManager):
        def estimate_tokens_from_history(self, **kwargs: Any) -> int:
            history = kwargs["history"]
            if history and history[0].get("role") == "system":
                return 1_000_000
            return super().estimate_tokens_from_history(**kwargs)

    resolver_calls: list[dict[str, Any]] = []

    class _Resolver:
        def get_client(self, selection: Any, **kwargs: Any) -> Any:
            resolver_calls.append({"selection": selection, **kwargs})
            raise AssertionError("oversized request must fail before client resolution")

    service = ContextCompressionService(
        context_window_manager_factory=lambda max_tokens: _OversizedPromptManager(),  # noqa: ARG005
    )
    runtime_selection = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "medium",
    }

    with pytest.raises(
        CompressionRequiredError,
        match="compressor_request_exceeds_context",
    ):
        _run(
            service.compress(
                _request(),
                runtime_selection=runtime_selection,
                runtime_services=type(
                    "_Services",
                    (),
                    {"client_resolver": _Resolver()},
                )(),
                runtime_user_id=7,
            )
        )

    assert resolver_calls == []


async def _async_return(value: str) -> str:
    return value


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
