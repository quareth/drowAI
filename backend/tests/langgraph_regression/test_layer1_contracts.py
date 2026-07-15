"""Layer 1 contract tests for prompt, history, and payload wiring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from agent.graph.nodes.simple_chat import _build_simple_chat_messages
from backend.services.langgraph_chat.contracts import ChatInputs, ExecutionMode, LangGraphRuntimeConfig
from backend.services.langgraph_chat.intent.classifier import IntentClassifier
from core.prompts.constants import CLASSIFIER_SYSTEM_PROMPT, PROMPT_TEMPLATE
from core.prompts.loader import TemplateLoader
from core.prompts.registry import PromptRegistry

pytestmark = [
    pytest.mark.regression_layer1,
    pytest.mark.regression_quick,
    pytest.mark.regression_main,
    pytest.mark.regression_nightly,
]


@dataclass
class _StubLLMResult:
    content: str
    usage: Optional[Any] = None


class _RecordingIntentClient:
    """Records classifier payloads and returns a deterministic response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[Dict[str, Any]] = []

    async def chat_with_usage(  # noqa: D401 - test stub
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> _StubLLMResult:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        return _StubLLMResult(content=self.response, usage=None)


def test_intent_templates_resolve_from_latest_pointer() -> None:
    """Intent constants must map to `versions/intent/latest.txt` payloads."""
    versions_root = Path(__file__).resolve().parents[3] / "core" / "prompts" / "versions"
    loader = TemplateLoader(versions_root)

    assert loader.load_latest_version("intent", "intent_classifier.txt") == CLASSIFIER_SYSTEM_PROMPT
    assert loader.load_latest_version("intent", "prompt_template.txt") == PROMPT_TEMPLATE


def test_prompt_registry_exposes_intent_template_contract() -> None:
    registry = PromptRegistry()
    assert registry.get_latest_version("intent") == "v10"
    assert registry.get_template("intent_classifier") == CLASSIFIER_SYSTEM_PROMPT


def test_intent_classifier_prompt_keeps_latest_turn_as_intent_authority() -> None:
    """Latest user turn must bound current action scope despite prior context."""
    assert "latest=true as the only source of current user intent" in CLASSIFIER_SYSTEM_PROMPT
    assert "Do not let earlier turns expand the latest turn's requested action" in (
        CLASSIFIER_SYSTEM_PROMPT
    )
    assert "Create seeds only for work grounded in the latest user intent" in (
        CLASSIFIER_SYSTEM_PROMPT
    )


def test_build_metadata_history_contract(regression_harness) -> None:
    history = [
        {"role": "user", "content": "Scan 10.0.0.5"},
        {"role": "assistant", "content": "Ports 22 and 80 are open."},
    ]
    metadata = regression_harness.build_history_metadata(
        message="Continue with service checks",
        history=history,
        metadata={"eligible_routes": ["normal_chat"]},
    )

    assert metadata["history_turns"] == 2
    assert metadata["conversation_history"] == history
    assert isinstance(metadata["conversation_history"], list)
    assert metadata["execution_mode"] == ExecutionMode.NORMAL_CHAT.value
    assert "graph_runtime_context" in metadata


@pytest.mark.asyncio
async def test_intent_classifier_payload_contract_uses_rendered_prompt_template() -> None:
    response = (
        '{"label":"direct_executor","confidence":0.81,"reasoning":"tool path",'
        '"suggested_capabilities":["direct_executor"],"risk_flags":[]}'
    )
    recording_client = _RecordingIntentClient(response)
    classifier = IntentClassifier(client_factory=lambda call_settings: recording_client)

    history = [
        {"role": "user", "content": "Scan 10.0.0.5"},
        {"role": "assistant", "content": "Ports 22 and 80 are open."},
    ]
    chat_inputs = ChatInputs(
        task_id=91,
        user_id=8,
        message="Run targeted enumeration on 10.0.0.5.",
        conversation_id="conv-91",
        history=history,
        api_key="test-key",
        model="gpt-5.2",
    )
    # Phase 5 cutover: the classifier requires a
    # ``ConversationContextBundle`` on metadata. Facade / context-builder
    # wiring populate it normally; direct-node tests install one here.
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    runtime_config = LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.NORMAL_CHAT,
        metadata={
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["nmap"], "targets": ["10.0.0.5"]},
            "risk_flags": [],
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-91",
                turn_id="turn-91",
                turn_sequence=0,
                messages=list(history),
                current_message=chat_inputs.message,
            ),
        },
    )

    result = await classifier.enrich_runtime_config(runtime_config)

    assert result is not None
    assert len(recording_client.calls) == 1
    payload = recording_client.calls[0]
    assert payload["system_prompt"] == CLASSIFIER_SYSTEM_PROMPT
    assert "Run targeted enumeration on 10.0.0.5." in payload["user_prompt"]
    # Shared serializer (agent/graph/context/serialization.py) renders
    # each message inside a bounded ``<turn n=N role=R>…</turn>`` block
    # so multiline assistant answers never visually swallow later user
    # turns. Every prompt-authoritative role consumes this same format.
    assert (
        "<turn n=1 role=assistant>\nPorts 22 and 80 are open.\n</turn>"
        in payload["user_prompt"]
    )
    assert "nmap" in payload["user_prompt"]
    assert runtime_config.metadata["intent_classifier_label"] == "direct_executor"
    assert runtime_config.execution_mode == ExecutionMode.SIMPLE_TOOL


def test_simple_chat_message_contract_preserves_order_and_filters_roles() -> None:
    messages = _build_simple_chat_messages(
        history=[
            {"role": "assistant", "content": "previous answer"},
            {"role": "invalid", "content": "ignore me"},
            {"role": "user", "content": "previous question"},
        ],
        current_user_turn={"role": "user", "content": "current question"},
    )

    roles = [entry["role"] for entry in messages]
    assert roles == ["system", "assistant", "user", "user"]
    assert messages[-1]["content"] == "current question"
