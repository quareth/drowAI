"""Focused tests for working-memory schema defaults and deterministic caps."""

from __future__ import annotations

import json

from agent.graph.memory.working_memory import (
    AUTHORITY_ORDER,
    CAP_COLLECTIONS,
    CAP_ENTITIES,
    CAP_FACTS,
    CAP_OPEN_QUESTIONS,
    CAP_RECENT_TURNS,
    CAP_TOOL_RUNS,
    ID_KIND_COLLECTION,
    ID_KIND_ENTITY,
    ID_KIND_TARGET,
    ID_KIND_TOOL_RUN,
    append_collection,
    append_fact,
    append_open_question,
    append_recent_turn,
    append_tool_run,
    create_working_memory,
    default_provenance,
    ensure_typed_id,
    mint_typed_id,
    normalize_working_memory,
    unknown_item,
    update_active_handles,
    upsert_entity,
)


def test_default_schema_is_deterministic_and_serializable() -> None:
    """Default construction should be stable and deterministic."""
    wm_a = create_working_memory()
    wm_b = create_working_memory()

    assert wm_a == wm_b
    assert wm_a["schema"] == "drowai.working_memory.v1"
    assert wm_a["v"] == 1
    assert wm_a["authority"]["order"] == list(AUTHORITY_ORDER)
    assert wm_a["authority"]["llm_proposals_authoritative"] is False
    assert "ids" in wm_a
    assert "stage" in wm_a
    assert "objective" in wm_a
    assert "active" in wm_a
    assert "required_inputs" in wm_a
    assert "validation" in wm_a
    assert "constraints" in wm_a
    assert "recent_turns" in wm_a
    assert "tool_state" in wm_a
    assert "active_decision" in wm_a
    assert wm_a["active_decision"] is None

    serialized_a = json.dumps(wm_a, sort_keys=True, separators=(",", ":"))
    serialized_b = json.dumps(wm_b, sort_keys=True, separators=(",", ":"))
    assert serialized_a == serialized_b


def test_deterministic_caps_and_eviction() -> None:
    """All bounded collections should evict deterministically."""
    memory = create_working_memory()

    for idx in range(CAP_RECENT_TURNS + 2):
        memory = append_recent_turn(memory, {"role": "user", "turn_sequence": idx})
    assert len(memory["recent_turns"]) == CAP_RECENT_TURNS
    assert [item["turn_sequence"] for item in memory["recent_turns"]] == list(
        range(2, CAP_RECENT_TURNS + 2)
    )

    for idx in range(CAP_FACTS + 5):
        memory = append_fact(memory, {"id": f"fact-{idx}"})
    assert len(memory["facts"]) == CAP_FACTS
    assert memory["facts"][0]["id"] == "fact-5"
    assert memory["facts"][-1]["id"] == f"fact-{CAP_FACTS + 4}"
    assert memory["facts"][0]["status"] == "unknown"
    assert memory["facts"][0]["provenance"]["authority"] == "derived"

    for idx in range(CAP_ENTITIES + 5):
        memory = upsert_entity(memory, f"entity-{idx}", {"name": f"Entity {idx}"})
    assert len(memory["entities"]) == CAP_ENTITIES
    assert "entity:entity-0" not in memory["entities"]
    assert "entity:entity-4" not in memory["entities"]
    assert "entity:entity-5" in memory["entities"]
    assert f"entity:entity-{CAP_ENTITIES + 4}" in memory["entities"]
    assert memory["entities"]["entity:entity-5"]["status"] == "unknown"
    assert memory["entities"]["entity:entity-5"]["provenance"]["authority"] == "derived"

    for idx in range(CAP_TOOL_RUNS + 2):
        memory = append_tool_run(memory, {"id": f"tool-run-{idx}"})
    assert len(memory["tool_runs"]) == CAP_TOOL_RUNS
    assert memory["tool_runs"][0]["id"] == "tool_run:tool-run-2"
    assert memory["tool_runs"][-1]["id"] == f"tool_run:tool-run-{CAP_TOOL_RUNS + 1}"
    assert memory["tool_runs"][0]["provenance"]["authority"] == "derived"

    for idx in range(CAP_COLLECTIONS + 3):
        memory = append_collection(memory, {"id": f"collection-{idx}"})
    assert len(memory["collections"]) == CAP_COLLECTIONS
    assert memory["collections"][0]["id"] == "collection:collection-3"
    assert memory["collections"][-1]["id"] == f"collection:collection-{CAP_COLLECTIONS + 2}"
    assert memory["collections"][0]["provenance"]["authority"] == "derived"

    for idx in range(CAP_OPEN_QUESTIONS + 2):
        memory = append_open_question(memory, {"id": f"question-{idx}"})
    assert len(memory["open_questions"]) == CAP_OPEN_QUESTIONS
    assert memory["open_questions"][0]["id"] == "question-2"
    assert memory["open_questions"][-1]["id"] == f"question-{CAP_OPEN_QUESTIONS + 1}"
    assert memory["open_questions"][0]["provenance"]["authority"] == "derived"


def test_active_required_inputs_validation_defaults_are_safe() -> None:
    """Normalization must always provide safe defaults for required guardrail fields."""
    memory = normalize_working_memory(
        {
            "active": None,
            "required_inputs": None,
            "validation": None,
            "entities": None,
            "recent_turns": None,
            "facts": None,
            "tool_runs": None,
            "collections": None,
            "open_questions": None,
        }
    )

    assert memory["active"] == {"target_id": None, "subject_id": None, "collection_id": None}
    assert memory["required_inputs"] == []
    assert memory["validation"]["is_ready"] is True
    assert isinstance(memory["validation"]["missing"], list)
    assert isinstance(memory["validation"]["errors"], list)
    assert memory["entities"] == {}


def test_unknown_and_provenance_contract_are_explicit() -> None:
    """Unknown values and precedence metadata should be explicit and deterministic."""
    unknown = unknown_item({"detail": "unresolved referent"})
    assert unknown["status"] == "unknown"
    assert unknown["provenance"] == {"authority": "derived", "source": "unknown"}

    preserved = normalize_working_memory(
        {
            "facts": [
                {
                    "id": "fact-1",
                    "status": "confirmed",
                    "provenance": default_provenance(authority="tool", source="tool:run-123"),
                }
            ],
            "objective": {
                "text": "Collect host evidence",
                "status": "known",
                "source": "planner",
                "provenance": default_provenance(authority="user", source="user:turn-7"),
            },
        }
    )
    assert preserved["facts"][0]["status"] == "confirmed"
    assert preserved["facts"][0]["provenance"]["authority"] == "tool"
    assert preserved["objective"]["status"] == "known"
    assert preserved["objective"]["provenance"]["authority"] == "user"


def test_active_handles_are_typed_and_never_dangling() -> None:
    """Active handles should be typed and must resolve to known memory IDs."""
    memory = create_working_memory()
    memory = upsert_entity(memory, "host-1", {"kind": "host"})
    memory = append_tool_run(memory, {"id": "run-1", "name": "nmap"})
    memory = append_collection(memory, {"id": "col-1", "count": 3})
    memory["referents"]["primary"] = {"value": "127.0.0.1"}

    updated = update_active_handles(
        memory,
        target_id="primary",
        subject_id="host-1",
        collection_id="col-1",
    )
    assert updated["active"]["target_id"] == "target:primary"
    assert updated["active"]["subject_id"] == "entity:host-1"
    assert updated["active"]["collection_id"] == "collection:col-1"

    cleared = update_active_handles(
        updated,
        target_id="missing-target",
        subject_id="missing-subject",
        collection_id="missing-collection",
    )
    assert cleared["active"] == {"target_id": None, "subject_id": None, "collection_id": None}


def test_typed_id_helpers_are_deterministic() -> None:
    """ID helper behavior should be stable and unambiguous."""
    assert ensure_typed_id(ID_KIND_ENTITY, "alpha") == "entity:alpha"
    assert ensure_typed_id(ID_KIND_TOOL_RUN, "tool_run:abc") == "tool_run:abc"
    assert ensure_typed_id(ID_KIND_COLLECTION, "  c1  ") == "collection:c1"
    assert ensure_typed_id(ID_KIND_TARGET, "") is None
    assert mint_typed_id(ID_KIND_ENTITY, "entity-1") == "entity:entity-1"


def test_validation_fail_closed_for_tool_execution_when_context_missing() -> None:
    """Tool execution stage must fail closed when required context is absent."""
    memory = normalize_working_memory(
        {
            "stage": "tool_execution",
            "active": {"target_id": None, "subject_id": None, "collection_id": None},
            "tool_state": {"selected_tool": None, "tool_params": {}, "status": "none"},
        }
    )
    missing_codes = {item["code"] for item in memory["required_inputs"]}
    assert memory["validation"]["is_ready"] is False
    assert missing_codes == {
        "target_handle_required",
        "selected_tool_required",
        "tool_params_required",
    }
    assert memory["validation"]["missing"] == memory["required_inputs"]


def test_validation_ready_in_approval_stage_when_requirements_present() -> None:
    """Approval stage should be ready only when target/tool/params/approval are all valid."""
    memory = create_working_memory()
    memory["referents"]["primary"] = {"value": "127.0.0.1"}
    memory = update_active_handles(memory, target_id="primary")
    ready = normalize_working_memory(
        {
            **memory,
            "stage": "approval",
            "tool_state": {
                "selected_tool": "nmap_scan",
                "tool_params": {"target": "127.0.0.1"},
                "status": "approved",
            },
        }
    )
    assert ready["required_inputs"] == []
    assert ready["validation"]["missing"] == []
    assert ready["validation"]["errors"] == []
    assert ready["validation"]["is_ready"] is True
