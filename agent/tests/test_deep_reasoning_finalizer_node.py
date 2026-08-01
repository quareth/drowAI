"""Tests for the deep-reasoning finalizer graph node."""

import pytest

from agent.graph.nodes import finalize as finalize_module
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.providers.llm.core.base import LLMResponse
from backend.services.usage_tracking.models import UsageData


class DummyWriter:
    def __init__(self) -> None:
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class DummyClient:
    async def chat_messages_with_usage(self, messages, **kwargs):
        return LLMResponse(
            content='{"internal":"ignored"}',
            structured_output={
                "action": "Completed deep service enumeration on 127.0.0.1.",
                "findings": "- PostgreSQL was detected on port 5432.",
                "impact": "The PostgreSQL service is reachable from the tested path.",
                "recommended_next_action": "Restrict network access and enumerate PostgreSQL.",
            },
            usage=UsageData(
                prompt_tokens=10,
                completion_tokens=3,
                total_tokens=13,
                model="gpt-5.2",
                provider="openai",
                api_surface="responses",
            ),
        )


@pytest.mark.asyncio
async def test_finalize_deep_reasoning_streams_final_answer(monkeypatch):
    """DR finalizer should stream the final response via message events."""

    interactive = InteractiveState(
        facts=FactsState(
            task_id=101,
            message="Perform a deep scan of 127.0.0.1",
            conversation_id="conv-dr-1",
            capability="deep_reasoning",
            plan=[
                "Enumerate reachable hosts",
                "Scan open ports on the active host",
                "Summarize vulnerabilities",
            ],
            todo_list=["Scan hosts", "Enumerate services"],
            metadata={
                "api_key": "test-key",
                "model": "gpt-5.2",
                "dr_iteration_meta": {"active_iteration": 1},
                "dr_iteration_records": {
                    "1": {
                        "reasoning": ["Host 127.0.0.1 responded to ping."],
                        "tool": {
                            "tool": "information_gathering.network_discovery.nmap",
                            "status": "success",
                            "command": "nmap -sV 127.0.0.1",
                            "summary": "Port 5432 open (postgresql)",
                        },
                        "observation": "PostgreSQL 9.6 detected.",
                    }
                },
            },
        ),
        trace=TraceState(observations=["PostgreSQL service reachable on 5432"]),
    )

    def fake_resolve_llm_client(metadata, context, **kwargs):
        return DummyClient()

    dummy_writer = DummyWriter()
    monkeypatch.setattr(finalize_module, "resolve_llm_client", fake_resolve_llm_client)
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: dummy_writer)

    result = await finalize_module.finalize_results(
        interactive.model_dump(),
        context=None,
        config={"configurable": {"thread_id": "lg-101"}},
    )

    final_text = result["trace"]["final_text"]
    assert "PostgreSQL" in final_text
    assert "Recommended Next Action" in final_text

    step_types = [event.get("step_type") for event in dummy_writer.events]
    assert "message_start" in step_types
    assert "message_delta" in step_types
    assert "message_section_end" in step_types


@pytest.mark.asyncio
async def test_finalize_deep_reasoning_fallback_without_client(monkeypatch):
    """Missing API key should fail fast."""

    interactive = InteractiveState(
        facts=FactsState(
            task_id=202,
            message="Summarize findings for localhost",
            conversation_id="conv-dr-2",
            capability="deep_reasoning",
            plan=["Scan host", "Document findings"],
            metadata={
                "dr_iteration_meta": {"active_iteration": 2},
                "dr_iteration_records": {
                    "2": {
                        "reasoning": ["Need to document the findings."],
                    }
                },
            },
        ),
        trace=TraceState(observations=["Host reachable", "No additional ports open"]),
    )

    from agent.providers.llm.core.exceptions import LLMConfigurationError
    def raise_config_error(metadata, context, **kwargs):
        raise LLMConfigurationError("No API key")
    monkeypatch.setattr(finalize_module, "resolve_llm_client", raise_config_error)
    monkeypatch.setattr(finalize_module, "get_stream_writer", lambda: None)

    with pytest.raises(LLMConfigurationError):
        await finalize_module.finalize_results(
            interactive.model_dump(),
            context=None,
            config={"configurable": {"thread_id": "lg-202"}},
        )
