"""Regression tests for intent metadata handoff into graph-state metadata."""

from __future__ import annotations

import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

# Some backend modules expect DATABASE_URL at import time.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes.select_tool_categories import select_tool_categories_node
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade_helpers import build_metadata
from backend.services.langgraph_chat.intent.briefs import (
    METADATA_KEY_INTENT_BRIEF_SEED,
    METADATA_KEY_INTENT_TARGET_CONTINUITY,
    METADATA_KEY_INTENT_TARGET_RESOLUTION,
    METADATA_KEY_REQUEST_CONTRACT,
    METADATA_KEY_TURN_INTERPRETATION,
)


def _chat_inputs(
    *,
    message: str,
    history: List[Dict[str, Any]] | None = None,
) -> ChatInputs:
    return ChatInputs(
        task_id=99,
        user_id=1,
        message=message,
        conversation_id="conv-intent-handoff",
        history=list(history or []),
        api_key="test-key",
        model="gpt-test",
    )


def _runtime_config(
    *,
    chat_inputs: ChatInputs,
    extra_metadata: Dict[str, Any],
) -> LangGraphRuntimeConfig:
    metadata = dict(extra_metadata)
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id=chat_inputs.conversation_id or "",
        turn_id=str(metadata.get("turn_id") or ""),
        turn_sequence=int(metadata.get("turn_sequence") or 0),
        messages=list(chat_inputs.history),
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata=metadata,
    )


def test_build_metadata_forwards_intent_seed_and_classifier_keys() -> None:
    chat_inputs = _chat_inputs(message="scan 10.129.30.246")
    seeded = {
        METADATA_KEY_TURN_INTERPRETATION: {
            "resolved_user_intent": "Scan host for services",
            "next_operational_goal": "Run nmap -sV",
            "execution_readiness": "ready",
        },
        METADATA_KEY_INTENT_BRIEF_SEED: {
            "resolved_user_intent": "Scan host for services",
            "next_operational_goal": "Run nmap -sV",
            "execution_readiness": "ready",
            "target_status": "resolved",
            "target_source": "explicit_current_message",
            "explicit_constraints": [],
            "suggested_category_focus": [],
            "retrieval_hints": [],
            "relevant_memory_fragments": [],
            "request_contract": {
                "question_type": "multi_step",
                "answer_style": "normal",
                "terminal_when": "all_steps_done",
            },
        },
        METADATA_KEY_REQUEST_CONTRACT: {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        METADATA_KEY_INTENT_TARGET_RESOLUTION: {
            "target_status": "resolved",
            "resolved_target": "10.129.30.246",
            "target_source": "explicit_current_message",
        },
        METADATA_KEY_INTENT_TARGET_CONTINUITY: {"status": "disallow"},
    }
    runtime_config = _runtime_config(chat_inputs=chat_inputs, extra_metadata=seeded)

    metadata = build_metadata(chat_inputs, runtime_config)

    for key, value in seeded.items():
        assert key in metadata, f"missing key after build_metadata handoff: {key}"
        assert metadata[key] == value


@pytest.mark.asyncio
async def test_category_selector_reads_intent_brief_from_working_memory() -> None:
    chat_inputs = _chat_inputs(message="scan this host and detect services")
    runtime_config = _runtime_config(chat_inputs=chat_inputs, extra_metadata={})
    metadata = build_metadata(chat_inputs, runtime_config)
    metadata["working_memory"] = {
        "intent_brief": {
            "resolved_user_intent": "Scan 10.129.30.246 to identify open services and versions.",
            "overall_goal": "Service and port discovery on a single host.",
            "continuation_mode": "new_request",
            "resolved_step_title": "Scan target host",
            "resolved_step_detail": "Perform host scan and version detection",
            "next_operational_goal": "Run nmap -sV against 10.129.30.246",
            "success_condition": "Return open ports and version strings.",
            "execution_readiness": "ready",
            "blocking_reason": None,
            "resolved_target": "10.129.30.246",
            "target_status": "resolved",
            "target_source": "explicit_current_message",
            "explicit_constraints": [],
            "suggested_category_focus": ["information_gathering", "network_scanning"],
            "retrieval_hints": ["nmap -sV", "full port scan then version scan"],
            "relevant_memory_fragments": [],
            "request_contract": {
                "question_type": "multi_step",
                "answer_style": "normal",
                "terminal_when": "all_steps_done",
            },
        }
    }

    state = {
        "facts": {
            "task_id": chat_inputs.task_id,
            "message": chat_inputs.message,
            "selected_tool": None,
            "tool_parameters": {},
            "metadata": metadata,
        },
        "trace": {"history": [], "reasoning": []},
    }

    captured_prompt: Dict[str, str] = {"value": ""}

    async def _capture_prompt(**kwargs):  # noqa: ANN003
        captured_prompt["value"] = kwargs["prompt"]
        return ["information_gathering"]

    with patch(
        "agent.tools.category_utils.get_tool_categories",
        return_value=["information_gathering", "network_scanning"],
    ), patch(
        "agent.tools.category_utils.get_category_descriptions",
        return_value={
            "information_gathering": "Reconnaissance and discovery",
            "network_scanning": "Host and service enumeration",
        },
    ), patch(
        "agent.graph.nodes.select_tool_categories._call_llm_for_categories",
        new=AsyncMock(side_effect=_capture_prompt),
    ):
        await select_tool_categories_node(state)

    prompt = captured_prompt["value"]
    assert "Turn Execution Brief" in prompt
    assert "Scan 10.129.30.246 to identify open services and versions." in prompt
    assert "Run nmap -sV against 10.129.30.246" in prompt
    assert "question_type: multi_step" in prompt
