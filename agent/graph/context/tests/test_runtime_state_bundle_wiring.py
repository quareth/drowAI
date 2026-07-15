"""Tests for runtime-state / evidence-refs bundle refresh wiring.

These tests lock in the P1 fix that keeps
``metadata[context_bundle]["runtime_state"]`` and
``metadata[context_bundle]["evidence_refs"]`` aligned with the
canonical ``metadata["working_memory"]`` after every reducer mutation.
They exercise the helper module in isolation
(``agent.graph.context.runtime_state``); call-site wiring from nodes
and subgraphs is covered in their respective test modules.
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.runtime_state import (
    evidence_refs_from_working_memory,
    refresh_bundle_active_todo,
    refresh_bundle_from_working_memory,
    runtime_state_snapshot_from_working_memory,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _wm_with_active_target_and_tool() -> dict[str, Any]:
    """Return a realistic canonical WM with runtime-state fields populated."""
    return {
        "active": {"target_id": "target:intent:target"},
        "referents": {
            "intent:target": {"value": "10.0.0.1", "kind": "ip"}
        },
        "objective": {
            "text": "Enumerate services on 10.0.0.1",
            "status": "in_progress",
        },
        "active_decision": {
            "source": "post_tool_reasoning",
            "status": "active",
            "next_action": "call_tool",
        },
        "tool_state": {
            "selected_tool": "nmap_scan",
            "status": "approved",
        },
        "tool_runs": [
            {
                "id": "tool_run:run-1",
                "tool_id": "nmap_scan",
                "summary": "Ports 22 and 80 open",
            }
        ],
    }


def _empty_bundle_metadata() -> dict[str, Any]:
    """Return metadata with a freshly-seeded bundle (empty runtime state)."""
    metadata: dict[str, Any] = {}
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id="conv-p1",
        turn_id="turn-p1",
        turn_sequence=0,
        messages=[{"role": "user", "content": "hello"}],
    )
    return metadata


# ---------------------------------------------------------------------------
# runtime_state_snapshot_from_working_memory
# ---------------------------------------------------------------------------


def test_runtime_state_snapshot_maps_canonical_wm_fields() -> None:
    wm = _wm_with_active_target_and_tool()

    snapshot = runtime_state_snapshot_from_working_memory(wm)

    assert snapshot["active_target"] == {
        "target_id": "target:intent:target",
        "value": "10.0.0.1",
        "kind": "ip",
    }
    assert snapshot["current_goal"] == {
        "text": "Enumerate services on 10.0.0.1",
        "status": "in_progress",
    }
    assert snapshot["current_decision"] == wm["active_decision"]
    assert snapshot["in_flight_tool"] == {
        "selected_tool": "nmap_scan",
        "status": "approved",
    }
    # Handles are projected from ``wm["active"]`` — target_id is present,
    # subject/collection are not set in the fixture so they are omitted.
    assert snapshot["handles"] == {"target_id": "target:intent:target"}


def test_runtime_state_snapshot_empty_for_none_input() -> None:
    snapshot = runtime_state_snapshot_from_working_memory(None)

    assert snapshot == {
        "active_target": None,
        "current_goal": None,
        "current_decision": None,
        "in_flight_tool": None,
        "handles": {},
        "active_todo": None,
    }


def test_runtime_state_snapshot_empty_for_default_objective() -> None:
    wm = {
        "active": {"target_id": None},
        "referents": {},
        "objective": {"text": "unknown", "status": "unknown"},
        "tool_state": {"selected_tool": None, "status": "none"},
    }

    snapshot = runtime_state_snapshot_from_working_memory(wm)

    assert snapshot["active_target"] is None
    assert snapshot["current_goal"] is None
    assert snapshot["in_flight_tool"] is None


def test_runtime_state_snapshot_drops_unresolved_target() -> None:
    wm = {
        "active": {"target_id": "target:intent:target"},
        # No matching referent — snapshot must not invent a value.
        "referents": {},
        "objective": {"text": "unknown", "status": "unknown"},
    }

    assert runtime_state_snapshot_from_working_memory(wm)["active_target"] is None


def test_runtime_state_snapshot_requires_selected_tool_for_in_flight() -> None:
    wm = {
        "active": {"target_id": None},
        "referents": {},
        "objective": {"text": "unknown", "status": "unknown"},
        "tool_state": {"selected_tool": None, "status": "approved"},
    }

    assert runtime_state_snapshot_from_working_memory(wm)["in_flight_tool"] is None


# ---------------------------------------------------------------------------
# evidence_refs_from_working_memory
# ---------------------------------------------------------------------------


def test_evidence_refs_extracts_tool_runs_with_truncated_summaries() -> None:
    # Pull the module-level cap directly so the assertion stays in sync
    # if the budget is rescaled (it was 4x'd on 2026-04-14).
    from agent.graph.context import runtime_state as _runtime_state

    cap = _runtime_state._EVIDENCE_SUMMARY_MAX_CHARS
    oversize_summary = "x" * (cap + 100)
    wm = {
        "tool_runs": [
            {"id": f"tool_run:r{i}", "tool_id": "nmap_scan", "summary": oversize_summary}
            for i in range(12)
        ]
    }

    refs = evidence_refs_from_working_memory(wm)

    assert len(refs) == 10
    # Newest-last, matches the last 10 of the input.
    assert refs[0]["evidence_id"] == "tool_run:r2"
    assert refs[-1]["evidence_id"] == "tool_run:r11"
    for ref in refs:
        assert ref["kind"] == "tool_run"
        assert ref["source"] == "nmap_scan"
        assert len(ref["summary"]) == cap
        assert ref["summary"] == "x" * cap


def test_evidence_refs_empty_for_missing_tool_runs() -> None:
    assert evidence_refs_from_working_memory(None) == []
    assert evidence_refs_from_working_memory({}) == []
    assert evidence_refs_from_working_memory({"tool_runs": []}) == []


def test_evidence_refs_uses_execution_id_when_id_absent() -> None:
    wm = {
        "tool_runs": [
            {"execution_id": "run-99", "tool_id": "nmap_scan", "summary": "short"}
        ]
    }

    refs = evidence_refs_from_working_memory(wm)

    assert len(refs) == 1
    assert refs[0]["evidence_id"] == "run-99"


# ---------------------------------------------------------------------------
# refresh_bundle_from_working_memory
# ---------------------------------------------------------------------------


def test_refresh_bundle_updates_runtime_state_and_evidence_in_place() -> None:
    metadata = _empty_bundle_metadata()
    bundle_before = metadata[METADATA_CONTEXT_BUNDLE_KEY]

    assert bundle_before["runtime_state"]["active_target"] is None
    assert bundle_before["evidence_refs"] == []

    metadata["working_memory"] = _wm_with_active_target_and_tool()
    refresh_bundle_from_working_memory(metadata)

    bundle_after = metadata[METADATA_CONTEXT_BUNDLE_KEY]
    # Same object is mutated in place.
    assert bundle_after is bundle_before
    assert bundle_after["runtime_state"]["active_target"] == {
        "target_id": "target:intent:target",
        "value": "10.0.0.1",
        "kind": "ip",
    }
    assert bundle_after["runtime_state"]["in_flight_tool"] == {
        "selected_tool": "nmap_scan",
        "status": "approved",
    }
    assert len(bundle_after["evidence_refs"]) == 1
    assert bundle_after["evidence_refs"][0]["evidence_id"] == "tool_run:run-1"


def test_refresh_bundle_is_noop_when_bundle_missing() -> None:
    metadata: dict[str, Any] = {
        "working_memory": _wm_with_active_target_and_tool(),
    }

    # Must not raise, must not inject a bundle key.
    refresh_bundle_from_working_memory(metadata)

    assert METADATA_CONTEXT_BUNDLE_KEY not in metadata


def test_refresh_bundle_is_idempotent() -> None:
    metadata = _empty_bundle_metadata()
    metadata["working_memory"] = _wm_with_active_target_and_tool()

    refresh_bundle_from_working_memory(metadata)
    snapshot_once = dict(metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"])
    refs_once = [dict(r) for r in metadata[METADATA_CONTEXT_BUNDLE_KEY]["evidence_refs"]]

    refresh_bundle_from_working_memory(metadata)
    snapshot_twice = dict(metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"])
    refs_twice = [dict(r) for r in metadata[METADATA_CONTEXT_BUNDLE_KEY]["evidence_refs"]]

    assert snapshot_once == snapshot_twice
    assert refs_once == refs_twice


def test_refresh_bundle_clears_runtime_when_working_memory_missing() -> None:
    metadata = _empty_bundle_metadata()
    metadata["working_memory"] = _wm_with_active_target_and_tool()

    refresh_bundle_from_working_memory(metadata)
    assert metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_target"] is not None

    # Remove working memory and refresh again: snapshot resets to empty.
    del metadata["working_memory"]
    refresh_bundle_from_working_memory(metadata)

    bundle = metadata[METADATA_CONTEXT_BUNDLE_KEY]
    assert bundle["runtime_state"]["active_target"] is None
    assert bundle["runtime_state"]["in_flight_tool"] is None
    assert bundle["evidence_refs"] == []


# ---------------------------------------------------------------------------
# refresh_bundle_active_todo
# ---------------------------------------------------------------------------


def test_refresh_bundle_active_todo_writes_in_progress_descriptor() -> None:
    from agent.graph.state import TodoItem, TodoStatus

    metadata = _empty_bundle_metadata()
    todos = [
        TodoItem(description="Ping 10.0.0.5"),
        TodoItem(description="Scan open ports", status=TodoStatus.IN_PROGRESS),
        TodoItem(description="Grab banners"),
    ]

    refresh_bundle_active_todo(metadata, todos)

    active = metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_todo"]
    assert active == {"index": 1, "description": "Scan open ports"}


def test_refresh_bundle_active_todo_clears_slot_when_no_in_progress() -> None:
    from agent.graph.state import TodoItem, TodoStatus

    metadata = _empty_bundle_metadata()
    # Seed an active_todo first.
    refresh_bundle_active_todo(
        metadata,
        [TodoItem(description="Step 1", status=TodoStatus.IN_PROGRESS)],
    )
    assert metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_todo"] is not None

    # All todos now terminal: slot resets to None (omitted from projection).
    metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_todo"] = None
    refresh_bundle_active_todo(
        metadata,
        [TodoItem(description="Step 1", status=TodoStatus.COMPLETE_POSITIVE)],
    )
    assert metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_todo"] is None


def test_refresh_bundle_active_todo_is_noop_when_bundle_missing() -> None:
    from agent.graph.state import TodoItem, TodoStatus

    metadata: dict[str, Any] = {}
    refresh_bundle_active_todo(
        metadata,
        [TodoItem(description="Step 1", status=TodoStatus.IN_PROGRESS)],
    )
    assert METADATA_CONTEXT_BUNDLE_KEY not in metadata


def test_refresh_bundle_from_working_memory_preserves_active_todo() -> None:
    """WM refresh must not clobber active_todo; that slot is todo-sourced."""
    from agent.graph.state import TodoItem, TodoStatus

    metadata = _empty_bundle_metadata()
    metadata["working_memory"] = _wm_with_active_target_and_tool()

    refresh_bundle_active_todo(
        metadata,
        [TodoItem(description="Active step", status=TodoStatus.IN_PROGRESS)],
    )
    assert (
        metadata[METADATA_CONTEXT_BUNDLE_KEY]["runtime_state"]["active_todo"]
        is not None
    )

    refresh_bundle_from_working_memory(metadata)

    bundle = metadata[METADATA_CONTEXT_BUNDLE_KEY]
    # WM-sourced slots refreshed from working memory...
    assert bundle["runtime_state"]["active_target"] is not None
    # ...but active_todo survives because WM refresh is not its authority.
    assert bundle["runtime_state"]["active_todo"] == {
        "index": 0,
        "description": "Active step",
    }
