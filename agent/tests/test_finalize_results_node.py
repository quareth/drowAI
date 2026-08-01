"""Tests for the unified finalizer graph node."""

from typing import Any, List

import pytest

from agent.graph.nodes import finalize as finalize_module  # noqa: E402
from agent.graph.state import FactsState, InteractiveState, TraceState  # noqa: E402
from agent.graph.utils import iteration_memory as _iteration_memory  # noqa: E402
from agent.providers.llm.core.base import LLMResponse
from agent.providers.llm.core.exceptions import LLMConfigurationError, LLMResponseError
from backend.services.usage_tracking.models import UsageData
from core.llm.structured_schemas import FINAL_ANSWER_STRUCTURED_OUTPUT


class DummyWriter:
    def __init__(self) -> None:
        self.events: List[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


class DummyClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def chat_messages_with_usage(self, messages, **kwargs):
        return LLMResponse(
            content='{"provider_internal":"ignored"}',
            structured_output={
                "action": "Scanned the target for PostgreSQL exposure.",
                "findings": "- PostgreSQL was detected on port 5432.",
                "impact": "The service is reachable from the tested network path.",
                "recommended_next_action": "Restrict PostgreSQL access to trusted hosts.",
            },
            usage=UsageData(
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
                model="gpt-5.2",
                provider="openai",
                api_surface="responses",
            ),
        )


class CapturingClient(DummyClient):
    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(api_key=api_key, model=model)
        self.calls: List[List[dict[str, Any]]] = []
        self.call_kwargs: List[dict[str, Any]] = []

    async def chat_messages_with_usage(self, messages, **kwargs):
        self.calls.append(messages)
        self.call_kwargs.append(kwargs)
        return await super().chat_messages_with_usage(messages, **kwargs)


class PlainStreamingOnlyClient:
    async def stream_chat_messages(self, messages, **kwargs):
        yield "plain stream should not be used"


class MissingUsageClient(DummyClient):
    async def chat_messages_with_usage(self, messages, **kwargs):
        response = await super().chat_messages_with_usage(messages, **kwargs)
        response.usage = None
        return response


class FailingStructuredClient(DummyClient):
    async def chat_messages_with_usage(self, messages, **kwargs):
        raise RuntimeError("structured call failed")


class LeakyRawContentClient(DummyClient):
    async def chat_messages_with_usage(self, messages, **kwargs):
        response = await super().chat_messages_with_usage(messages, **kwargs)
        response.content = (
            '{"tool":"filesystem.find_paths","parameters":{"path":"/workspace"}}'
        )
        return response


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
    assert "Recommended Next Action" in final_text

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
            "sections": [
                {
                    "heading": "Tool Output Summary",
                    "body": "Homepage discovered linked routes.",
                },
                {
                    "heading": "Key Findings",
                    "body": "HTTP 200 from http://10.0.0.5/.",
                },
            ],
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
    assert client.call_kwargs[-1]["structured_output"] is FINAL_ANSWER_STRUCTURED_OUTPUT
    user_prompt = client.calls[-1][1]["content"]
    assert "## Prior Current-Turn Phase Memory" in user_prompt
    assert "## Effective Goal" in user_prompt
    assert "## PTR Analyst Observation" in user_prompt
    assert "## Active Decision (advisory)" in user_prompt
    assert "### Key Findings (analyst-derived)" in user_prompt


@pytest.mark.asyncio
async def test_finalize_results_keeps_main_prompt_for_subagent_metadata(monkeypatch):
    """Subagent attribution no longer selects a child-only finalizer mode."""
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Scan localhost for PostgreSQL.",
            conversation_id="conv-xyz",
            metadata={
                "producer_type": "subagent",
                "agent_run_id": "pathfinder-run-1",
                "agent_kind": "recon",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "5432/tcp is closed.",
                    "key_findings": ["5432/tcp closed"],
                },
            },
        ),
        trace=TraceState(),
    )

    client = CapturingClient("test-key", "gpt-5.2")
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    result = await finalize_module.finalize_results(
        interactive.model_dump(),
        context=None,
        config={"configurable": {"thread_id": "lg-42"}},
    )

    assert client.calls
    system_prompt = client.calls[-1][0]["content"]
    user_prompt = client.calls[-1][1]["content"]
    assert "fill exactly the four structured fields" in system_prompt.lower()
    assert "`recommended_next_action`" in user_prompt
    assert any(
        "(simple_tool_execution)" in entry
        for entry in result["trace"]["reasoning"]
    )


@pytest.mark.asyncio
async def test_finalize_results_closes_subagent_message_section_on_generation_failure(monkeypatch):
    """An opened child final-answer section must close after generation fails."""
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Scan localhost for PostgreSQL.",
            conversation_id="conv-xyz",
            metadata={
                "producer_type": "subagent",
                "agent_run_id": "pathfinder-run-1",
                "agent_kind": "recon",
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "5432/tcp is closed.",
                    "key_findings": ["5432/tcp closed"],
                },
            },
        ),
        trace=TraceState(),
    )
    dummy_writer = DummyWriter()
    monkeypatch.setattr(
        finalize_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: FailingStructuredClient("test-key", "gpt-5.2"),
    )
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: dummy_writer)

    with pytest.raises(RuntimeError, match="structured call failed"):
        await finalize_module.finalize_results(
            interactive.model_dump(),
            context=None,
            config={"configurable": {"thread_id": "lg-42"}},
        )

    step_types = [event.get("step_type") for event in dummy_writer.events]
    assert step_types.count("message_start") == 1
    assert "stream_error" in step_types
    assert "message_section_end" in step_types
    terminal_event = dummy_writer.events[-1]
    assert terminal_event["step_type"] == "message_section_end"
    assert terminal_event["agent_run_id"] == "pathfinder-run-1"


@pytest.mark.asyncio
async def test_finalize_tool_results_requires_usage_aware_structured_client(monkeypatch):
    """Finalizer must fail closed without usage-aware structured generation."""
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: PlainStreamingOnlyClient())
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    with pytest.raises(LLMConfigurationError, match="structured finalization is required"):
        await finalize_module.finalize_results(
            _minimal_state(),
            context=None,
            config={"configurable": {"thread_id": "lg-42"}},
        )


@pytest.mark.asyncio
async def test_finalize_tool_results_rejects_missing_usage(monkeypatch):
    """Finalizer must fail closed when structured generation omits usage."""
    monkeypatch.setattr(finalize_module, "resolve_llm_client", lambda *_args, **_kwargs: MissingUsageClient("test-key", "gpt-5.2"))
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    with pytest.raises(LLMResponseError, match="completed without usage data"):
        await finalize_module.finalize_results(
            _minimal_state(),
            context=None,
            config={"configurable": {"thread_id": "lg-42"}},
        )


@pytest.mark.asyncio
async def test_finalize_results_never_emits_untrusted_provider_content(monkeypatch):
    """Only validated final-answer fields may cross into assistant-visible text."""
    writer = DummyWriter()
    monkeypatch.setattr(
        finalize_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: LeakyRawContentClient("test-key", "gpt-5.2"),
    )
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: writer)

    result = await finalize_module.finalize_results(
        _minimal_state(),
        context=None,
        config={"configurable": {"thread_id": "lg-42"}},
    )

    visible_text = "".join(
        str(event.get("content") or event.get("delta") or event.get("text") or "")
        for event in writer.events
        if event.get("step_type") == "message_delta"
    )
    assert "filesystem.find_paths" not in result["trace"]["final_text"]
    assert "filesystem.find_paths" not in visible_text
    assert result["trace"]["final_text"].startswith("## Action\n")
