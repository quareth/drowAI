"""End-to-end continuity regression at the bundle + facade layer.

Locks in the Phase 6 success criterion that a canonical follow-up
scenario — turn 1 "scan 5.5.5.5" -> turn 2 "enumerate it" — preserves
target continuity on two independent surfaces:

1. The turn-2 planner projection's ``recent_transcript`` section must
   carry both user turns *verbatim* (no truncation / no re-wording),
   so the LLM planner sees "scan 5.5.5.5" when it reads the recent
   transcript on turn 2.
2. The turn-2 bundle's ``runtime_state.active_target`` must reflect
   the target established on turn 1 (``5.5.5.5``), because the
   working-memory reducer refreshed the bundle after the turn-1
   mutation via ``refresh_bundle_from_working_memory``.

Scope: exercises the real ``build_metadata`` facade seam plus the
shared projection layer. It does not boot the full LangGraph runtime —
the point is to prove the bundle *and* its runtime-state slot work
together across turns.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.projections import (
    SECTION_RECENT_TRANSCRIPT,
    SECTION_RUNTIME_STATE,
    project_for_planner,
    serialize_projection_to_prompt_sections,
)
from agent.graph.context.runtime_state import refresh_bundle_from_working_memory
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade_helpers import build_metadata


def _seeded_runtime_config(
    chat_inputs: ChatInputs,
    extra_metadata: Dict[str, Any] | None = None,
) -> LangGraphRuntimeConfig:
    """Build runtime_config with a bundle pre-seeded (mimics context builder)."""
    metadata: Dict[str, Any] = dict(extra_metadata or {})
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


def _chat_inputs(history: List[Dict[str, Any]], message: str) -> ChatInputs:
    return ChatInputs(
        message=message,
        history=list(history),
        conversation_id="conv-e2e-continuity",
        task_id=777,
        user_id=1,
        api_key="sk-stub",
        model="gpt-test",
    )


def _runtime_config(metadata: Dict[str, Any]) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=_chat_inputs(history=[], message=""),
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata=dict(metadata),
    )


def _section_content(sections: List[Dict[str, str]], name: str) -> str:
    for section in sections:
        if section.get("name") == name:
            return section.get("content", "")
    raise AssertionError(f"section {name!r} missing from projection serialization")


def test_followup_preserves_target_in_bundle_transcript_and_runtime_state() -> None:
    """Turn 2 sees turn 1's target both in transcript and runtime state.

    The scenario walks two turns of a conversation through the real
    facade seam:

    - Turn 1: user sends "scan 5.5.5.5". The working-memory reducer
      runs (simulated here by placing the reduced working_memory on
      metadata), then the bundle is refreshed so its runtime_state
      reflects the newly active target.
    - Turn 2: user sends "enumerate it". Facade builds a new metadata
      with the turn-1 assistant reply in history and the persisted
      working_memory carried across. The turn-2 bundle must project
      both user turns verbatim AND carry ``active_target = 5.5.5.5``
      in its runtime_state.
    """
    # --- Turn 1 metadata build ------------------------------------------------
    turn1_history: List[Dict[str, Any]] = []  # no prior turns yet
    turn1_inputs = _chat_inputs(history=turn1_history, message="scan 5.5.5.5")
    turn1_runtime = _seeded_runtime_config(
        turn1_inputs,
        {"turn_sequence": 0, "turn_id": "turn-1"},
    )
    turn1_metadata = build_metadata(turn1_inputs, turn1_runtime)

    # Simulate the working-memory reducer establishing the active target
    # after turn 1 runs. In production this is done by
    # ``agent/graph/nodes/working_memory.py`` after the reducer produces a
    # normalized working-memory dict; here we install an equivalent shape
    # so the runtime_state derivation has something to read.
    turn1_metadata["working_memory"] = {
        "active": {"target_id": "target:host:5.5.5.5"},
        "referents": {
            "host:5.5.5.5": {
                "value": "5.5.5.5",
                "kind": "host",
            },
        },
        "objective": {
            "text": "scan 5.5.5.5",
            "status": "active",
        },
        "tool_runs": [],
    }
    refresh_bundle_from_working_memory(turn1_metadata)

    # Sanity: turn 1's bundle runtime_state carries the active target.
    turn1_bundle = turn1_metadata[METADATA_CONTEXT_BUNDLE_KEY]
    assert turn1_bundle["runtime_state"]["active_target"] == {
        "target_id": "target:host:5.5.5.5",
        "value": "5.5.5.5",
        "kind": "host",
    }

    # --- Turn 2 metadata build ------------------------------------------------
    # The persisted transcript now includes turn 1's user+assistant pair.
    turn2_history: List[Dict[str, Any]] = [
        {"role": "user", "content": "scan 5.5.5.5"},
        {
            "role": "assistant",
            "content": "Completed scan on 5.5.5.5; ports 22 and 80 open.",
        },
    ]
    turn2_inputs = _chat_inputs(history=turn2_history, message="enumerate it")
    turn2_runtime = _seeded_runtime_config(
        turn2_inputs,
        {
            "turn_sequence": 1,
            "turn_id": "turn-2",
            # Facade carries working_memory forward across turns so the
            # turn-2 bundle can be seeded with the prior runtime state.
            "working_memory": turn1_metadata["working_memory"],
        },
    )
    turn2_metadata = build_metadata(turn2_inputs, turn2_runtime)
    turn2_bundle = turn2_metadata[METADATA_CONTEXT_BUNDLE_KEY]

    # --- Assertion 1: planner projection shows both user turns verbatim ------
    projection = project_for_planner(turn2_bundle)
    sections = serialize_projection_to_prompt_sections(projection)
    transcript = _section_content(sections, SECTION_RECENT_TRANSCRIPT)

    # Bounded turn-block rendering: each message sits inside a
    # ``<turn n=N role=R>…</turn>`` pair, so every user turn is clearly
    # bounded from the surrounding assistant response even when the
    # assistant answer spans many lines.
    assert "<turn n=1 role=user>\nscan 5.5.5.5\n</turn>" in transcript
    assert (
        "<turn n=1 role=assistant>\n"
        "Completed scan on 5.5.5.5; ports 22 and 80 open.\n"
        "</turn>"
    ) in transcript
    # The turn-2 user message is the agent's *current* turn; the bundle's
    # recent transcript carries only persisted prior turns (the current
    # user message is appended separately by each role's prompt builder),
    # so "enumerate it" must not appear in the serialized transcript
    # section derived from the persisted history.
    assert "enumerate it" not in transcript

    # --- Assertion 2: turn 2 runtime_state reflects turn 1's active target ---
    assert turn2_bundle["runtime_state"]["active_target"] == {
        "target_id": "target:host:5.5.5.5",
        "value": "5.5.5.5",
        "kind": "host",
    }

    runtime_state_section = _section_content(sections, SECTION_RUNTIME_STATE)
    assert "active_target" in runtime_state_section
    assert "5.5.5.5" in runtime_state_section


def test_appended_turn_extends_transcript_prefix_without_shifting_earlier_blocks() -> None:
    """Cache-stability end-to-end: append-only growth of the prompt prefix.

    Building the shared projection/serializer surface for an N-turn
    conversation and then for the same conversation plus one appended
    turn must produce a serialized transcript whose N-turn prefix is
    byte-identical. This guards the provider-side prompt-prefix cache
    contract end-to-end — a silent drift in section ordering, role
    labels, separators, or metadata inclusion would shift the prefix
    and invalidate the cache on every turn.
    """
    conversation_id = "conv-cache-stability"

    turn1_history: List[Dict[str, Any]] = [
        {"role": "user", "content": "scan 5.5.5.5"},
        {
            "role": "assistant",
            "content": "Completed scan on 5.5.5.5; ports 22 and 80 open.",
        },
    ]
    turn1_inputs = _chat_inputs(history=turn1_history, message="continue")
    turn1_inputs.conversation_id = conversation_id
    turn1_runtime = _seeded_runtime_config(
        turn1_inputs,
        {"turn_sequence": 0, "turn_id": "turn-1"},
    )
    turn1_metadata = build_metadata(turn1_inputs, turn1_runtime)
    turn1_bundle = turn1_metadata[METADATA_CONTEXT_BUNDLE_KEY]
    turn1_sections = serialize_projection_to_prompt_sections(
        project_for_planner(turn1_bundle)
    )
    turn1_transcript = _section_content(turn1_sections, SECTION_RECENT_TRANSCRIPT)

    # Turn 2 persists turn 1's pair plus a new user/assistant pair.
    turn2_history = turn1_history + [
        {"role": "user", "content": "enumerate service on 5.5.5.5"},
        {"role": "assistant", "content": "Running enumeration now."},
    ]
    turn2_inputs = _chat_inputs(history=turn2_history, message="next")
    turn2_inputs.conversation_id = conversation_id
    turn2_runtime = _seeded_runtime_config(
        turn2_inputs,
        {"turn_sequence": 1, "turn_id": "turn-2"},
    )
    turn2_metadata = build_metadata(turn2_inputs, turn2_runtime)
    turn2_bundle = turn2_metadata[METADATA_CONTEXT_BUNDLE_KEY]
    turn2_sections = serialize_projection_to_prompt_sections(
        project_for_planner(turn2_bundle)
    )
    turn2_transcript = _section_content(turn2_sections, SECTION_RECENT_TRANSCRIPT)

    # The new appended turn is the only delta; the earlier prefix is
    # byte-identical between turn 1 and turn 2.
    assert turn2_transcript.startswith(turn1_transcript)
    appended_tail = turn2_transcript[len(turn1_transcript) :]
    assert appended_tail.startswith(
        "\n\n<turn n=2 role=user>\nenumerate service on 5.5.5.5\n</turn>"
    )
    assert appended_tail.endswith(
        "<turn n=2 role=assistant>\nRunning enumeration now.\n</turn>"
    )

    # Section ordering is stable across both turns (cache prefix shape).
    assert [section["name"] for section in turn1_sections] == [
        section["name"] for section in turn2_sections
    ]
