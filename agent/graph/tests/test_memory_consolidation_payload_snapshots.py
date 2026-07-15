"""Phase 0 characterization snapshots for node-to-LLM payload wiring.

These tests lock what key nodes actually pass into LLM seams so memory
carrier refactors can prove no prompt/message behavior drift.
"""

from __future__ import annotations

from types import SimpleNamespace
import json
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes.finalize import finalize_results as finalize_deep_reasoning
from agent.graph.nodes.finalize import finalize_results as finalize_tool_results
from agent.graph.nodes.reflect import reflect_node
from agent.graph.nodes.think_more import think_more_node
from agent.graph.nodes.synthesis import synthesis_node
from agent.graph.nodes.tool_articulation import articulate_tool_intent
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.compression.compressor import compress_tool_output
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.intent.classifier import IntentClassifier
from core.prompts.tests._golden import assert_golden


class _StreamingLLMStub:
    """Simple streaming LLM stub that captures messages payloads."""

    def __init__(self, *, chunks: Optional[List[str]] = None) -> None:
        self.messages_payloads: List[List[Dict[str, str]]] = []
        self.chunks = chunks or ["ok"]

    async def stream_chat_messages(
        self,
        messages: List[Dict[str, str]],
        **_: Any,
    ) -> AsyncIterator[str]:
        self.messages_payloads.append(list(messages))
        for chunk in self.chunks:
            yield chunk


class _UsageLLMStub:
    """Non-streaming chat_with_usage stub that captures prompt payloads."""

    def __init__(
        self,
        *,
        content: str,
        structured_output: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.content = content
        self.structured_output = structured_output
        self.calls: List[Dict[str, Any]] = []

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(
            content=self.content,
            usage=None,
            structured_output=self.structured_output,
        )


def _intent_brief() -> Dict[str, Any]:
    """Return a deterministic brief payload for snapshot fixtures."""
    return {
        "resolved_user_intent": "Enumerate exposed services on 10.0.0.5",
        "overall_goal": "Map externally reachable services",
        "continuation_mode": "new_request",
        "resolved_step_title": "Discovery",
        "resolved_step_detail": "Start with TCP scan",
        "next_operational_goal": "Run nmap service scan",
        "success_condition": "List open ports and service names",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "resolved_target": "10.0.0.5",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": ["No UDP", "Low-noise only"],
        "suggested_category_focus": ["network_recon"],
        "retrieval_hints": ["nmap", "service banners"],
        "relevant_memory_fragments": ["host alive from previous discovery"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
    }


def _install_bundle(metadata: Dict[str, Any], history: List[Dict[str, Any]]) -> None:
    """Install a deterministic context bundle on metadata."""
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=7,
        messages=history,
    )


def _interactive_state_for_nodes() -> InteractiveState:
    """Construct a baseline interactive state used by node snapshot tests."""
    metadata: Dict[str, Any] = {
        "api_key": "test-key",
        "model": "gpt-5.2",
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "working_memory": {
            "intent_brief": {
                key: value
                for key, value in _intent_brief().items()
                if key != "suggested_category_focus"
            },
            "active": {"target_id": "intent:target"},
            "referents": {"intent:target": {"value": "10.0.0.5"}},
            "available_findings": [
                {
                    "kind": "port_open",
                    "target": "10.0.0.5",
                    "subject": "10.0.0.5:443/tcp",
                    "details": {"service": "https"},
                }
            ],
        },
        "synthesized_output": {
            "tool": "nmap.scan",
            "summary": "443/tcp open https",
            "key_findings": ["443/tcp open https"],
            "vulnerabilities": [],
            "next_actions": ["Inspect TLS service"],
        },
        "last_tool_result": {
            "tool": "nmap.scan",
            "parameters": {"target": "10.0.0.5"},
            "stdout_excerpt": "443/tcp open https\n",
            "stderr_excerpt": "",
            "status": "success",
            "success": True,
        },
        "last_tool_result_compact": {
            "summary": "443/tcp open https",
            "key_findings": ["443/tcp open https"],
            "errors": [],
            "structured_signals": [],
            "decision_evidence": ["nmap scan output"],
            "lossiness_risk": "low",
        },
    }
    _install_bundle(
        metadata,
        history=[
            {"role": "user", "content": "scan 10.0.0.5"},
            {"role": "assistant", "content": "Running scan."},
        ],
    )

    facts = FactsState(
        task_id=42,
        message="Enumerate 10.0.0.5 services",
        conversation_id="conv-1",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"nmap.scan": {"target": "10.0.0.5"}},
        plan=["Run nmap -sV 10.0.0.5", "Inspect TLS service"],
        todo_list=["Run nmap", "Inspect TLS service"],
        current_goal="Service enumeration",
        metadata=metadata,
        decision_history=["call_tool: run nmap"],
        iterations=2,
    )
    trace = TraceState(
        observations=["443/tcp open https"],
        reasoning=["Need service-level validation."],
        scratchpad="diagnostic only",
        executed_tools=[
            {
                "tool_id": "nmap.scan",
                "observation": "443/tcp open https",
            }
        ],
    )
    return InteractiveState(facts=facts, trace=trace)


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_intent_classifier_prompt_payload() -> None:
    """Snapshot classifier user-prompt payload assembled by enrich_runtime_config."""
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution", "deep_reasoning"],
        "intent_hints": {"tool_hints": ["nmap"], "targets": ["10.0.0.5"]},
        "risk_flags": ["external_target"],
        "execution_route_policy": {
            "forced_classifier_label": "plan_executor",
            "forced_execution_mode": "deep_reasoning",
        },
    }
    history = [
        {"role": "user", "content": "scan 10.0.0.5"},
        {"role": "assistant", "content": "What depth do you want?"},
    ]
    _install_bundle(metadata, history)
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=7,
        message="Run a complete service scan on 10.0.0.5",
        conversation_id="conv-1",
        history=history,
        api_key="test-key",
        model="gpt-5.2",
    )
    config = LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata=metadata,
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )
    stub = _UsageLLMStub(
        content='{"label":"tool_call","confidence":0.9,"reasoning":"direct action"}',
    )
    classifier = IntentClassifier(client_factory=lambda _settings: stub)
    await classifier.enrich_runtime_config(config)

    captured = stub.calls[0]
    assert_golden(
        "memory_consolidation__intent_classifier_prompt_payload.json",
        json.dumps(captured, indent=2, sort_keys=True, default=str),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_tool_articulation_payload(monkeypatch) -> None:
    """Snapshot tool-articulation node prompt payload assembly."""
    interactive = _interactive_state_for_nodes()
    captured: Dict[str, Any] = {}

    def _capture_prompt(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "captured articulation prompt"

    stub = _UsageLLMStub(content="I will run nmap to enumerate services.")
    monkeypatch.setattr(
        "agent.graph.nodes.tool_articulation.get_stream_writer",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.tool_articulation.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.tool_articulation.build_tool_articulation_prompt",
        _capture_prompt,
    )

    await articulate_tool_intent(interactive.as_graph_state(), context=None, config={})
    assert_golden(
        "memory_consolidation__tool_articulation_node_payload.json",
        json.dumps(captured, indent=2, sort_keys=True, default=str),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_reflect_payload(monkeypatch) -> None:
    """Characterize reflect-node LLM payload contract."""
    interactive = _interactive_state_for_nodes()
    interactive.facts.stuck_counter = 3
    interactive.facts.decision_history = [
        "call_tool: run nmap",
        "call_tool: run nmap",
        "call_tool: run nmap",
    ]
    working_memory = interactive.facts.metadata.get("working_memory")
    if isinstance(working_memory, dict):
        working_memory["available_findings"] = []
        working_memory["active"] = {}

    stub = _UsageLLMStub(
        content='{"root_cause":"stuck loop","alternative_approaches":["switch tool family"]}',
        structured_output={
            "root_cause": "stuck loop",
            "alternative_approaches": ["switch tool family"],
        },
    )
    monkeypatch.setattr(
        "agent.graph.nodes.reflect.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )

    await reflect_node(interactive.as_graph_state(), context=None)
    captured = stub.calls[0]
    assert captured["system_prompt"] == (
        "You are an expert problem analyzer helping troubleshoot pentesting issues."
    )
    assert "Stuck in loop: repeated the same action 3 times without progress" in captured[
        "user_prompt"
    ]
    assert "Decision paralysis: same decision repeated 3+ times" in captured["user_prompt"]
    assert "## Current Plan\n1. Run nmap -sV 10.0.0.5\n2. Inspect TLS service" in captured[
        "user_prompt"
    ]
    assert (
        captured["kwargs"]["structured_output"].name == "reflection_analysis"
    )
    assert captured["kwargs"]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_think_more_payload(monkeypatch) -> None:
    """Snapshot think-more node LLM payload assembly."""
    interactive = _interactive_state_for_nodes()
    stub = _UsageLLMStub(
        content='{"reasoning":"analyze findings","updated_plan":[],"next_goal":"continue","key_observations":[]}',
        structured_output={
            "reasoning": "analyze findings",
            "updated_plan": [],
            "next_goal": "continue",
            "key_observations": [],
        },
    )
    monkeypatch.setattr(
        "agent.graph.nodes.think_more.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )

    await think_more_node(interactive.as_graph_state(), context=None, config=None, writer=None)
    captured = stub.calls[0]
    assert_golden(
        "memory_consolidation__think_more_payload.json",
        json.dumps(captured, indent=2, sort_keys=True, default=str),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_synthesis_payload(monkeypatch) -> None:
    """Snapshot synthesis node LLM payload assembly."""
    interactive = _interactive_state_for_nodes()
    stub = _UsageLLMStub(content="Loop detected; summarizing findings and next steps.")
    monkeypatch.setattr(
        "agent.graph.nodes.synthesis.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )

    await synthesis_node(interactive.as_graph_state(), context=None)
    captured = stub.calls[0]
    assert_golden(
        "memory_consolidation__synthesis_payload.json",
        json.dumps(captured, indent=2, sort_keys=True),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_finalize_results_messages(monkeypatch) -> None:
    """Snapshot finalize-tool-results message list passed to LLM."""
    interactive = _interactive_state_for_nodes()
    stub = _StreamingLLMStub(chunks=["Final answer from finalize_results."])
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.get_stream_writer",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )

    await finalize_tool_results(interactive.as_graph_state(), context=None, config={})
    assert_golden(
        "memory_consolidation__finalize_results_messages.json",
        json.dumps(stub.messages_payloads[0], indent=2, sort_keys=True),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_dr_finalizer_messages(monkeypatch) -> None:
    """Snapshot deep-reasoning finalizer message list passed to LLM."""
    interactive = _interactive_state_for_nodes()
    stub = _StreamingLLMStub(chunks=["Final answer from deep_reasoning_finalizer."])
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.get_stream_writer",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.resolve_llm_client",
        lambda *_args, **_kwargs: stub,
    )

    await finalize_deep_reasoning(interactive.as_graph_state(), context=None, config={})
    assert_golden(
        "memory_consolidation__deep_reasoning_finalizer_messages.json",
        json.dumps(stub.messages_payloads[0], indent=2, sort_keys=True),
    )


@pytest.mark.asyncio
async def test_memory_consolidation_snapshot_compressor_llm_payload(monkeypatch) -> None:
    """Snapshot payload passed from compressor boundary into tool processor."""
    captured: Dict[str, Any] = {}

    async def _fake_process_output(
        self: Any,
        *,
        tool_name: str,
        raw_output: str,
        metadata: Dict[str, Any],
    ) -> Any:
        captured["tool_name"] = tool_name
        captured["raw_output"] = raw_output
        captured["metadata"] = metadata
        return SimpleNamespace(
            summary="443/tcp open https",
            key_findings=["443/tcp open https"],
            structured_signals=[],
            decision_evidence=["nmap output observed"],
            lossiness_risk="low",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            analysis_reason="",
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _fake_process_output,
    )

    await compress_tool_output(
        tool_name="nmap.scan",
        raw_result={
            "status": "success",
            "success": True,
            "stdout": "443/tcp open https\n",
            "stderr": "",
            "parameters": {"target": "10.0.0.5"},
            "metadata": {"semantic_observations": ["open port"]},
        },
        artifact_path=None,
        execution_id="exec-1",
        llm_client=SimpleNamespace(model="gpt-5.2"),
    )

    assert_golden(
        "memory_consolidation__compressor_llm_payload.json",
        json.dumps(captured, indent=2, sort_keys=True),
    )
