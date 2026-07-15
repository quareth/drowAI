"""Phase 3 Task 3.3 regression tests — planner reads the shared bundle.

Locks in the contract that:

- The tool-execution request path (``request_context``) populates
  ``ToolExecutionRequest.history`` from
  ``metadata["context_bundle"]`` via the planner projection — not from
  legacy within-turn prose logs.
- After the Phase 5 authority cutover a missing bundle raises
  ``RuntimeError`` rather than silently falling back to
  stale metadata channels / ``request.history``.
- Cross-role invariant: classifier and planner observe the same
  recent-transcript set for the same turn when projecting one bundle.

Runner_control follow-up (Fix 1): the planner service no longer projects a
prompt-history list from the bundle. ``_resolve_planner_prompt_history``
has been removed and ``build_planner_context`` no longer emits a
``history`` key — downstream callsites read the classifier-derived
``intent_brief`` instead. The planner-service-side invariants
of Fix 1 are locked by
``agent/graph/subgraphs/tests/test_planner_service_brief_only.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Force full graph package init to sidestep the pre-existing circular import
# between agent.graph.subgraphs.tool_execution_runtime.planner_service and
# agent.graph.nodes / agent.graph.builders.  Importing builders here loads
# them before planner_service, so the lookup resolves cleanly.
import agent.graph.builders  # noqa: F401  # side-effect: break the import cycle
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.projections import (
    SECTION_RECENT_TRANSCRIPT,
    project_for_intent_classifier,
    project_for_planner,
    serialize_projection_to_prompt_sections,
)
from agent.graph.subgraphs.tool_execution_runtime.request_context import (
    _resolve_planner_history,
)
from agent.tool_runtime import ToolExecutionRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    """Return ``turn_count`` user/assistant turns with distinctive content."""
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


def _install_bundle(
    metadata: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=list(messages),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


def _make_request(
    message: str = "follow up on that target",
    history: List[Dict[str, Any]] | None = None,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message=message,
        history=list(history or []),
    )


def _extract_transcript_section(sections: List[Dict[str, str]]) -> str:
    for section in sections:
        if section.get("name") == SECTION_RECENT_TRANSCRIPT:
            return section.get("content", "")
    raise AssertionError("recent_transcript section missing from projection serialization")


# ---------------------------------------------------------------------------
# request_context — request.history population
# ---------------------------------------------------------------------------


def test_request_context_history_comes_from_bundle_not_legacy_metadata(caplog) -> None:
    """Primary path: bundle drives request.history; legacy key is not read."""
    history = _make_history(turn_count=3)
    bundle_messages = list(history)
    metadata: Dict[str, Any] = {
        # Distinctive content that should NOT end up in request.history.
        "history": [{"role": "user", "content": "LEGACY_METADATA_HISTORY_ENTRY"}],
    }
    _install_bundle(metadata, bundle_messages)

    with caplog.at_level(
        "WARNING",
        logger="agent.graph.subgraphs.tool_execution_runtime.request_context",
    ):
        resolved = _resolve_planner_history(metadata)

    assert resolved, "expected bundle to produce non-empty history"
    # Exactly the projected transcript window (verbatim) came back.
    expected_turns = project_for_planner(metadata[METADATA_CONTEXT_BUNDLE_KEY])[
        "transcript_window"
    ]["turns"]
    assert resolved == [dict(m) for m in expected_turns]

    # Legacy within-turn iteration log must not be read on the primary path.
    contents = [entry.get("content") for entry in resolved]
    assert "LEGACY_METADATA_HISTORY_ENTRY" not in contents

    # Primary path must not log a fallback warning.
    fallback_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if "falling back to legacy metadata[history]" in rec.getMessage()
    ]
    assert fallback_msgs == []


def test_request_context_raises_when_bundle_missing() -> None:
    """Phase 5 cutover: missing bundle raises ``RuntimeError``.

    The legacy fallback that read from ``metadata['history']`` has
    been removed. A missing bundle indicates upstream wiring failed
    to populate ``metadata[context_bundle]``.
    """
    legacy_history = [
        {"role": "user", "content": "legacy-user-turn"},
        {"role": "assistant", "content": "legacy-assistant-turn"},
    ]
    metadata: Dict[str, Any] = {"history": legacy_history}
    # Deliberately do not install a bundle.

    import pytest

    with pytest.raises(RuntimeError, match="context_bundle"):
        _resolve_planner_history(metadata)


# ---------------------------------------------------------------------------
# planner_service — prompt-history projection (removed by runner_control Fix 1)
#
# The runner_control follow-up cleanup removed
# ``planner_service._resolve_planner_prompt_history`` and dropped the
# ``history`` key from ``build_planner_context``'s output. The
# replacement brief-only invariants live in
# ``agent/graph/subgraphs/tests/test_planner_service_brief_only.py``.
# This section deliberately carries no planner-history-projection
# tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-consumer invariant (Phase 3 Acceptance Criteria)
# ---------------------------------------------------------------------------


def test_classifier_and_planner_observe_same_recent_transcript_set() -> None:
    """Classifier and planner must see the same recent-transcript window.

    For one bundle (one turn), projecting it for both the intent
    classifier and the planner must yield byte-identical
    ``recent_transcript`` section content.
    """
    history = _make_history(turn_count=4)
    metadata: Dict[str, Any] = {}
    bundle = _install_bundle(metadata, history)

    classifier_transcript = _extract_transcript_section(
        serialize_projection_to_prompt_sections(
            project_for_intent_classifier(bundle)
        )
    )
    planner_transcript = _extract_transcript_section(
        serialize_projection_to_prompt_sections(project_for_planner(bundle))
    )

    assert classifier_transcript == planner_transcript
    assert classifier_transcript, "transcript section unexpectedly empty"


def test_planner_projection_reflects_refreshed_runtime_state() -> None:
    """After ``refresh_bundle_from_working_memory``, the planner sees runtime state.

    Regression guard for the P1 wiring: seeding the bundle at turn
    start leaves ``runtime_state`` empty until working memory is
    reduced. Once the refresh helper runs, the planner projection must
    expose the new ``active_target`` slot so the planner LLM actually
    sees it.
    """
    from agent.graph.context.runtime_state import (
        refresh_bundle_from_working_memory,
    )

    metadata: Dict[str, Any] = {}
    bundle = _install_bundle(
        metadata,
        [
            {"role": "user", "content": "scan 10.0.0.1"},
            {"role": "assistant", "content": "Starting nmap"},
        ],
    )
    # Bundle starts with empty runtime state.
    assert bundle["runtime_state"]["active_target"] is None

    metadata["working_memory"] = {
        "active": {"target_id": "target:intent:target"},
        "referents": {
            "intent:target": {"value": "10.0.0.1", "kind": "ip"}
        },
        "objective": {
            "text": "Enumerate services on 10.0.0.1",
            "status": "in_progress",
        },
        "tool_state": {
            "selected_tool": "nmap_scan",
            "status": "approved",
        },
        "tool_runs": [],
    }

    refresh_bundle_from_working_memory(metadata)

    projection = project_for_planner(metadata[METADATA_CONTEXT_BUNDLE_KEY])
    runtime_state = projection["runtime_state"]

    assert runtime_state["active_target"] == {
        "target_id": "target:intent:target",
        "value": "10.0.0.1",
        "kind": "ip",
    }
    # Planner slots include in-flight tool; verify it flows through.
    assert runtime_state["in_flight_tool"] == {
        "selected_tool": "nmap_scan",
        "status": "approved",
    }


# NOTE: The end-to-end ``build_planner_context_uses_bundle_history``
# regression was removed by the runner_control follow-up cleanup (Fix 1). The
# planner context dict no longer carries a ``history`` key; the
# brief-only invariants that replace it live in
# ``agent/graph/subgraphs/tests/test_planner_service_brief_only.py``.
