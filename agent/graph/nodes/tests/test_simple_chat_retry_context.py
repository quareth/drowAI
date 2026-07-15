"""Phase 2.4 retry-context consumer pin for the simple_chat node.

The retry route surfaces sanitized retry context (canonical retry
identity + ``previous_failure``) onto the LangGraph run config under
``configurable``. Graph nodes consume that via
``read_retry_context(config)``. This test pins that the simple_chat
prompt assembly actually reads it and emits a guidance message that
discourages blindly repeating the failing action.

Sanitization invariant: only the whitelisted previous-failure fields
reach the prompt. Raw payloads / secret-bearing keys never appear.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.graph.context.builder import build_conversation_context_bundle
from agent.graph.nodes.simple_chat import (
    _build_retry_guidance_message,
    _build_simple_chat_messages,
    run_simple_chat,
)
from agent.graph.utils.retry_context import read_retry_context
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome


def _system_messages(messages):
    return [m for m in messages if m.get("role") == "system"]


def test_simple_chat_emits_no_retry_guidance_when_config_has_no_retry_context() -> None:
    """A normal turn (no retry identity on configurable) emits no guidance."""
    config = {"configurable": {"thread_id": "task-1", "graph_name": "simple_chat"}}
    retry_context = read_retry_context(config)
    assert retry_context.is_retry is False

    guidance = _build_retry_guidance_message(retry_context)
    assert guidance is None

    messages = _build_simple_chat_messages(
        history=[],
        current_user_turn={"role": "user", "content": "hello"},
        retry_guidance=guidance,
    )
    # Only the default system prompt — no retry guidance message.
    system_msgs = _system_messages(messages)
    assert len(system_msgs) == 1
    assert "Previous attempt failed" not in system_msgs[0]["content"]


def test_simple_chat_emits_sanitized_retry_guidance_when_config_carries_retry_context() -> None:
    """A retry turn surfaces a sanitized one-line guidance message."""
    config = {
        "configurable": {
            "thread_id": "task-1",
            "graph_name": "simple_chat",
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "previous_failure": {
                "error_code": "tool_argument_invalid",
                "failure_stage": "graph_continuation",
                "tool_name": "http_get",
                "tool_call_id": "call-77",
                "summary": "tool rejected malformed url argument",
                # Secret-bearing keys must never appear in the rendered guidance.
                "raw_response": "<html>secret provider response</html>",
                "auth_token": "Bearer sk-LEAK-ME",
            },
        }
    }
    retry_context = read_retry_context(config)
    assert retry_context.is_retry is True

    guidance = _build_retry_guidance_message(retry_context)
    assert isinstance(guidance, str) and guidance.strip()

    messages = _build_simple_chat_messages(
        history=[],
        current_user_turn={"role": "user", "content": "fetch the url"},
        retry_guidance=guidance,
    )
    system_msgs = _system_messages(messages)
    # Default system prompt plus retry guidance.
    assert len(system_msgs) == 2
    rendered = system_msgs[1]["content"]
    assert "Previous attempt failed" in rendered
    assert "tool_argument_invalid" in rendered
    assert "graph_continuation" in rendered
    assert "http_get" in rendered
    assert "tool rejected malformed url argument" in rendered
    # Sanitization: no secret-bearing fields leak into the prompt.
    for forbidden in (
        "<html>",
        "secret provider response",
        "Bearer sk-LEAK-ME",
    ):
        assert forbidden not in rendered

    # The guidance must change the prompt vs. a non-retry run so a downstream
    # consumer cannot accidentally treat retry and fresh-turn paths as equal.
    fresh_messages = _build_simple_chat_messages(
        history=[],
        current_user_turn={"role": "user", "content": "fetch the url"},
        retry_guidance=None,
    )
    assert len(_system_messages(fresh_messages)) < len(system_msgs)


def test_simple_chat_retry_guidance_handles_partial_previous_failure_safely() -> None:
    """A retry config with only retry_attempt (no failure dict) still emits guidance."""
    config = {
        "configurable": {
            "retry_attempt": 1,
            "retry_max_attempts": 2,
        }
    }
    retry_context = read_retry_context(config)
    assert retry_context.is_retry is True

    guidance = _build_retry_guidance_message(retry_context)
    assert isinstance(guidance, str)
    assert "Previous attempt failed" in guidance
    # No tool/error_code -> falls back to a generic descriptor without crashing.
    assert "Choose a corrected or alternate path" in guidance


@pytest.mark.asyncio
async def test_simple_chat_does_not_fallback_after_provider_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usage-aware refusal must reach terminal dispatch without a second call."""

    class RefusingClient:
        def __init__(self) -> None:
            self.fallback_calls = 0

        async def chat_messages_with_usage(self, *_args, **_kwargs):
            raise LLMRefusalError(
                "declined",
                outcome=LLMRefusalOutcome(
                    provider="anthropic",
                    model="claude-fable-5",
                ),
            )

        async def chat_messages(self, *_args, **_kwargs):
            self.fallback_calls += 1
            return "fallback"

    client = RefusingClient()
    monkeypatch.setattr(
        "agent.graph.nodes.simple_chat.resolve_llm_client",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.simple_chat.resolve_llm_call_settings",
        lambda *_args, **_kwargs: SimpleNamespace(reasoning_effort=None),
    )
    state = {
        "facts": {
            "task_id": 1,
            "message": "request",
            "metadata": {
                "context_bundle": build_conversation_context_bundle(
                    conversation_id="conv-refusal",
                    turn_id="turn-refusal",
                    turn_sequence=1,
                    messages=[],
                    current_message="request",
                )
            },
        },
        "trace": {},
    }

    with pytest.raises(LLMRefusalError):
        await run_simple_chat(state)

    assert client.fallback_calls == 0
