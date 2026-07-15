"""Phase 0 characterization snapshots for node-level prompt/message assembly.

These tests lock node-side memory plumbing inputs so later carrier moves
can prove no behavioral drift at the prompt/message boundary.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.nodes.select_tool_categories import _build_category_selection_prompt
from agent.graph.nodes.simple_chat import (
    _build_simple_chat_messages,
    _messages_from_bundle,
    _referenced_prior_turns_from_bundle,
)
from core.prompts.tests._golden import assert_golden


def _intent_brief() -> Dict[str, Any]:
    """Return deterministic classifier-brief data for snapshot calls."""
    return {
        "resolved_user_intent": "Enumerate open services on 10.0.0.5",
        "overall_goal": "Map exposed network surface",
        "continuation_mode": "new_request",
        "resolved_step_title": "Discovery",
        "resolved_step_detail": "Start with TCP service scan",
        "next_operational_goal": "Run targeted nmap scan",
        "success_condition": "Open ports and likely services identified",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "resolved_target": "10.0.0.5",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": ["No UDP", "Stay low-noise"],
        "suggested_category_focus": ["network_recon"],
        "retrieval_hints": ["nmap", "service detection"],
        "relevant_memory_fragments": ["host alive from previous ping sweep"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
    }


def test_memory_consolidation_snapshot_simple_chat_messages_from_bundle() -> None:
    """Snapshot message list assembled from bundle transcript + current turn."""
    metadata = {
        METADATA_CONTEXT_BUNDLE_KEY: {
            "transcript_window": {
                "turns": [
                    {"role": "user", "content": "scan 10.0.0.5"},
                    {"role": "assistant", "content": "Starting scan now."},
                ]
            },
            "current_user_turn": {
                "role": "user",
                "content": "include service detection",
            },
        },
        "long_term_memory_summary": "must not affect simple-chat messages",
    }
    history, current_user_turn = _messages_from_bundle(metadata)
    messages = _build_simple_chat_messages(history, current_user_turn)
    assert_golden(
        "memory_consolidation__simple_chat_messages_from_bundle.json",
        json.dumps(messages, indent=2, sort_keys=True),
    )


def test_memory_consolidation_snapshot_simple_chat_messages_without_current_turn() -> None:
    """Snapshot message list when bundle has no in-flight current user turn."""
    metadata = {
        METADATA_CONTEXT_BUNDLE_KEY: {
            "transcript_window": {
                "turns": [
                    {"role": "user", "content": "scan 10.0.0.5"},
                    {"role": "assistant", "content": "Starting scan now."},
                ]
            },
            "current_user_turn": None,
        }
    }
    history, current_user_turn = _messages_from_bundle(metadata)
    messages = _build_simple_chat_messages(history, current_user_turn)
    assert_golden(
        "memory_consolidation__simple_chat_messages_no_current_turn.json",
        json.dumps(messages, indent=2, sort_keys=True),
    )


def test_simple_chat_includes_materialized_prior_turns_only_when_present() -> None:
    metadata = {
        METADATA_CONTEXT_BUNDLE_KEY: {
            "transcript_window": {"turns": []},
            "current_user_turn": {"role": "user", "content": "what did you mean?"},
            "prior_turn_references": {
                "operation": "reference_resolution",
                "status": "partial",
                "materialized_turns": [
                    {
                        "turn_number": 2,
                        "speaker": "assistant",
                        "message_id": 9,
                        "text": "Capture packets from that traffic.",
                    }
                ],
                "unresolved_hints": [{"anchor_text": "MODEL ANCHOR"}],
            },
        }
    }

    history, current_user_turn = _messages_from_bundle(metadata)
    messages = _build_simple_chat_messages(
        history,
        current_user_turn,
        _referenced_prior_turns_from_bundle(metadata),
    )

    assert messages[1]["role"] == "system"
    assert "Capture packets from that traffic." in messages[1]["content"]
    assert "MODEL ANCHOR" not in messages[1]["content"]


def test_memory_consolidation_snapshot_category_selector_prompt_full() -> None:
    """Snapshot category-selector prompt with populated brief + hint."""
    prompt = _build_category_selection_prompt(
        available_categories=["network_recon", "web_assessment", "database_assessment"],
        category_descriptions={
            "network_recon": "Host and service discovery tools",
            "web_assessment": "Web application testing tools",
            "database_assessment": "Database discovery and assessment tools",
        },
        next_tool_hint="run targeted nmap service scan",
        intent_brief=_intent_brief(),
    )
    assert_golden("memory_consolidation__category_selector_node_full.txt", prompt)


def test_memory_consolidation_snapshot_category_selector_prompt_sparse() -> None:
    """Snapshot category-selector prompt with empty brief and no hint."""
    prompt = _build_category_selection_prompt(
        available_categories=["network_recon", "web_assessment"],
        category_descriptions={
            "network_recon": "Host and service discovery tools",
            "web_assessment": "Web application testing tools",
        },
        next_tool_hint=None,
        intent_brief={},
    )
    assert_golden("memory_consolidation__category_selector_node_sparse.txt", prompt)
