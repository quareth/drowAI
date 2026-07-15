"""Unit tests for the shared role projections and prompt-section serializer.

Exercises ``agent.graph.context.projections``. These tests lock in the
Phase 2 guarantees that matter for prompt continuity and provider-side
caching:

- Every projection is derived from the bundle only — mutating the
  bundle and reprojecting tracks the change (category selector test).
- Classifier projection carries recent transcript plus minimal runtime
  state (no evidence refs).
- Planner projection carries active target, recent transcript, and
  evidence refs.
- Projection output ordering is deterministic across equivalent inputs
  (dict equality for projections; list equality for serialized
  sections).
- The shared serializer emits sections in the declared stable order for
  every projection.
- No projection or serializer call truncates transcript message
  content — verified with deliberately long turn strings.
"""

from __future__ import annotations

import copy
from typing import Any

from agent.graph.context.builder import (
    build_conversation_context_bundle,
    update_prior_turn_references,
)
from agent.graph.context.contracts import (
    CLASSIFIER_TRANSCRIPT_WINDOW_KEY,
    EvidenceRef,
    RuntimeStateSnapshot,
)
from agent.graph.context.projections import (
    ROLE_ARTICULATION,
    ROLE_CATEGORY_SELECTOR,
    ROLE_INTENT_CLASSIFIER,
    ROLE_PLANNER,
    SECTION_EVIDENCE_REFS,
    SECTION_REFERENCED_PRIOR_TURNS,
    SECTION_RECENT_TRANSCRIPT,
    SECTION_RUNTIME_STATE,
    project_for_articulation,
    project_for_category_selector,
    project_for_intent_classifier,
    project_for_planner,
    serialize_projection_to_prompt_sections,
)
from agent.graph.context.serialization import (
    serialize_projection_to_section_map,
)


# -- Fixture helpers. ---------------------------------------------------


LONG_USER_TAIL = "x" * 1024
LONG_ASSISTANT_TAIL = "y" * 1024


def _turn_messages(index: int) -> list[dict[str, Any]]:
    """Build a 2-message turn for tests: user + assistant with long content."""
    return [
        {"role": "user", "content": f"user question {index} {LONG_USER_TAIL}"},
        {
            "role": "assistant",
            "content": f"assistant answer {index} {LONG_ASSISTANT_TAIL}",
        },
    ]


def _build_messages(turn_count: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for i in range(turn_count):
        messages.extend(_turn_messages(i))
    return messages


def _runtime_state(
    *,
    active_target: dict[str, Any] | None = None,
    current_goal: dict[str, Any] | None = None,
    current_decision: dict[str, Any] | None = None,
    in_flight_tool: dict[str, Any] | None = None,
    handles: dict[str, Any] | None = None,
    active_todo: dict[str, Any] | None = None,
) -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        active_target=active_target,
        current_goal=current_goal,
        current_decision=current_decision,
        in_flight_tool=in_flight_tool,
        handles=handles or {},
        active_todo=active_todo,
    )


def _bundle(
    *,
    messages: list[dict[str, Any]] | None = None,
    runtime_state: RuntimeStateSnapshot | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
    conversation_id: str = "conv-1",
    turn_id: str = "turn-9",
    turn_sequence: int = 9,
):
    return build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        messages=list(messages) if messages is not None else _build_messages(3),
        runtime_state=runtime_state,
        evidence_refs=evidence_refs,
    )


# -- Classifier projection. ---------------------------------------------


def test_classifier_projection_includes_recent_transcript_and_minimal_runtime_state() -> None:
    active_target = {"host": "5.5.5.5"}
    current_goal = {"goal": "scan for open ports"}
    current_decision = {"action": "run nmap"}
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target=active_target,
            current_goal=current_goal,
            current_decision=current_decision,  # must NOT surface on classifier
            in_flight_tool={"name": "nmap"},  # must NOT surface on classifier
        ),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )

    projection = project_for_intent_classifier(bundle)

    assert projection["role"] == ROLE_INTENT_CLASSIFIER
    # Classifier-only window surfaces verbatim (identity, not copy).
    assert projection["transcript_window"] is bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY]
    # Minimal runtime state: active target + current goal, nothing else.
    assert projection["runtime_state"] == {
        "active_target": active_target,
        "current_goal": current_goal,
    }
    # Classifier does not consume evidence refs.
    assert "evidence_refs" not in projection
    # Turn identity is carried for diagnostics.
    assert projection["turn_identity"] == {
        "conversation_id": bundle["conversation_id"],
        "turn_id": bundle["turn_id"],
        "turn_sequence": bundle["turn_sequence"],
    }


def test_classifier_projection_omits_missing_runtime_slots() -> None:
    # No active target, no goal -> runtime_state is empty, not None-filled.
    bundle = _bundle(runtime_state=_runtime_state())

    projection = project_for_intent_classifier(bundle)

    assert projection["runtime_state"] == {}


def test_classifier_only_window_does_not_replace_other_role_transcript() -> None:
    """Compacted classifier context leaves the shared role window untouched."""
    bundle = _bundle(messages=_build_messages(3))
    shared_window = bundle["transcript_window"]
    classifier_window = copy.deepcopy(shared_window)
    classifier_window["turns"] = [
        {"role": "system", "content": "validated compacted summary"},
        {"role": "user", "content": "retained question"},
        {"role": "assistant", "content": "retained answer"},
    ]
    bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY] = classifier_window

    classifier = project_for_intent_classifier(bundle)
    planner = project_for_planner(bundle)
    category = project_for_category_selector(bundle)
    articulation = project_for_articulation(bundle)

    assert classifier["transcript_window"] is classifier_window
    assert planner["transcript_window"] is shared_window
    assert category["transcript_window"] is shared_window
    assert articulation["transcript_window"] is shared_window


# -- Planner projection. ------------------------------------------------


def test_planner_projection_includes_active_target_and_recent_transcript() -> None:
    active_target = {"host": "10.0.0.5"}
    refs = [
        EvidenceRef(
            evidence_id="f1", kind="finding", summary="port 80 open", source="nmap"
        ),
        EvidenceRef(
            evidence_id="a2",
            kind="artifact",
            summary="nmap scan report",
            source="nmap",
        ),
    ]
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target=active_target,
            current_goal={"goal": "enumerate service"},
            in_flight_tool={"name": "nikto"},
            handles={"session_id": "s-1"},
        ),
        evidence_refs=refs,
    )

    projection = project_for_planner(bundle)

    assert projection["role"] == ROLE_PLANNER
    assert projection["transcript_window"] is bundle["transcript_window"]
    assert projection["runtime_state"]["active_target"] == active_target
    assert "current_goal" in projection["runtime_state"]
    assert "in_flight_tool" in projection["runtime_state"]
    assert projection["runtime_state"]["handles"] == {"session_id": "s-1"}
    # Evidence refs surface verbatim in original order.
    assert projection["evidence_refs"] == refs


# -- Category selector: bundle-only derivation. -------------------------


def test_category_selector_projection_tracks_bundle_transcript_only() -> None:
    # Build bundle A, project it, then build a *different* bundle with
    # altered transcript messages and project it. The projection of B
    # must reflect bundle B's transcript — confirming the projection
    # draws from the bundle only and does not re-read or cache history
    # from some external source.
    bundle_a = _bundle(messages=_build_messages(turn_count=3))
    projection_a = project_for_category_selector(bundle_a)

    # New bundle with an obviously different transcript.
    altered_messages = _build_messages(turn_count=3)
    altered_messages[0]["content"] = "user question DISTINCT_MARKER"
    bundle_b = _bundle(messages=altered_messages)
    projection_b = project_for_category_selector(bundle_b)

    assert projection_a["transcript_window"]["turns"][0]["content"].startswith(
        "user question 0 "
    )
    assert (
        projection_b["transcript_window"]["turns"][0]["content"]
        == "user question DISTINCT_MARKER"
    )

    # And the role is correct.
    assert projection_b["role"] == ROLE_CATEGORY_SELECTOR


def test_non_classifier_roles_preserve_the_shared_ten_turn_projection() -> None:
    """Category, planner, and articulation retain the current bounded window."""
    bundle = _bundle(messages=_build_messages(turn_count=12))

    for project in (
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        projection = project(bundle)
        turns = projection["transcript_window"]["turns"]
        contents = [str(message.get("content") or "") for message in turns]

        assert projection["transcript_window"] is bundle["transcript_window"]
        assert projection["transcript_window"]["target_turn_count"] == 10
        assert projection["transcript_window"]["dropped_older_turn_count"] == 2
        assert all("question 0 " not in content for content in contents)
        assert all("question 1 " not in content for content in contents)
        for index in range(2, 12):
            assert any(f"user question {index} " in content for content in contents)
            assert any(f"assistant answer {index} " in content for content in contents)


def test_classifier_projection_uses_full_history_beyond_shared_ten_turn_window() -> None:
    """Classifier history is canonical and never falls back to the shared cap."""
    bundle = _bundle(messages=_build_messages(turn_count=12))

    classifier = project_for_intent_classifier(bundle)
    classifier_contents = [
        str(message.get("content") or "")
        for message in classifier["transcript_window"]["turns"]
    ]

    assert classifier["transcript_window"] is bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY]
    assert classifier["transcript_window"]["dropped_older_turn_count"] == 0
    for index in range(12):
        assert any(f"user question {index} " in content for content in classifier_contents)
        assert any(f"assistant answer {index} " in content for content in classifier_contents)


# -- Determinism of projection output. ----------------------------------


def test_projections_are_deterministic_for_equivalent_inputs() -> None:
    runtime_state = _runtime_state(
        active_target={"host": "1.2.3.4"},
        current_goal={"goal": "scan"},
        current_decision={"action": "run nmap"},
        in_flight_tool={"name": "nmap"},
        handles={"session_id": "s-1"},
    )
    refs = [
        EvidenceRef(
            evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
        )
    ]
    messages = _build_messages(turn_count=4)

    bundle_1 = _bundle(messages=messages, runtime_state=runtime_state, evidence_refs=refs)
    bundle_2 = _bundle(
        messages=copy.deepcopy(messages),
        runtime_state=copy.deepcopy(runtime_state),
        evidence_refs=copy.deepcopy(refs),
    )

    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        assert project(bundle_1) == project(bundle_2)


# -- Serializer: section order. -----------------------------------------


def test_serializer_emits_declared_section_order_for_every_projection() -> None:
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target={"host": "5.5.5.5"},
            current_goal={"goal": "enumerate"},
            current_decision={"action": "run nmap"},
        ),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )

    for project, expected_sections in [
        (
            project_for_intent_classifier,
            [SECTION_RECENT_TRANSCRIPT, SECTION_RUNTIME_STATE],
        ),
        (
            project_for_category_selector,
            [SECTION_RECENT_TRANSCRIPT, SECTION_RUNTIME_STATE],
        ),
        (
            project_for_planner,
            [SECTION_RECENT_TRANSCRIPT, SECTION_RUNTIME_STATE, SECTION_EVIDENCE_REFS],
        ),
        (
            project_for_articulation,
            [SECTION_RECENT_TRANSCRIPT, SECTION_RUNTIME_STATE, SECTION_EVIDENCE_REFS],
        ),
    ]:
        projection = project(bundle)
        sections = serialize_projection_to_prompt_sections(projection)
        assert [s["name"] for s in sections] == expected_sections


def test_serializer_omits_current_user_turn_section_always() -> None:
    # There is no separately-addressable ``current_user_turn`` section:
    # when a projection carries the in-flight turn, it rides inside
    # ``recent_transcript`` as the final ``<turn … latest=true>`` block.
    bundle = _bundle()
    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        sections = serialize_projection_to_prompt_sections(project(bundle))
        assert all(s["name"] != "current_user_turn" for s in sections)


def test_projection_without_current_turn_omits_latest_block() -> None:
    # Presence-based rendering: when the bundle's ``current_user_turn``
    # is ``None``, the projection carries ``None`` and the serializer
    # emits only prior turns -- no ``latest=true`` block appears.
    bundle = _bundle(
        messages=[
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ],
    )
    assert bundle["current_user_turn"] is None

    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        projection = project(bundle)
        assert projection["current_user_turn"] is None
        # Presence-based: no opt-in flag exists on the projection.
        assert "include_current_user_turn" not in projection
        sections = serialize_projection_to_prompt_sections(projection)
        transcript = next(
            s for s in sections if s["name"] == SECTION_RECENT_TRANSCRIPT
        )
        assert "latest=true" not in transcript["content"]


def test_projection_with_current_turn_appends_latest_block() -> None:
    # Presence-based rendering: when the bundle carries a populated
    # ``current_user_turn``, the projection surfaces it and the
    # serializer renders it as the final ``latest=true`` block -- no
    # opt-in flag manipulation needed.
    bundle = _bundle(
        messages=[
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ],
    )
    bundle["current_user_turn"] = {"role": "user", "content": "run those"}

    projection = project_for_planner(bundle)
    assert projection["current_user_turn"] == {
        "role": "user",
        "content": "run those",
    }

    sections = serialize_projection_to_prompt_sections(projection)
    transcript = next(
        s for s in sections if s["name"] == SECTION_RECENT_TRANSCRIPT
    )

    # The prior turn renders first, then the in-flight turn as the
    # final block tagged ``latest=true`` with an incremented index.
    assert transcript["content"].startswith(
        "<turn n=1 role=user>\nprior question\n</turn>"
    )
    assert transcript["content"].endswith(
        "<turn n=2 role=user latest=true>\nrun those\n</turn>"
    )
    # No separate ``current_user_turn`` section -- the in-flight turn
    # lives inside the recent-transcript block, not alongside it.
    assert all(s["name"] != "current_user_turn" for s in sections)


# -- Serializer: determinism (same projection -> same bytes). -----------


def test_serializer_output_is_deterministic_list_equality() -> None:
    bundle_1 = _bundle(
        runtime_state=_runtime_state(active_target={"host": "5.5.5.5"}),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )
    bundle_2 = _bundle(
        runtime_state=_runtime_state(active_target={"host": "5.5.5.5"}),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )

    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        s1 = serialize_projection_to_prompt_sections(project(bundle_1))
        s2 = serialize_projection_to_prompt_sections(project(bundle_2))
        assert s1 == s2


# -- Serializer: no truncation of turn content. -------------------------


def test_serializer_does_not_truncate_selected_turn_content() -> None:
    # Use deliberately long turn content; it must appear verbatim in the
    # serialized transcript section for every projection.
    bundle = _bundle(messages=_build_messages(turn_count=3))

    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        sections = serialize_projection_to_prompt_sections(project(bundle))
        transcript_blocks = [
            s for s in sections if s["name"] == SECTION_RECENT_TRANSCRIPT
        ]
        assert len(transcript_blocks) == 1
        content = transcript_blocks[0]["content"]
        # Every original long tail must survive verbatim.
        for i in range(3):
            assert f"user question {i} {LONG_USER_TAIL}" in content
            assert f"assistant answer {i} {LONG_ASSISTANT_TAIL}" in content


def test_serializer_transcript_section_uses_bounded_turn_blocks() -> None:
    bundle = _bundle(messages=_build_messages(turn_count=1))

    sections = serialize_projection_to_prompt_sections(
        project_for_intent_classifier(bundle)
    )
    transcript = next(
        s for s in sections if s["name"] == SECTION_RECENT_TRANSCRIPT
    )
    content = transcript["content"]

    # Each message sits inside a bounded ``<turn n=N role=R>…</turn>``
    # block -- the legacy inline ``role: content`` and the unbounded
    # ``User:\n…`` / ``Assistant:\n…`` header form are both gone.
    assert content.startswith("<turn n=1 role=user>\nuser question 0 ")
    # Assistant block follows the user block, separated by exactly one
    # blank line, and shares the same turn index (same user-turn group).
    assert "</turn>\n\n<turn n=1 role=assistant>\nassistant answer 0 " in content


def test_serializer_runtime_state_and_evidence_render_stable_lines() -> None:
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target={"host": "5.5.5.5"},
            current_goal={"goal": "enumerate"},
            in_flight_tool={"name": "nmap"},
            handles={"session_id": "s-1"},
        ),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            ),
            EvidenceRef(
                evidence_id="a2",
                kind="artifact",
                summary="scan report",
                source="nmap",
            ),
        ],
    )

    sections = serialize_projection_to_prompt_sections(project_for_planner(bundle))
    by_name = {s["name"]: s["content"] for s in sections}

    runtime_lines = by_name[SECTION_RUNTIME_STATE].split("\n")
    # Slot order matches the planner's declared slot tuple: active_target,
    # current_goal, in_flight_tool, handles.
    assert runtime_lines[0].startswith("active_target: ")
    assert runtime_lines[1].startswith("current_goal: ")
    assert runtime_lines[2].startswith("in_flight_tool: ")
    assert runtime_lines[3].startswith("handles: ")

    evidence_lines = by_name[SECTION_EVIDENCE_REFS].split("\n")
    assert evidence_lines[0] == "finding:f1 port 22 open"
    assert evidence_lines[1] == "artifact:a2 scan report"


def test_articulation_projection_is_finalizer_shape() -> None:
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target={"host": "5.5.5.5"},
            current_decision={"action": "run nmap"},
        ),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )

    projection = project_for_articulation(bundle)

    assert projection["role"] == ROLE_ARTICULATION
    assert projection["transcript_window"] is bundle["transcript_window"]
    assert "active_target" in projection["runtime_state"]
    assert "current_decision" in projection["runtime_state"]
    # Articulation sees evidence refs.
    assert len(projection["evidence_refs"]) == 1


# -- Shared section-extraction helper. ---------------------------------


def test_section_map_matches_serializer_content_per_section() -> None:
    """``serialize_projection_to_section_map`` mirrors the ordered serializer.

    Consumers that need named-section lookup (classifier, category
    selector, planner service, articulation, finalizer) should reuse this
    helper instead of open-coding the ``for section in ... if name == ...``
    loop. The invariant: for every section the serializer emits, the
    section-map returns byte-identical content under the same key.
    """
    bundle = _bundle(
        runtime_state=_runtime_state(
            active_target={"host": "5.5.5.5"},
            current_goal={"goal": "enumerate"},
            in_flight_tool={"name": "nmap"},
            handles={"session_id": "s-1"},
        ),
        evidence_refs=[
            EvidenceRef(
                evidence_id="f1", kind="finding", summary="port 22 open", source="nmap"
            )
        ],
    )

    for project in (
        project_for_intent_classifier,
        project_for_category_selector,
        project_for_planner,
        project_for_articulation,
    ):
        projection = project(bundle)
        ordered = serialize_projection_to_prompt_sections(projection)
        section_map = serialize_projection_to_section_map(projection)

        # Same set of section names in both views.
        assert set(section_map.keys()) == {s["name"] for s in ordered}
        # Same content under each name (no extra rendering).
        for section in ordered:
            assert section_map[section["name"]] == section["content"]


def test_section_map_returns_empty_dict_for_projection_without_sections() -> None:
    # A projection with no section-relevant keys (no transcript_window,
    # no runtime_state, no evidence_refs) must produce an empty mapping.
    section_map = serialize_projection_to_section_map({"role": "custom"})

    assert section_map == {}


def test_section_map_is_deterministic_for_equivalent_inputs() -> None:
    bundle_1 = _bundle(
        runtime_state=_runtime_state(active_target={"host": "5.5.5.5"}),
    )
    bundle_2 = _bundle(
        runtime_state=_runtime_state(active_target={"host": "5.5.5.5"}),
    )

    s1 = serialize_projection_to_section_map(project_for_intent_classifier(bundle_1))
    s2 = serialize_projection_to_section_map(project_for_intent_classifier(bundle_2))
    assert s1 == s2


# ---------------------------------------------------------------------------
# active_todo — current in-progress plan step surfacing to tool-selection
# layers (category selector, planner, articulation). Classifier intentionally
# excluded so routing does not see plan internals.
# ---------------------------------------------------------------------------


def test_active_todo_surfaces_on_category_selector_planner_and_articulation() -> None:
    active_todo = {"index": 1, "description": "Scan open ports on 10.0.0.5"}
    bundle = _bundle(runtime_state=_runtime_state(active_todo=active_todo))

    category = project_for_category_selector(bundle)
    planner = project_for_planner(bundle)
    articulation = project_for_articulation(bundle)

    assert category["runtime_state"].get("active_todo") == active_todo
    assert planner["runtime_state"].get("active_todo") == active_todo
    assert articulation["runtime_state"].get("active_todo") == active_todo


def test_active_todo_omitted_when_none_on_selection_projections() -> None:
    bundle = _bundle(runtime_state=_runtime_state(active_todo=None))

    for projection in (
        project_for_category_selector(bundle),
        project_for_planner(bundle),
        project_for_articulation(bundle),
    ):
        assert "active_todo" not in projection["runtime_state"]


def test_active_todo_not_leaked_to_intent_classifier_projection() -> None:
    active_todo = {"index": 0, "description": "Resolve example.com"}
    bundle = _bundle(runtime_state=_runtime_state(active_todo=active_todo))

    classifier = project_for_intent_classifier(bundle)

    # Routing stays plan-blind by design: classifier gets active_target +
    # current_goal only, never active_todo.
    assert "active_todo" not in classifier["runtime_state"]


def test_active_todo_renders_into_runtime_state_section() -> None:
    active_todo = {"index": 2, "description": "Grab SSH banner"}
    bundle = _bundle(runtime_state=_runtime_state(active_todo=active_todo))

    section_map = serialize_projection_to_section_map(
        project_for_category_selector(bundle)
    )

    runtime_text = section_map.get(SECTION_RUNTIME_STATE, "")
    assert "active_todo:" in runtime_text
    assert "Grab SSH banner" in runtime_text


# ---------------------------------------------------------------------------
# prior_turn_references — materialized canonical transcript context.
# ---------------------------------------------------------------------------


def test_bundle_defaults_prior_turn_references_to_empty_shape() -> None:
    bundle = _bundle()

    assert bundle["prior_turn_references"] == {
        "operation": "none",
        "status": "none",
        "materialized_turns": [],
        "unresolved_hints": [],
    }


def test_prior_turn_references_project_only_to_planner_and_articulation() -> None:
    bundle = _bundle()
    update_prior_turn_references(
        bundle,
        {
            "operation": "reference_resolution",
            "status": "ok",
            "materialized_turns": [
                {
                    "turn_number": 3,
                    "speaker": "assistant",
                    "message_id": 9,
                    "text": "Canonical prior assistant text.",
                    "matched_by": "anchor_text",
                    "classifier_confidence": 0.9,
                }
            ],
            "unresolved_hints": [],
        },
    )

    classifier = project_for_intent_classifier(bundle)
    category = project_for_category_selector(bundle)
    planner = project_for_planner(bundle)
    articulation = project_for_articulation(bundle)

    assert "prior_turn_references" not in classifier
    assert "prior_turn_references" not in category
    assert planner["prior_turn_references"] is bundle["prior_turn_references"]
    assert articulation["prior_turn_references"] is bundle["prior_turn_references"]


def test_referenced_prior_turns_section_renders_canonical_text_only() -> None:
    bundle = _bundle()
    update_prior_turn_references(
        bundle,
        {
            "operation": "reference_resolution",
            "status": "partial",
            "materialized_turns": [
                {
                    "turn_number": 7,
                    "speaker": "assistant",
                    "message_id": 99,
                    "text": "Use tcpdump to capture packets from that traffic.",
                    "matched_by": "rendered_turn",
                    "classifier_confidence": 0.91,
                }
            ],
            "unresolved_hints": [
                {
                    "status": "unresolved",
                    "anchor_text": "MODEL GENERATED ANCHOR",
                }
            ],
        },
    )

    section_map = serialize_projection_to_section_map(project_for_planner(bundle))
    referenced = section_map[SECTION_REFERENCED_PRIOR_TURNS]

    assert referenced.startswith("Referenced Prior Turns:")
    assert "<turn n=7 role=assistant>" in referenced
    assert "Use tcpdump to capture packets from that traffic." in referenced
    assert "</turn>" in referenced
    assert "MODEL GENERATED ANCHOR" not in referenced


def test_referenced_prior_turns_section_omitted_when_unmaterialized() -> None:
    bundle = _bundle()
    update_prior_turn_references(
        bundle,
        {
            "operation": "reference_resolution",
            "status": "unresolved",
            "materialized_turns": [],
            "unresolved_hints": [{"anchor_text": "not canonical"}],
        },
    )

    assert SECTION_REFERENCED_PRIOR_TURNS not in serialize_projection_to_section_map(
        project_for_planner(bundle)
    )
