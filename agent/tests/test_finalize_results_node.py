"""Tests for the unified finalizer graph node."""

from typing import Any, List

import pytest

from agent.graph.nodes import finalize as finalize_module  # noqa: E402
from agent.graph.state import FactsState, InteractiveState, TraceState  # noqa: E402
from agent.graph.utils import iteration_memory as _iteration_memory  # noqa: E402
from agent.providers.llm.core.exceptions import LLMConfigurationError, LLMResponseError
from backend.services.usage_tracking.models import UsageData


class DummyWriter:
    def __init__(self) -> None:
        self.events: List[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


class DummyClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def stream_chat_messages(self, messages, **kwargs):
        yield "Findings: host exposes PostgreSQL.\n"
        yield "Recommendation: restrict access.\n"

    async def stream_chat_messages_with_usage(self, messages, **kwargs):
        iterator = self.stream_chat_messages(messages, **kwargs)

        class _StreamWithUsage:
            content_iterator = iterator

            def get_final_usage(self):
                return UsageData(
                    prompt_tokens=10,
                    completion_tokens=2,
                    total_tokens=12,
                    model="gpt-5.2",
                    provider="openai",
                    api_surface="responses",
                )

        return _StreamWithUsage()


class CapturingClient(DummyClient):
    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(api_key=api_key, model=model)
        self.calls: List[List[dict[str, Any]]] = []

    async def stream_chat_messages(self, messages, **kwargs):
        self.calls.append(messages)
        yield "Final answer body.\n"


class PlainStreamingOnlyClient:
    async def stream_chat_messages(self, messages, **kwargs):
        yield "plain stream should not be used"


class MissingUsageClient(DummyClient):
    async def stream_chat_messages_with_usage(self, messages, **kwargs):
        iterator = self.stream_chat_messages(messages, **kwargs)

        class _StreamWithoutUsage:
            content_iterator = iterator

            def get_final_usage(self):
                return None

        return _StreamWithoutUsage()


def _minimal_state() -> dict[str, Any]:
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Scan localhost for open ports.",
            conversation_id="conv-xyz",
            metadata={
                "api_key": "test-key",
                "model": "gpt-5.2",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "PostgreSQL detected on 5432.",
                },
            },
        ),
        trace=TraceState(),
    )
    return interactive.model_dump()


@pytest.mark.asyncio
async def test_finalize_tool_results_streams_final_answer(monkeypatch):
    """The node should stream the final answer via message events."""

    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Scan localhost for open ports.",
            conversation_id="conv-xyz",
            metadata={
                "api_key": "test-key",
                "model": "gpt-5.2",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "PostgreSQL detected on 5432.",
                    "key_findings": ["Port 5432/tcp open (postgresql)"],
                    "next_actions": ["Restrict access to trusted hosts."],
                },
                "last_tool_result": {"stdout_excerpt": "5432/tcp open postgresql"},
            },
        ),
        trace=TraceState(),
    )

    dummy_writer = DummyWriter()
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: DummyClient("test-key", "gpt-5.2"))
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: dummy_writer)

    result = await finalize_module.finalize_results(interactive.model_dump(), context=None, config={"configurable": {"thread_id": "lg-42"}})

    final_text = result["trace"]["final_text"]
    assert "PostgreSQL" in final_text
    assert "Recommendation" in final_text

    # Ensure streaming events were emitted with assistant message phase
    step_types = [event.get("step_type") for event in dummy_writer.events if isinstance(event, dict)]
    assert "message_start" in step_types
    assert "message_delta" in step_types
    assert "message_section_end" in step_types


@pytest.mark.asyncio
async def test_finalize_tool_results_fallback_without_api_key(monkeypatch):
    """Missing API keys should fail fast."""

    interactive = InteractiveState(
        facts=FactsState(
            task_id=7,
            message="Enumerate services.",
            conversation_id="conv-no-key",
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Host reachable.",
                    "key_findings": ["No critical services detected."],
                    "next_actions": ["Schedule authenticated scan."],
                },
                "last_tool_result": {"stdout_excerpt": "Host is up"},
            },
        ),
        trace=TraceState(),
    )

    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    from agent.providers.llm.core.exceptions import LLMConfigurationError

    with pytest.raises(LLMConfigurationError):
        await finalize_module.finalize_results(interactive.model_dump(), context=None, config=None)


@pytest.mark.asyncio
async def test_finalize_tool_results_includes_ptr_context_sections(monkeypatch):
    """Finalizer prompt should include PTR-derived findings and phase memory."""
    metadata: dict[str, Any] = {
        "api_key": "test-key",
        "model": "gpt-5.2",
        "synthesized_output": {
            "tool": "information_gathering.web_enumeration.http_request",
            "summary": "HTTP 200 from dashboard.",
            "observation_text": "Dashboard links suggest additional endpoint exposure.",
            "key_findings": ["HTTP 200 from /"],
            "next_actions": ["Fallback action"],
        },
        "last_tool_result": {
            "parameters": {"target": "10.0.0.5"},
            "stdout_excerpt": "HTTP/1.1 200 OK",
        },
        "working_memory": {
            "referents": {"intent:target": "10.0.0.5"},
            "active_decision": {
                "status": "active",
                "next_action": "call_tool",
                "tool_intent": {
                    "description": "Enumerate linked routes",
                    "target": "10.0.0.5",
                    "focus": "endpoint discovery",
                },
            },
            "available_findings": [
                {
                    "kind": "finding.vulnerability_candidate",
                    "target": "10.0.0.5:80",
                    "subject": "10.0.0.5",
                    "details": {
                        "rationale": "Operational endpoints might be exposed.",
                        "evidence_refs": ["artifact://http-output#/capture"],
                        "vulnerability": "AUTHZ-CANDIDATE-EXPOSED-ENDPOINTS",
                        "vulnerability_confidence": 0.35,
                    },
                    "assertion_level": "candidate",
                    "confidence": 0.35,
                    "seen_at": 1713870000,
                    "ttl_seconds": 300,
                }
            ],
        },
    }
    _iteration_memory.append(
        metadata,
        turn_sequence=12,
        source="tool",
        payload={
            "kind": "http_request",
            "target": "http://10.0.0.5/",
            "action": "GET /",
            "status": "success",
            "result": "positive",
            "summary": "Homepage discovered linked routes.",
            "terminal_for_hypothesis": False,
        },
    )

    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Enumerate endpoints",
            current_goal="Validate endpoint exposure and access controls",
            conversation_id="conv-xyz",
            metadata=metadata,
        ),
        trace=TraceState(),
    )

    client = CapturingClient("test-key", "gpt-5.2")
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    await finalize_module.finalize_results(
        interactive.model_dump(),
        context=None,
        config={"configurable": {"thread_id": "lg-42"}},
    )

    assert client.calls, "expected finalizer LLM call to be captured"
    user_prompt = client.calls[-1][1]["content"]
    assert "## Prior Current-Turn Phase Memory" in user_prompt
    assert "## Effective Goal" in user_prompt
    assert "## PTR Analyst Observation" in user_prompt
    assert "## Active Decision (advisory)" in user_prompt
    assert "### Key Findings (analyst-derived)" in user_prompt


@pytest.mark.asyncio
async def test_finalize_tool_results_rejects_plain_streaming_client(monkeypatch):
    """Finalizer must fail closed instead of falling back to plain streaming."""
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: PlainStreamingOnlyClient())
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    with pytest.raises(LLMConfigurationError, match="Usage-aware streaming is required"):
        await finalize_module.finalize_results(
            _minimal_state(),
            context=None,
            config={"configurable": {"thread_id": "lg-42"}},
        )


@pytest.mark.asyncio
async def test_finalize_tool_results_rejects_missing_stream_usage(monkeypatch):
    """Finalizer must fail closed when usage-aware streaming omits final usage."""
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: MissingUsageClient("test-key", "gpt-5.2"))
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    with pytest.raises(LLMResponseError, match="completed without final usage"):
        await finalize_module.finalize_results(
            _minimal_state(),
            context=None,
            config={"configurable": {"thread_id": "lg-42"}},
        )
