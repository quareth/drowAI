"""Regression tests for runtime-state bundle wiring gaps (B1 / B2 / C).

- B1: ``runtime_state_snapshot_from_working_memory`` must populate the
  ``handles`` field from ``wm["active"]`` (target/subject/collection
  IDs), not hardcode ``{}``.
- B2: ``context.runtime_state.sync_target_hint_from_plan_todo`` must
  refresh the bundle after mutating working memory so prompt consumers
  observe the newly-synced target.
- C: the planner's working-memory summary is derived from the bundle's
  planner projection, not from a second read of
  ``metadata["working_memory"]``.
"""

from __future__ import annotations

from typing import Dict

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.runtime_state import (
    refresh_bundle_from_working_memory,
    runtime_state_snapshot_from_working_memory,
    sync_target_hint_from_plan_todo,
)


def test_runtime_state_snapshot_populates_handles_from_active() -> None:
    """Working memory's typed handles must surface on the bundle snapshot."""
    wm = {
        "active": {
            "target_id": "target:intent:target",
            "subject_id": "entity:host:10.0.0.1",
            "collection_id": "collection:scans",
        },
        "entities": {"host:10.0.0.1": {}},
        "referents": {"intent:target": {"value": "10.0.0.1", "kind": "ip"}},
    }

    snapshot = runtime_state_snapshot_from_working_memory(wm)

    assert snapshot["handles"] == {
        "target_id": "target:intent:target",
        "subject_id": "entity:host:10.0.0.1",
        "collection_id": "collection:scans",
    }


def test_runtime_state_snapshot_omits_empty_handles() -> None:
    """Missing / blank handle slots drop out of the snapshot cleanly."""
    wm = {"active": {"target_id": None, "subject_id": "", "collection_id": None}}

    snapshot = runtime_state_snapshot_from_working_memory(wm)

    assert snapshot["handles"] == {}


def test_refresh_bundle_syncs_handles_after_active_mutation() -> None:
    """After a WM mutation that sets a handle, a bundle refresh exposes it."""
    bundle = build_conversation_context_bundle(
        conversation_id="conv-sync",
        turn_id="turn-sync",
        turn_sequence=0,
        messages=[{"role": "user", "content": "scan 10.0.0.1"}],
    )
    assert bundle["runtime_state"]["handles"] == {}

    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
        "working_memory": {
            "active": {"target_id": "target:intent:target"},
            "referents": {"intent:target": {"value": "10.0.0.1"}},
        },
    }
    refresh_bundle_from_working_memory(metadata)

    assert metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["handles"] == {
        "target_id": "target:intent:target"
    }


def test_sync_target_hint_from_plan_todo_refreshes_bundle() -> None:
    """sync_target_hint_from_plan_todo refreshes bundle after WM mutation."""
    initial_bundle = build_conversation_context_bundle(
        conversation_id="conv-plan-sync",
        turn_id="turn-plan-sync",
        turn_sequence=0,
        messages=[{"role": "user", "content": "scan 10.0.0.5"}],
    )
    assert initial_bundle["runtime_state"]["active_target"] is None

    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: initial_bundle,
        "working_memory": {
            "stage": "tool_selection",
            "active": {"target_id": None},
            "referents": {},
        },
    }
    changed = sync_target_hint_from_plan_todo(
        metadata,
        todo_list=[],
        plan=["scan 10.0.0.5 for open ports"],
        current_goal="scan 10.0.0.5",
    )
    assert changed is True
    bundle_after = metadata[METADATA_CONTEXT_BUNDLE_KEY]
    active_target = bundle_after["runtime_state"]["active_target"]
    assert active_target is not None
    assert active_target.get("value") == "10.0.0.5"


def test_planner_wm_summary_is_bundle_derived() -> None:
    """build_working_memory_context_for_planner reads from the bundle, not WM.

    Builds a metadata dict with a bundle whose runtime_state carries a
    distinctive active target and an *intentionally stale* working
    memory. The returned summary must reflect the bundle's runtime
    state (current), not the stale WM dict (drift).
    """
    # Defer import to bypass the circular-import trap that also affects
    # other planner_service tests (agent.graph.builders pre-import).
    import agent.graph.builders  # noqa: F401
    from agent.graph.context.contracts import RuntimeStateSnapshot
    from agent.graph.subgraphs.tool_execution_runtime import planner_service

    bundle = build_conversation_context_bundle(
        conversation_id="conv-wm",
        turn_id="turn-wm",
        turn_sequence=0,
        messages=[{"role": "user", "content": "scan 5.5.5.5"}],
        runtime_state=RuntimeStateSnapshot(
            active_target={"value": "5.5.5.5", "kind": "ip"},
            current_goal={"text": "port scan"},
            current_decision=None,
            in_flight_tool=None,
            handles={"target_id": "target:intent:target"},
        ),
    )
    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
        "working_memory": {
            # Stale: should NOT drive the planner summary.
            "active": {"target_id": "target:old:stale"},
            "referents": {"old:stale": {"value": "STALE_TARGET_SHOULD_NOT_APPEAR"}},
        },
    }

    result = planner_service.build_working_memory_context_for_planner(
        metadata,
        max_summary_chars=1200,
    )

    summary = result["working_memory_summary"]
    assert "5.5.5.5" in summary
    assert "STALE_TARGET_SHOULD_NOT_APPEAR" not in summary
    # The exposed runtime-state view mirrors the bundle projection.
    assert result["working_memory"]["active_target"] == {
        "value": "5.5.5.5",
        "kind": "ip",
    }


def test_planner_wm_summary_empty_when_bundle_missing() -> None:
    """Absent bundle yields an empty summary rather than falling back to WM."""
    import agent.graph.builders  # noqa: F401
    from agent.graph.subgraphs.tool_execution_runtime import planner_service

    metadata: Dict[str, Any] = {
        "working_memory": {"stage": "tool_selection"},
    }
    result = planner_service.build_working_memory_context_for_planner(
        metadata,
        max_summary_chars=1200,
    )
    assert result == {
        "working_memory": {},
        "working_memory_summary": "",
        "referenced_prior_turns": "",
    }
