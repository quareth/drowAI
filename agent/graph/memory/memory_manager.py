"""Deterministic reducer for runtime-state working memory updates.

This module is the single reducer authority for producing bounded,
pointer-first working-memory snapshots from structured inputs across
turn start, tool result, and turn end transitions.

Scope narrowing (Phase 4)
-------------------------
Working memory is scoped to **deterministic runtime state** — active
target, current goal/objective, latest decision, selected tool, and
relevant typed IDs / handles. It is NOT a cross-turn transcript
continuity authority: recent-turn excerpts that the reducer still
tracks internally (for structural fields such as target-resolution
fallback in ``target_resolution``) are never surfaced into prompts.
Prompt-authority recent-transcript continuity is owned by the shared
``ConversationContextBundle`` — see ``agent/graph/context/contracts.py``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from .findings import merge_available_findings, project_candidate_observations
from .working_memory import (
    append_collection,
    append_open_question,
    append_recent_turn,
    append_tool_run,
    normalize_working_memory,
    set_active_decision,
    update_active_handles,
)
from .target_resolution import (
    RUNTIME_TOOL_TARGET_FIELD_SPECS,
    coerce_target_candidate,
    resolve_active_target_from_working_memory,
)

USER_EXCERPT_MAX = 280
TOOL_SUMMARY_MAX = 280
CONVERSATION_TAIL_MAX = 4

SENSITIVE_KEY_MARKERS = ("token", "password", "secret", "api_key", "authorization", "cookie", "bearer")
REDACTED_TEXT = "<REDACTED>"
_INTENT_PRIMARY_TARGET_REFERENT = "intent:target"
_INTENT_GOAL_SOURCE = "intent_turn_interpretation"
_INTENT_BLOCKER_OPEN_QUESTION_CODE = "intent_execution_blocked"
_TARGET_CONTINUITY_ALLOW = "allow"
_TARGET_OPEN_QUESTION_CODES = {
    "missing_target_handle",
    "unresolved_target_for_tool_run",
    "target_handle_required",
}


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mask_sensitive_text(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in SENSITIVE_KEY_MARKERS):
        return REDACTED_TEXT
    return text


def _redact_sensitive_payload(value: Any) -> Any:
    """Return a deep-copied payload with sensitive values replaced deterministically."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in SENSITIVE_KEY_MARKERS):
                redacted[key_text] = REDACTED_TEXT
            else:
                redacted[key_text] = _redact_sensitive_payload(item)
        return redacted

    if isinstance(value, list):
        return [_redact_sensitive_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive_payload(item) for item in value]

    try:
        copied = deepcopy(value)
    except Exception:
        copied = str(value)
    if isinstance(copied, str):
        return _mask_sensitive_text(copied)
    return copied


def _stage_from_route(route: str) -> str:
    normalized = str(route or "chat").strip().lower()
    if normalized in {"simple_tool_execution", "deep_reasoning", "tool"}:
        return "tool_selection"
    if normalized in {"tool_selection", "tool_parameterization", "tool_execution", "approval", "chat"}:
        return normalized
    return "chat"


def _extract_turn_sequence(turn: Mapping[str, Any]) -> int:
    value = turn.get("turn_sequence")
    if isinstance(value, int):
        return value
    return 0


def _format_recent_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    content = turn.get("content", turn.get("content_excerpt", ""))
    return {
        "role": str(turn.get("role", "user")),
        "turn_sequence": _extract_turn_sequence(turn),
        "content_excerpt": _mask_sensitive_text(_truncate_text(content, USER_EXCERPT_MAX)),
    }


def _merge_constraints(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(current))
    for key, value in incoming.items():
        if isinstance(value, Mapping):
            target = _as_mapping(merged.get(key))
            target.update(dict(value))
            merged[key] = target
        else:
            merged[key] = deepcopy(value)
    return merged


def _upsert_open_question(memory: Mapping[str, Any], code: str, message: str, stage: str) -> dict[str, Any]:
    normalized = normalize_working_memory(memory)
    questions = _as_list(normalized.get("open_questions"))
    for existing in questions:
        if isinstance(existing, Mapping) and existing.get("code") == code:
            return normalized
    return append_open_question(
        normalized,
        {"code": code, "stage": stage, "message": message, "status": "open"},
    )


def _drop_open_questions(memory: Mapping[str, Any], codes: set[str]) -> dict[str, Any]:
    normalized = normalize_working_memory(memory)
    kept_questions = []
    for item in _as_list(normalized.get("open_questions")):
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code", "")).strip()
        if code in codes:
            continue
        kept_questions.append(deepcopy(dict(item)))
    normalized["open_questions"] = kept_questions
    return normalize_working_memory(normalized)


def _sanitize_compact_envelope(compact_envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    envelope = _as_mapping(compact_envelope)
    forbidden_keys = {"stdout", "stderr", "stdout_excerpt", "stderr_excerpt", "raw_output"}
    return {k: deepcopy(v) for k, v in envelope.items() if k not in forbidden_keys}


def _extract_primary_target(intent_hints: Mapping[str, Any] | None) -> dict[str, Any] | None:
    hints = _as_mapping(intent_hints)
    direct_target = coerce_target_candidate(hints.get("target"), allow_single_label=True)
    if direct_target:
        return direct_target
    raw_targets = hints.get("targets") or []
    candidate = coerce_target_candidate(raw_targets, allow_single_label=True)
    if candidate:
        return candidate
    if isinstance(raw_targets, Sequence) and not isinstance(raw_targets, (str, bytes, bytearray)):
        for item in raw_targets:
            candidate = coerce_target_candidate(item, allow_single_label=True)
            if candidate:
                return candidate
    return None


def _extract_target_candidate_from_params(tool_params: Mapping[str, Any]) -> dict[str, Any] | None:
    params = _as_mapping(tool_params)
    if not params:
        return None
    return coerce_target_candidate(
        params,
        field_specs=RUNTIME_TOOL_TARGET_FIELD_SPECS,
    )


def _target_continuity_status(intent_target_continuity: Mapping[str, Any] | None) -> str:
    continuity = _as_mapping(intent_target_continuity)
    status = str(continuity.get("status") or "").strip().lower()
    if status == _TARGET_CONTINUITY_ALLOW:
        return _TARGET_CONTINUITY_ALLOW
    if status in {"disallow", "ambiguous"}:
        return status
    return "disallow"


def _project_objective_from_turn_interpretation(
    intent_turn_interpretation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a normalized objective projection from classifier turn interpretation."""
    interpretation = _as_mapping(intent_turn_interpretation)
    goal = str(interpretation.get("next_operational_goal") or "").strip()
    if not goal:
        return None

    readiness = str(interpretation.get("execution_readiness") or "").strip().lower()
    if readiness == "blocked":
        status = "blocked"
    elif readiness == "ready":
        status = "active"
    else:
        status = "ambiguous"

    return {
        "text": goal,
        "status": status,
        "source": _INTENT_GOAL_SOURCE,
        "provenance": {
            "authority": "derived",
            "source": _INTENT_GOAL_SOURCE,
        },
    }


class MemoryManager:
    """Single-writer deterministic reducer for working-memory state."""

    @staticmethod
    def reduce_turn_start(
        previous: Mapping[str, Any] | None,
        user_message: str,
        conversation_history_tail: Sequence[Mapping[str, Any]] | None,
        runtime_ids: Mapping[str, Any] | None,
        route: str,
        constraints: Mapping[str, Any] | None,
        intent_hints: Mapping[str, Any] | None,
        intent_target_continuity: Mapping[str, Any] | None = None,
        intent_turn_interpretation: Mapping[str, Any] | None = None,
        project_classifier_goal: bool = True,
    ) -> dict[str, Any]:
        """Reduce turn-start inputs into a bounded canonical memory snapshot."""
        memory = normalize_working_memory(previous)
        previous_turn_id = str(_as_mapping(memory.get("ids")).get("turn_id") or "")
        previous_turn_sequence = int(_as_mapping(memory.get("ids")).get("turn_sequence") or 0)
        ids = _as_mapping(memory.get("ids"))
        ids.update(_as_mapping(runtime_ids))
        memory["ids"] = ids
        current_turn_id = str(ids.get("turn_id") or "")
        current_turn_sequence = int(ids.get("turn_sequence") or 0)
        turn_changed = (
            (previous_turn_id and current_turn_id and previous_turn_id != current_turn_id)
            or (current_turn_sequence > 0 and previous_turn_sequence > 0 and current_turn_sequence != previous_turn_sequence)
        )
        if turn_changed:
            # New turn/replan should not inherit stale advisory decisions.
            memory = set_active_decision(memory, None)

        stage = _stage_from_route(route)
        memory["stage"] = stage
        memory["input"] = {
            "user_message_ref": {"turn_sequence": int(ids.get("turn_sequence", 0) or 0)},
            "user_message_excerpt": _mask_sensitive_text(_truncate_text(user_message, USER_EXCERPT_MAX)),
        }

        tail = list(conversation_history_tail or [])[-CONVERSATION_TAIL_MAX:]
        for turn in tail:
            memory = append_recent_turn(memory, _format_recent_turn(_as_mapping(turn)))

        current_turn = {
            "role": "user",
            "turn_sequence": int(ids.get("turn_sequence", 0) or 0),
            "content_excerpt": _mask_sensitive_text(_truncate_text(user_message, USER_EXCERPT_MAX)),
        }
        memory = append_recent_turn(memory, current_turn)

        if constraints:
            memory["constraints"] = _merge_constraints(_as_mapping(memory.get("constraints")), constraints)

        continuity_status = _target_continuity_status(intent_target_continuity)
        primary_target = _extract_primary_target(intent_hints)
        primary_target_source = "intent_hints"
        if primary_target is None:
            primary_target = coerce_target_candidate(user_message)
            if primary_target:
                primary_target_source = "user_message"
        if primary_target is None and continuity_status == _TARGET_CONTINUITY_ALLOW:
            resolved_active_target = resolve_active_target_from_working_memory(memory)
            primary_target = coerce_target_candidate(
                resolved_active_target,
                allow_single_label=True,
            ) if resolved_active_target else None
            if primary_target:
                primary_target_source = "working_memory_active_binding"
        if primary_target:
            referents = _as_mapping(memory.get("referents"))
            referents[_INTENT_PRIMARY_TARGET_REFERENT] = {
                "value": primary_target.get("value"),
                "kind": primary_target.get("kind"),
                "confidence": primary_target.get("confidence"),
                "source": primary_target_source,
            }
            memory["referents"] = referents
            memory = update_active_handles(memory, target_id=_INTENT_PRIMARY_TARGET_REFERENT)
            memory = _drop_open_questions(memory, _TARGET_OPEN_QUESTION_CODES)
        elif continuity_status in {"disallow", "ambiguous"}:
            # New turn should not inherit stale active target bindings.
            memory = update_active_handles(memory, target_id=None)
            if stage == "tool_selection":
                memory = _drop_open_questions(memory, _TARGET_OPEN_QUESTION_CODES)
        elif stage == "tool_selection":
            # Selection-stage memory should remain advisory and never carry stale target blockers.
            memory = _drop_open_questions(memory, _TARGET_OPEN_QUESTION_CODES)

        projected_objective = (
            _project_objective_from_turn_interpretation(intent_turn_interpretation)
            if project_classifier_goal
            else None
        )
        if projected_objective is not None:
            objective = projected_objective
        else:
            objective = {"text": "unknown", "status": "unknown", "source": "unset"}
        memory["objective"] = objective

        interpretation = _as_mapping(intent_turn_interpretation)
        readiness = str(interpretation.get("execution_readiness") or "").strip().lower()
        blocking_reason = str(interpretation.get("blocking_reason") or "").strip()
        if readiness == "blocked" and blocking_reason:
            memory = _upsert_open_question(
                memory,
                code=_INTENT_BLOCKER_OPEN_QUESTION_CODE,
                message=blocking_reason,
                stage=stage,
            )
        else:
            memory = _drop_open_questions(memory, {_INTENT_BLOCKER_OPEN_QUESTION_CODE})

        memory = normalize_working_memory(memory)
        if stage in {"tool_parameterization", "tool_execution", "approval"} and memory["active"][
            "target_id"
        ] is None:
            memory = _upsert_open_question(
                memory,
                code="missing_target_handle",
                message="Please specify which target to operate on.",
                stage=stage,
            )
        return normalize_working_memory(memory)

    @staticmethod
    def reduce_intent_brief_fold(
        previous: Mapping[str, Any] | None,
        seed: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Fold the pre-graph classifier seed into working-memory intent brief."""
        memory = normalize_working_memory(previous)
        memory["intent_brief"] = dict(seed) if isinstance(seed, Mapping) else {}
        return normalize_working_memory(memory)

    @staticmethod
    def reduce_phase_ledger_reset(
        previous: Mapping[str, Any] | None,
        *,
        turn_sequence: int,
        next_phase_counter: int = 0,
    ) -> dict[str, Any]:
        """Set the active phase-ledger turn scope and counter in working memory."""
        memory = normalize_working_memory(previous)
        next_counter = int(next_phase_counter) if isinstance(next_phase_counter, int) else 0
        if next_counter < 0:
            next_counter = 0
        memory["current_turn_phase_turn"] = int(turn_sequence)
        memory["current_turn_phase_counter"] = next_counter
        # Deliberately avoid a second full normalization pass: ``memory`` is
        # already normalized above and this reducer only mutates scalar
        # phase-ledger fields.
        return memory

    @staticmethod
    def reduce_phase_ledger_append(
        previous: Mapping[str, Any] | None,
        *,
        record: Mapping[str, Any],
        turn_sequence: int,
        phase_sequence: int,
    ) -> dict[str, Any]:
        """Append one phase-ledger record to working memory deterministically.

        The list and the per-turn counter are twins: when the active turn
        changes, both are reset so storage stays scoped to the active turn.
        Prior-turn records are pruned here, on the boundary, before the new
        record is appended.
        """
        memory = normalize_working_memory(previous)
        normalized_turn = int(turn_sequence)
        scoped_turn = memory.get("current_turn_phase_turn")
        same_turn_scope = isinstance(scoped_turn, int) and scoped_turn == normalized_turn

        existing = list(memory.get("current_turn_phases") or [])
        # Cross-turn persistence is intentionally not supported (see
        # iteration_memory.py module docstring). Dropping prior-turn
        # records on the boundary keeps storage aligned with the rendering
        # contract (renderer always filters by turn_sequence) and bounds
        # the ledger at one turn's worth of phases.
        #
        # We can't reuse ``same_turn_scope`` for this check: the upstream
        # ``reserve_next_phase_sequence`` advances ``current_turn_phase_turn``
        # to the new turn before reaching this reducer, so the scope flag
        # is True at the boundary. The records themselves still carry
        # their original ``turn_sequence``, so we read the boundary off
        # the last record instead.
        last_record_turn = existing[-1].get("turn_sequence") if existing else None
        crosses_turn_boundary = (
            isinstance(last_record_turn, int) and last_record_turn != normalized_turn
        )
        ledger = [] if crosses_turn_boundary else existing
        ledger.append(deepcopy(dict(record)))
        memory["current_turn_phases"] = ledger

        if same_turn_scope:
            current_counter = memory.get("current_turn_phase_counter")
            next_counter = int(current_counter) if isinstance(current_counter, int) else 0
        else:
            # Turn boundary: never carry stale counter state into a new turn.
            next_counter = 0

        memory["current_turn_phase_turn"] = normalized_turn
        phase_plus_one = int(phase_sequence) + 1
        if phase_plus_one > next_counter:
            next_counter = phase_plus_one
        memory["current_turn_phase_counter"] = next_counter
        # Deliberately avoid a second full normalization pass: this reducer
        # mutates only the phase-ledger list and scalar turn/counter fields
        # after obtaining a normalized base snapshot above.
        return memory

    @staticmethod
    def reduce_post_tool_decision(
        previous: Mapping[str, Any] | None,
        active_decision: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Reduce post-tool active decision state into canonical memory."""
        return set_active_decision(previous, active_decision)

    @staticmethod
    def reduce_tool_result(
        previous: Mapping[str, Any] | None,
        tool_id: str,
        tool_params: Mapping[str, Any] | None,
        compact_envelope: Mapping[str, Any] | None,
        artifact_refs: Sequence[Mapping[str, Any]] | None,
        execution_id: str,
        observed_findings: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Reduce a tool-result event into pointer-first memory updates."""
        memory = normalize_working_memory(previous)
        raw_params = _as_mapping(tool_params)
        target_candidate = _extract_target_candidate_from_params(raw_params)
        params = _redact_sensitive_payload(raw_params)
        compact = _sanitize_compact_envelope(compact_envelope)

        memory["tool_state"] = {
            "selected_categories": _as_list(_as_mapping(memory.get("tool_state")).get("selected_categories")),
            "selected_tool": str(tool_id or ""),
            "tool_params": deepcopy(params),
            "status": "completed",
        }

        coverage = _as_list(memory.get("coverage"))
        coverage.append(
            {
                "tool_id": str(tool_id or ""),
                "execution_id": str(execution_id or ""),
                "checked_params": sorted([str(k) for k in params.keys()]),
                "status": "covered",
                "provenance": {"authority": "tool", "source": f"tool:{execution_id}"},
            }
        )
        memory["coverage"] = coverage

        tool_run = {
            "id": execution_id,
            "tool_id": str(tool_id or ""),
            "status": "completed",
            "summary": _mask_sensitive_text(_truncate_text(compact.get("summary", ""), TOOL_SUMMARY_MAX)),
            "key_findings": _redact_sensitive_payload(_as_list(compact.get("key_findings"))),
            "errors": _redact_sensitive_payload(_as_list(compact.get("errors"))),
            "provenance": {"authority": "tool", "source": f"tool:{execution_id}"},
        }
        memory = append_tool_run(memory, tool_run)
        memory = update_active_handles(memory, subject_id=execution_id)

        refs = list(artifact_refs or [])
        for idx, ref in enumerate(refs):
            collection_id = f"{execution_id}:{idx}"
            memory = append_collection(
                memory,
                {
                    "id": collection_id,
                    "artifact_ref": deepcopy(_as_mapping(ref)),
                    "count": int(_as_mapping(ref).get("count", 0) or 0),
                    "status": "available",
                    "provenance": {"authority": "tool", "source": f"tool:{execution_id}"},
                },
            )
            memory = update_active_handles(memory, collection_id=collection_id)

        if target_candidate:
            referents = _as_mapping(memory.get("referents"))
            referent_key = f"tool:{execution_id}:target"
            referents[referent_key] = {
                "value": str(target_candidate.get("value")),
                "kind": target_candidate.get("kind"),
                "confidence": target_candidate.get("confidence"),
                "source": "tool_params",
            }
            memory["referents"] = referents
            memory = update_active_handles(memory, target_id=referent_key)
        elif _as_mapping(memory.get("active")).get("target_id") is None:
            memory = _upsert_open_question(
                memory,
                code="unresolved_target_for_tool_run",
                message="Tool execution has no resolvable target handle; clarify target.",
                stage="tool_execution",
            )

        memory["available_findings"] = merge_available_findings(
            memory.get("available_findings"),
            observed_findings or [],
        )
        return normalize_working_memory(memory)

    @staticmethod
    def reduce_post_tool_findings(
        previous: Mapping[str, Any] | None,
        candidate_observations: Sequence[Mapping[str, Any]] | None,
        *,
        active_target: str | None = None,
    ) -> dict[str, Any]:
        """Reduce PTR candidate observations into canonical runtime findings."""
        memory = normalize_working_memory(previous)
        projected = project_candidate_observations(
            candidate_observations or [],
            active_target=str(active_target or ""),
        )
        memory["available_findings"] = merge_available_findings(
            memory.get("available_findings"),
            projected,
        )
        return normalize_working_memory(memory)

    @staticmethod
    def reduce_turn_end(
        previous: Mapping[str, Any] | None,
        assistant_commitments: Sequence[Mapping[str, Any]] | None,
        open_questions: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        """Reduce turn-end commitments/questions into bounded memory."""
        memory = normalize_working_memory(previous)
        commitments = _as_list(memory.get("commitments"))
        for item in list(assistant_commitments or []):
            if isinstance(item, Mapping):
                commitments.append(deepcopy(dict(item)))
        memory["commitments"] = commitments[-20:]

        for item in list(open_questions or []):
            if isinstance(item, Mapping):
                code = str(item.get("code", "clarification_required"))
                message = str(item.get("message", "Additional clarification is required."))
                memory = _upsert_open_question(memory, code=code, message=message, stage=str(item.get("stage", "chat")))

        memory["stage"] = "chat"
        return normalize_working_memory(memory)
