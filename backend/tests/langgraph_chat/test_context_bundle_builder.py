"""Unit tests for the hot-path ``ConversationContextBundle`` builder.

Locks in Phase 1's single-assembly authority: the builder in
``agent.graph.context.builder`` produces a bundle whose shape, section
ordering, and determinism are consumable by every prompt-authoritative
role (directly or via a Phase 2 projection).

These tests explicitly avoid coupling to persistence, Docker, or the
LangGraph facade — they feed plain OpenAI-style message dicts through
the public builder signature.
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.contracts import (
    EvidenceRef,
    RuntimeStateSnapshot,
)


def _turn(index: int) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"user question {index}"},
        {"role": "assistant", "content": f"assistant answer {index}"},
    ]


def _messages(turn_count: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(turn_count):
        out.extend(_turn(i))
    return out


def test_metadata_key_constant_is_stable() -> None:
    assert METADATA_CONTEXT_BUNDLE_KEY == "context_bundle"


def test_bundle_exposes_all_required_keys() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=_messages(turn_count=3),
    )

    expected_keys = {
        "conversation_id",
        "turn_id",
        "turn_sequence",
        "transcript_window",
        "runtime_state",
        "evidence_refs",
        "current_user_turn",
        "retrieved_prior_context",
        "prior_turn_references",
    }
    assert set(bundle.keys()) == expected_keys

    assert bundle["conversation_id"] == "conv-1"
    assert bundle["turn_id"] == "turn-1"
    assert bundle["turn_sequence"] == 0


def test_retrieved_prior_context_is_reserved_empty() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=_messages(turn_count=2),
    )

    assert bundle["retrieved_prior_context"] == []


def test_runtime_state_defaults_to_empty_snapshot_shape() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=_messages(turn_count=1),
    )

    runtime_state = bundle["runtime_state"]
    assert runtime_state == {
        "active_target": None,
        "current_goal": None,
        "current_decision": None,
        "in_flight_tool": None,
        "active_todo": None,
        "handles": {},
    }


def test_evidence_refs_default_to_empty_list() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=_messages(turn_count=1),
    )

    assert bundle["evidence_refs"] == []


def test_runtime_state_and_evidence_refs_are_passed_through() -> None:
    runtime_state: RuntimeStateSnapshot = {
        "active_target": {"host": "10.0.0.5"},
        "current_goal": {"summary": "enumerate services"},
        "current_decision": None,
        "in_flight_tool": None,
        "handles": {"session_id": "abc"},
    }
    evidence: list[EvidenceRef] = [
        {
            "evidence_id": "ev-1",
            "kind": "finding",
            "summary": "open port 22",
            "source": "nmap",
        },
    ]

    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=_messages(turn_count=1),
        runtime_state=runtime_state,
        evidence_refs=evidence,
    )

    assert bundle["runtime_state"] == runtime_state
    assert bundle["evidence_refs"] == evidence


def test_transcript_window_contains_last_n_turns_verbatim() -> None:
    # 12 turns > target 10 -> transcript_window must carry the final 10
    # turns, byte-identical, with drop count 2.
    messages = _messages(turn_count=12)

    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=messages,
    )

    window = bundle["transcript_window"]
    assert window["dropped_older_turn_count"] == 2
    assert window["target_turn_count"] == 10
    assert window["hard_minimum_turn_count"] == 5

    # 10 kept turns * 2 messages/turn = 20 messages, all verbatim.
    kept = messages[2 * 2 :]
    assert window["turns"] == kept


def test_bundle_serialization_is_deterministic() -> None:
    # Same inputs must produce equal bundles across calls — the
    # hot-path authority relies on deterministic assembly for
    # cache-friendly prompt prefixes.
    messages = _messages(turn_count=4)
    runtime_state: RuntimeStateSnapshot = {
        "active_target": {"host": "example.com"},
        "current_goal": {"summary": "probe"},
        "current_decision": {"action": "run_tool"},
        "in_flight_tool": None,
        "handles": {"session": "s-1"},
    }
    evidence: list[EvidenceRef] = [
        {
            "evidence_id": "ev-1",
            "kind": "finding",
            "summary": "open port 80",
            "source": "nmap",
        },
        {
            "evidence_id": "ev-2",
            "kind": "artifact",
            "summary": "banner grab",
            "source": "curl",
        },
    ]

    first = build_conversation_context_bundle(
        conversation_id="conv-det",
        turn_id="turn-det",
        turn_sequence=7,
        messages=messages,
        runtime_state=runtime_state,
        evidence_refs=evidence,
    )
    second = build_conversation_context_bundle(
        conversation_id="conv-det",
        turn_id="turn-det",
        turn_sequence=7,
        messages=messages,
        runtime_state=runtime_state,
        evidence_refs=evidence,
    )

    # Dict equality: key order in TypedDicts is irrelevant at runtime,
    # but every value (including nested lists / dicts) must match.
    assert first == second

    # Mutating the caller's evidence list after the fact must not alter
    # the already-built bundle (defensive copy is part of determinism).
    evidence.append(
        {
            "evidence_id": "ev-3",
            "kind": "observation",
            "summary": "late addition",
            "source": "test",
        }
    )
    assert len(first["evidence_refs"]) == 2
    assert first == second
