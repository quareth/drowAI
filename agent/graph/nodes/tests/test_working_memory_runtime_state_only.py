"""Phase 4 contract tests — working memory is runtime-state only.

These tests lock in that:

- Prompt paths that run without ``trace.scratchpad`` produce the
  same continuity behavior as paths that run with a bogus scratchpad.
  Scratchpad drift cannot change prompt continuity.
"""

from __future__ import annotations

from typing import Any, Dict

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.contracts import RuntimeStateSnapshot
from agent.graph.context.projections import (
    project_for_category_selector,
    project_for_planner,
    serialize_projection_to_prompt_sections,
)


# ---------------------------------------------------------------------------
# Scratchpad absence / drift cannot change prompt continuity
# ---------------------------------------------------------------------------

def _bundle_with_messages(messages: list[Dict[str, Any]]) -> Dict[str, Any]:
    return build_conversation_context_bundle(
        conversation_id="conv-4",
        turn_id="turn-4",
        turn_sequence=4,
        messages=messages,
        runtime_state=RuntimeStateSnapshot(
            active_target={"kind": "host", "value": "10.0.0.1"},
            current_goal=None,
            current_decision=None,
            in_flight_tool=None,
            handles={},
        ),
    )


def _serialize_prompt_surface(bundle: Dict[str, Any]) -> str:
    category_selector_sections = serialize_projection_to_prompt_sections(
        project_for_category_selector(bundle)
    )
    planner_sections = serialize_projection_to_prompt_sections(
        project_for_planner(bundle)
    )
    all_sections = list(category_selector_sections) + list(planner_sections)
    return "\n".join(section.get("content", "") for section in all_sections)


def test_prompt_continuity_is_identical_without_vs_with_bogus_scratchpad() -> None:
    """Scratchpad drift cannot change prompt continuity behavior.

    Build two prompt surfaces (category selector + planner projections)
    from the same bundle — one constructed with no scratchpad attached
    anywhere, the other with a bogus scratchpad attached to the metadata
    envelope. Prompt outputs must be byte-identical for continuity.
    """
    messages = [
        {"role": "user", "content": "scan 10.0.0.1"},
        {"role": "assistant", "content": "Starting SYN scan"},
        {"role": "user", "content": "also list open ports"},
    ]

    # Surface A: no scratchpad anywhere.
    metadata_without_scratchpad: Dict[str, Any] = {}
    metadata_without_scratchpad[METADATA_CONTEXT_BUNDLE_KEY] = _bundle_with_messages(messages)

    # Surface B: bogus scratchpad in metadata AND attached to an
    # interactive trace. Neither should leak into the prompt surface.
    metadata_with_bogus_scratchpad: Dict[str, Any] = {
        "scratchpad": "BOGUS_SCRATCHPAD_SIDECAR_SHOULD_NOT_APPEAR",
    }
    metadata_with_bogus_scratchpad[METADATA_CONTEXT_BUNDLE_KEY] = _bundle_with_messages(messages)

    bundle_a = metadata_without_scratchpad[METADATA_CONTEXT_BUNDLE_KEY]
    bundle_b = metadata_with_bogus_scratchpad[METADATA_CONTEXT_BUNDLE_KEY]

    prompt_surface_a = _serialize_prompt_surface(bundle_a)
    prompt_surface_b = _serialize_prompt_surface(bundle_b)

    assert prompt_surface_a == prompt_surface_b
    assert "BOGUS_SCRATCHPAD_SIDECAR_SHOULD_NOT_APPEAR" not in prompt_surface_b


# ---------------------------------------------------------------------------
# P1 fix: update_working_memory_node refreshes the bundle's runtime state
# ---------------------------------------------------------------------------


def test_update_working_memory_node_refreshes_bundle_runtime_state() -> None:
    """After the WM node runs, ``context_bundle.runtime_state`` mirrors WM.

    Regression guard for the P1 wiring: before the fix, the bundle kept
    an empty runtime state for the entire turn because the WM reducer
    wrote only to ``metadata["working_memory"]``. This test verifies
    the refresh hook keeps both views aligned.
    """
    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.nodes.working_memory import update_working_memory_node

    bundle = build_conversation_context_bundle(
        conversation_id="conv-p1",
        turn_id="turn-p1",
        turn_sequence=0,
        messages=[{"role": "user", "content": "scan 10.0.0.1"}],
    )
    assert bundle["runtime_state"]["active_target"] is None

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-p1",
            "message": "enumerate 10.0.0.1",
            "capability": "simple_tool_execution",
            "metadata": {
                METADATA_CONTEXT_BUNDLE_KEY: bundle,
                "intent_hints": {
                    "targets": [{"value": "10.0.0.1", "kind": "ip"}]
                },
                "conversation_history": [
                    {"role": "user", "content": "enumerate 10.0.0.1", "turn_sequence": 1},
                ],
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }

    context = GraphRuntimeContext(task_id=1, turn_sequence=1, turn_id="turn-p1")
    result = update_working_memory_node(state, context=context)

    updated_metadata = result["facts"]["metadata"]
    bundle_after = updated_metadata[METADATA_CONTEXT_BUNDLE_KEY]

    # WM wrote an active target, and the bundle now reflects it.
    active_target = bundle_after["runtime_state"]["active_target"]
    assert active_target is not None
    assert active_target["value"] == "10.0.0.1"
    assert active_target["target_id"] == "target:intent:target"


def test_update_working_memory_node_projects_classifier_goal_into_runtime_state() -> None:
    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.nodes.working_memory import update_working_memory_node

    bundle = build_conversation_context_bundle(
        conversation_id="conv-goal",
        turn_id="turn-goal",
        turn_sequence=0,
        messages=[{"role": "user", "content": "continue with service enumeration"}],
    )

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 2,
            "conversation_id": "conv-goal",
            "message": "continue with service enumeration",
            "capability": "simple_tool_execution",
            "current_goal": "",
            "metadata": {
                METADATA_CONTEXT_BUNDLE_KEY: bundle,
                "intent_turn_interpretation": {
                    "next_operational_goal": "Enumerate exposed services on 10.0.0.5",
                    "execution_readiness": "ready",
                },
                "conversation_history": [
                    {
                        "role": "user",
                        "content": "continue with service enumeration",
                        "turn_sequence": 1,
                    },
                ],
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }

    context = GraphRuntimeContext(task_id=2, turn_sequence=1, turn_id="turn-goal")
    result = update_working_memory_node(state, context=context)

    updated_facts = result["facts"]
    updated_metadata = updated_facts["metadata"]
    assert updated_facts["current_goal"] == "Enumerate exposed services on 10.0.0.5"
    assert updated_metadata["working_memory"]["objective"]["text"] == "Enumerate exposed services on 10.0.0.5"
    assert updated_metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["current_goal"] == {
        "text": "Enumerate exposed services on 10.0.0.5",
        "status": "active",
    }


def test_update_working_memory_node_preserves_planner_owned_goal_after_plan_ready() -> None:
    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.nodes.working_memory import update_working_memory_node

    bundle = build_conversation_context_bundle(
        conversation_id="conv-dr",
        turn_id="turn-dr",
        turn_sequence=0,
        messages=[{"role": "user", "content": "continue the approved plan"}],
    )

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 3,
            "conversation_id": "conv-dr",
            "message": "continue the approved plan",
            "capability": "deep_reasoning",
            "plan": ["Enumerate services", "Assess exposures"],
            "current_goal": "Enumerate services",
            "metadata": {
                METADATA_CONTEXT_BUNDLE_KEY: bundle,
                "planner_mode": "plan_ready",
                "intent_turn_interpretation": {
                    "next_operational_goal": "Restart from target confirmation",
                    "execution_readiness": "ready",
                },
                "conversation_history": [
                    {
                        "role": "user",
                        "content": "continue the approved plan",
                        "turn_sequence": 2,
                    },
                ],
            },
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }

    context = GraphRuntimeContext(task_id=3, turn_sequence=2, turn_id="turn-dr")
    result = update_working_memory_node(state, context=context)

    updated_facts = result["facts"]
    updated_metadata = updated_facts["metadata"]

    assert updated_facts["current_goal"] == "Enumerate services"
    assert updated_metadata["working_memory"]["objective"]["text"] == "Enumerate services"
    assert updated_metadata["working_memory"]["objective"]["source"] == "planner_current_goal"
    assert updated_metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["current_goal"] == {
        "text": "Enumerate services",
        "status": "active",
    }
