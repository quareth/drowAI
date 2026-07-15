"""Working memory schema contract and deterministic cap enforcement helpers.

This module defines the canonical short-term working-memory payload for graph
execution. It provides deterministic constructors and pure mutation helpers that
enforce explicit caps/eviction rules so memory remains bounded and predictable.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Optional, TypedDict

from .findings import CAP_AVAILABLE_FINDINGS, normalize_available_findings
from .normalization import (
    normalize_active_decision as _normalize_active_decision_impl,
    normalize_objective as _normalize_objective_impl,
    normalize_stored_item as _normalize_stored_item_impl,
)

SCHEMA_ID = "drowai.working_memory.v1"
SCHEMA_VERSION = 1

# Deterministic authority order for conflict resolution.
AUTHORITY_ORDER = ("policy", "user", "tool", "derived", "llm_proposal")

# Deterministic cap policy:
# - Sequences: append-order FIFO eviction (drop oldest, keep newest max N).
# - Mappings: insertion-order FIFO eviction by key (drop oldest inserted keys).
CAP_RECENT_TURNS = 4
CAP_FACTS = 50
CAP_COVERAGE = 50
CAP_REFERENTS = 50
CAP_ENTITIES = 100
CAP_TOOL_RUNS = 10
CAP_COLLECTIONS = 20
CAP_OPEN_QUESTIONS = 10
CAP_ANALYSIS_NOTES = 20
ACTIVE_DECISION_STATUSES = ("active", "resolved", "superseded")
SEQUENCE_CAPS: dict[str, int] = {
    "recent_turns": CAP_RECENT_TURNS,
    "facts": CAP_FACTS,
    "coverage": CAP_COVERAGE,
    "tool_runs": CAP_TOOL_RUNS,
    "collections": CAP_COLLECTIONS,
    "open_questions": CAP_OPEN_QUESTIONS,
    "analysis_notes": CAP_ANALYSIS_NOTES,
    "available_findings": CAP_AVAILABLE_FINDINGS,
}
MAPPING_CAPS: dict[str, int] = {
    "referents": CAP_REFERENTS,
    "entities": CAP_ENTITIES,
}

ID_KIND_ENTITY = "entity"
ID_KIND_TOOL_RUN = "tool_run"
ID_KIND_COLLECTION = "collection"
ID_KIND_TARGET = "target"
ID_KINDS = (ID_KIND_ENTITY, ID_KIND_TOOL_RUN, ID_KIND_COLLECTION, ID_KIND_TARGET)

_UNSET = object()
TOOL_PATH_STAGES = {"tool_selection", "tool_parameterization", "tool_execution", "approval"}
TARGET_REQUIRED_STAGES = {"tool_parameterization", "tool_execution", "approval"}
TOOL_REQUIRED_STAGES = {"tool_parameterization", "tool_execution", "approval"}
TOOL_PARAMS_REQUIRED_STAGES = {"tool_execution", "approval"}
APPROVAL_REQUIRED_STAGES = {"approval"}


class RequestContractSlice(TypedDict):
    """Subset of request contract fields exposed inside intent brief."""

    question_type: Optional[str]
    answer_style: Optional[str]
    terminal_when: Optional[str]


class WorkingMemoryIntentBrief(TypedDict, total=False):
    """Static-typing contract for classifier-derived brief in working memory."""

    resolved_user_intent: Optional[str]
    overall_goal: Optional[str]
    continuation_mode: str
    resolved_step_title: Optional[str]
    resolved_step_detail: Optional[str]
    next_operational_goal: Optional[str]
    success_condition: Optional[str]
    execution_readiness: str
    blocking_reason: Optional[str]
    resolved_target: Optional[str]
    target_status: str
    target_source: str
    explicit_constraints: list[str]
    suggested_category_focus: list[str]
    retrieval_hints: list[str]
    relevant_memory_fragments: list[str]
    request_contract: RequestContractSlice


def ensure_typed_id(kind: str, value: str | None) -> str | None:
    """Return a deterministically typed ID value (`<kind>:<value>`)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if kind not in ID_KINDS:
        kind = ID_KIND_ENTITY
    prefix = f"{kind}:"
    if text.startswith(prefix):
        suffix = text[len(prefix) :].strip()
        return f"{prefix}{suffix}" if suffix else None
    return f"{prefix}{text}"


def mint_typed_id(kind: str, raw_value: str) -> str:
    """Mint a stable typed ID from explicit deterministic input."""
    typed = ensure_typed_id(kind, raw_value)
    if typed is None:
        return f"{kind}:unknown"
    return typed


def _known_ids(memory: Mapping[str, Any]) -> set[str]:
    """Collect known IDs in working memory for active-handle validation."""
    known: set[str] = set()
    entities = memory.get("entities", {})
    if isinstance(entities, Mapping):
        for key in entities:
            typed = ensure_typed_id(ID_KIND_ENTITY, str(key))
            if typed:
                known.add(typed)
    for item in _as_list(memory.get("tool_runs")):
        if isinstance(item, Mapping):
            typed = ensure_typed_id(ID_KIND_TOOL_RUN, item.get("id"))
            if typed:
                known.add(typed)
    for item in _as_list(memory.get("collections")):
        if isinstance(item, Mapping):
            typed = ensure_typed_id(ID_KIND_COLLECTION, item.get("id"))
            if typed:
                known.add(typed)
    for key in _as_mapping(memory.get("referents")):
        typed = ensure_typed_id(ID_KIND_TARGET, str(key))
        if typed:
            known.add(typed)
    return known


def _normalize_active(memory: Mapping[str, Any], active: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize active handles and clear dangling references deterministically."""
    handles = dict(active) if isinstance(active, Mapping) else {}
    target_id = ensure_typed_id(ID_KIND_TARGET, handles.get("target_id"))
    subject_id = handles.get("subject_id")
    collection_id = handles.get("collection_id")

    known = _known_ids(memory)

    normalized_subject = None
    for kind in (ID_KIND_ENTITY, ID_KIND_TOOL_RUN, ID_KIND_COLLECTION):
        candidate = ensure_typed_id(kind, subject_id)
        if candidate and candidate in known:
            normalized_subject = candidate
            break

    normalized_collection = ensure_typed_id(ID_KIND_COLLECTION, collection_id)
    if normalized_collection not in known:
        normalized_collection = None

    # target_id stays nullable for no-target workflows; if present it must resolve.
    if target_id is not None and target_id not in known:
        target_id = None

    return {
        "target_id": target_id,
        "subject_id": normalized_subject,
        "collection_id": normalized_collection,
    }


def update_active_handles(
    memory: Mapping[str, Any],
    *,
    target_id: str | None | object = _UNSET,
    subject_id: str | None | object = _UNSET,
    collection_id: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Deterministically set/clear/retain active handles.

    Rules:
    - `_UNSET` retains the current value.
    - `None` clears the value.
    - string values are typed and must resolve to known IDs or are cleared.
    """
    updated = normalize_working_memory(memory)
    active = dict(updated.get("active", {}))
    if target_id is not _UNSET:
        active["target_id"] = target_id
    if subject_id is not _UNSET:
        active["subject_id"] = subject_id
    if collection_id is not _UNSET:
        active["collection_id"] = collection_id
    updated["active"] = _normalize_active(updated, active)
    updated["required_inputs"] = compute_required_inputs(updated)
    updated["validation"] = compute_validation(updated)
    return updated


def compute_required_inputs(memory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compute stage-aware deterministic requirements for the next step."""
    stage = str(memory.get("stage", "chat"))
    required: list[dict[str, Any]] = []
    active = _as_mapping(memory.get("active"))
    tool_state = _as_mapping(memory.get("tool_state"))
    target_id = active.get("target_id")
    selected_tool = tool_state.get("selected_tool")
    tool_params = _as_mapping(tool_state.get("tool_params"))
    tool_status = str(tool_state.get("status", "none"))

    if stage in TARGET_REQUIRED_STAGES and target_id is None:
        required.append(
            {
                "code": "target_handle_required",
                "stage": stage,
                "message": "A target handle is required before continuing tool-path execution.",
            }
        )

    if stage in TOOL_REQUIRED_STAGES and not selected_tool:
        required.append(
            {
                "code": "selected_tool_required",
                "stage": stage,
                "message": "A selected tool is required before parameterization/execution.",
            }
        )

    if stage in TOOL_PARAMS_REQUIRED_STAGES and not tool_params:
        required.append(
            {
                "code": "tool_params_required",
                "stage": stage,
                "message": "Tool parameters are required before execution.",
            }
        )

    if stage in APPROVAL_REQUIRED_STAGES and tool_status != "approved":
        required.append(
            {
                "code": "approval_required",
                "stage": stage,
                "message": "Approval is required before execution in approval stage.",
            }
        )

    return required


def compute_validation(memory: Mapping[str, Any]) -> dict[str, Any]:
    """Compute deterministic validation guardrails from working-memory state."""
    missing = compute_required_inputs(memory)
    errors: list[dict[str, Any]] = []

    active = _as_mapping(memory.get("active"))
    tool_state = _as_mapping(memory.get("tool_state"))
    if active.get("target_id") is not None and ensure_typed_id(ID_KIND_TARGET, active.get("target_id")) is None:
        errors.append(
            {
                "code": "invalid_target_handle",
                "message": "active.target_id must be a typed target handle or null.",
            }
        )

    selected_tool = tool_state.get("selected_tool")
    if selected_tool is not None and not isinstance(selected_tool, str):
        errors.append(
            {
                "code": "invalid_selected_tool",
                "message": "tool_state.selected_tool must be a string or null.",
            }
        )

    return {"is_ready": len(missing) == 0 and len(errors) == 0, "missing": missing, "errors": errors}


def default_provenance(authority: str = "derived", source: str = "unset") -> dict[str, Any]:
    """Return a default provenance envelope with authority metadata."""
    normalized_authority = authority if authority in AUTHORITY_ORDER else "derived"
    return {"authority": normalized_authority, "source": source}


def unknown_item(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Represent unresolved/unknown coverage deterministically."""
    payload = dict(value) if isinstance(value, Mapping) else {}
    payload["status"] = "unknown"
    payload["provenance"] = default_provenance(authority="derived", source="unknown")
    return payload


def _normalize_stored_item(item: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Normalize arbitrary item payloads with required status/provenance fields."""
    return _normalize_stored_item_impl(
        item,
        authority_order=AUTHORITY_ORDER,
        default_provenance=default_provenance,
    )


def _normalize_active_decision(value: Any) -> dict[str, Any] | None:
    """Normalize advisory active-decision payload for pointer-first memory."""
    return _normalize_active_decision_impl(value, statuses=ACTIVE_DECISION_STATUSES)


def _default_validation() -> dict[str, Any]:
    """Return a fail-closed validation default for uninitialized route context."""
    return {
        "is_ready": False,
        "missing": [{"code": "context_uninitialized", "message": "Missing routing context"}],
        "errors": [],
    }


def _normalize_objective(value: Any) -> dict[str, Any]:
    """Normalize objective payload with explicit provenance defaults."""
    return _normalize_objective_impl(
        value,
        authority_order=AUTHORITY_ORDER,
        default_provenance=default_provenance,
    )


def _base_working_memory() -> dict[str, Any]:
    """Return deterministic working-memory defaults."""
    return {
        "schema": SCHEMA_ID,
        "v": SCHEMA_VERSION,
        "authority": {
            "order": list(AUTHORITY_ORDER),
            "llm_proposals_authoritative": False,
        },
        "id_contract": {
            "typed_prefixes": {
                ID_KIND_ENTITY: "entity:<id>",
                ID_KIND_TOOL_RUN: "tool_run:<id>",
                ID_KIND_COLLECTION: "collection:<id>",
                ID_KIND_TARGET: "target:<id>",
            },
            "active_update_rules": {
                "unset": "retain",
                "null": "clear",
                "value": "set_if_resolved_else_clear",
            },
        },
        "ids": {
            "task_id": 0,
            "conversation_id": "",
            "turn_id": "",
            "turn_sequence": 0,
        },
        "timestamps": {"updated_at": ""},
        "input": {"user_message_ref": {"turn_sequence": 0}, "user_message_excerpt": ""},
        "recent_turns": [],
        "stage": "chat",
        "objective": {
            "text": "unknown",
            "status": "unknown",
            "source": "unset",
            "provenance": default_provenance(authority="derived", source="unset"),
        },
        # Always present; null is valid in no-target workflows.
        "active": {"target_id": None, "subject_id": None, "collection_id": None},
        # Always present; populated by deterministic validators in later phases.
        "required_inputs": [],
        "validation": _default_validation(),
        "preferences": {"verbosity": "normal", "output_format": "text", "language": "en"},
        "constraints": {"scope": [], "boundaries": [], "budgets": {}, "tool_policy": {}},
        "referents": {},
        # Empty is valid for no-target/no-entity workflows.
        "entities": {},
        "facts": [],
        "coverage": [],
        "tool_state": {
            "selected_categories": [],
            "selected_tool": None,
            "tool_params": {},
            "status": "none",
        },
        "tool_runs": [],
        "collections": [],
        "open_questions": [],
        "commitments": [],
        "analysis_notes": [],
        "available_findings": [],
        # Phase 2: PTR phase ledger moved from top-level metadata keys into
        # working memory; this sequence is intentionally uncapped.
        "current_turn_phases": [],
        "current_turn_phase_counter": 0,
        "current_turn_phase_turn": 0,
        "active_decision": None,
        "intent_brief": {},
    }


def _cap_sequence(items: list[Any], cap: int) -> list[Any]:
    """Apply FIFO cap to a sequence; keep newest items deterministically."""
    if len(items) <= cap:
        return items
    return items[-cap:]


def _cap_mapping(items: Mapping[str, Any], cap: int) -> dict[str, Any]:
    """Apply insertion-order FIFO cap to a mapping; keep newest key insertions."""
    pairs = list(items.items())
    if len(pairs) <= cap:
        return dict(pairs)
    return dict(pairs[-cap:])


def _as_list(value: Any) -> list[Any]:
    """Return a list value or a safe empty list fallback."""
    if isinstance(value, list):
        return value
    return []


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return a mapping value or a safe empty dict fallback."""
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _normalize_current_turn_phases(value: Any) -> list[dict[str, Any]]:
    """Normalize iteration-memory ledger storage as a plain list of mappings."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            normalized.append(deepcopy(dict(item)))
    return normalized


def _coerce_non_negative_int(value: Any) -> int:
    """Coerce optional numeric field to a non-negative int with 0 fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 0 else 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def apply_caps(memory: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of memory with deterministic caps applied."""
    bounded = dict(memory)
    for field, cap in SEQUENCE_CAPS.items():
        bounded[field] = _cap_sequence(_as_list(bounded.get(field)), cap)
    for field, cap in MAPPING_CAPS.items():
        bounded[field] = _cap_mapping(_as_mapping(bounded.get(field)), cap)
    bounded["active_decision"] = _normalize_active_decision(bounded.get("active_decision"))
    return bounded


def create_working_memory(ids: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Create a deterministic default working-memory payload."""
    memory = _base_working_memory()
    if ids:
        merged_ids = dict(memory["ids"])
        merged_ids.update(dict(ids))
        memory["ids"] = merged_ids
    return normalize_working_memory(memory)


def normalize_working_memory(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize an optional payload into a fully-populated bounded contract."""
    memory = _base_working_memory()
    if payload:
        for key, value in payload.items():
            memory[key] = deepcopy(value)

    # Ensure required contract fields are always present with safe defaults.
    # Ensure capped containers are always present and type-safe before capping.
    for field in SEQUENCE_CAPS:
        if field == "available_findings":
            continue
        memory[field] = [_normalize_stored_item(item) for item in _as_list(memory.get(field))]
    memory["available_findings"] = normalize_available_findings(
        _as_list(memory.get("available_findings")),
        cap=CAP_AVAILABLE_FINDINGS,
    )
    memory["current_turn_phases"] = _normalize_current_turn_phases(
        memory.get("current_turn_phases")
    )
    memory["current_turn_phase_counter"] = _coerce_non_negative_int(
        memory.get("current_turn_phase_counter")
    )
    memory["current_turn_phase_turn"] = _coerce_non_negative_int(
        memory.get("current_turn_phase_turn")
    )
    memory["intent_brief"] = (
        dict(memory.get("intent_brief"))
        if isinstance(memory.get("intent_brief"), Mapping)
        else {}
    )
    memory["entities"] = {
        key: _normalize_stored_item(value) for key, value in _as_mapping(memory.get("entities")).items()
    }
    memory["active_decision"] = _normalize_active_decision(memory.get("active_decision"))
    normalized_entities: dict[str, Any] = {}
    for key, value in memory["entities"].items():
        typed_key = ensure_typed_id(ID_KIND_ENTITY, str(key))
        if typed_key:
            normalized_entities[typed_key] = value
    memory["entities"] = normalized_entities
    memory["objective"] = _normalize_objective(memory.get("objective"))
    memory["active"] = _normalize_active(memory, memory.get("active"))
    memory["required_inputs"] = compute_required_inputs(memory)
    memory["validation"] = compute_validation(memory)

    return apply_caps(memory)


def set_active_decision(
    memory: Mapping[str, Any] | None,
    active_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Set or clear the advisory active decision contract in working memory."""
    updated = normalize_working_memory(memory)
    updated["active_decision"] = _normalize_active_decision(active_decision)
    return normalize_working_memory(updated)


def _append_capped_stored_item(
    memory: Mapping[str, Any],
    *,
    field: str,
    item: Mapping[str, Any],
    cap: int,
    id_kind: str | None = None,
    refresh_active_handles: bool = False,
) -> dict[str, Any]:
    """Append one normalized item to a capped sequence field."""
    updated = normalize_working_memory(memory)
    normalized = _normalize_stored_item(item)
    if id_kind and "id" in normalized:
        normalized["id"] = ensure_typed_id(id_kind, normalized.get("id"))
    updated[field] = _cap_sequence(updated[field] + [normalized], cap)
    if refresh_active_handles:
        updated["active"] = _normalize_active(updated, updated.get("active"))
    return updated


def append_recent_turn(memory: Mapping[str, Any], turn: Mapping[str, Any]) -> dict[str, Any]:
    """Append a recent turn with deterministic FIFO eviction."""
    return _append_capped_stored_item(
        memory,
        field="recent_turns",
        item=turn,
        cap=CAP_RECENT_TURNS,
    )


def append_fact(memory: Mapping[str, Any], fact: Mapping[str, Any]) -> dict[str, Any]:
    """Append a fact with deterministic FIFO eviction."""
    return _append_capped_stored_item(
        memory,
        field="facts",
        item=fact,
        cap=CAP_FACTS,
    )


def upsert_entity(memory: Mapping[str, Any], entity_id: str, entity: Mapping[str, Any]) -> dict[str, Any]:
    """Upsert an entity and enforce deterministic insertion-order map cap."""
    updated = normalize_working_memory(memory)
    entities = dict(updated["entities"])
    typed_entity_id = ensure_typed_id(ID_KIND_ENTITY, str(entity_id))
    if typed_entity_id is not None:
        entities[typed_entity_id] = _normalize_stored_item(entity)
    updated["entities"] = _cap_mapping(entities, CAP_ENTITIES)
    updated["active"] = _normalize_active(updated, updated.get("active"))
    return updated


def append_tool_run(memory: Mapping[str, Any], tool_run: Mapping[str, Any]) -> dict[str, Any]:
    """Append a tool run with deterministic FIFO eviction."""
    return _append_capped_stored_item(
        memory,
        field="tool_runs",
        item=tool_run,
        cap=CAP_TOOL_RUNS,
        id_kind=ID_KIND_TOOL_RUN,
        refresh_active_handles=True,
    )


def append_collection(memory: Mapping[str, Any], collection: Mapping[str, Any]) -> dict[str, Any]:
    """Append a collection pointer with deterministic FIFO eviction."""
    return _append_capped_stored_item(
        memory,
        field="collections",
        item=collection,
        cap=CAP_COLLECTIONS,
        id_kind=ID_KIND_COLLECTION,
        refresh_active_handles=True,
    )


def append_open_question(memory: Mapping[str, Any], question: Mapping[str, Any]) -> dict[str, Any]:
    """Append an open question with deterministic FIFO eviction."""
    return _append_capped_stored_item(
        memory,
        field="open_questions",
        item=question,
        cap=CAP_OPEN_QUESTIONS,
    )
