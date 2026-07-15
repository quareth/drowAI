"""Deterministic derivation of the unified working-memory intent brief.

This module builds a single classifier-derived brief payload used across
planner/category/articulation surfaces and provides the pre-graph seed
bridge (`intent_brief_seed`) that is folded into
`metadata["working_memory"]["intent_brief"]` by the first graph node.

Boundary rules:
- Pure deterministic derivation only (no I/O, no transcript reads).
- Inputs come from classifier-owned metadata keys.
- Missing/partial classifier output is tolerated via safe defaults.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, TypedDict


# ---------------------------------------------------------------------------
# Metadata key constants.
# ---------------------------------------------------------------------------

METADATA_KEY_TURN_INTERPRETATION: str = "intent_turn_interpretation"
METADATA_KEY_CLASSIFIER_RAW_RESPONSE: str = "intent_classifier_raw_response"
METADATA_KEY_REQUEST_CONTRACT: str = "request_contract"
METADATA_KEY_INTENT_TARGET_RESOLUTION: str = "intent_target_resolution"
METADATA_KEY_INTENT_TARGET_CONTINUITY: str = "intent_target_continuity"
METADATA_KEY_INTENT_BRIEF_SEED: str = "intent_brief_seed"


# ---------------------------------------------------------------------------
# Brief payload shapes.
# ---------------------------------------------------------------------------


class RequestContractSlice(TypedDict):
    """Downstream-relevant slice of the classifier's request contract."""

    question_type: Optional[str]
    answer_style: Optional[str]
    terminal_when: Optional[str]


class TargetSlice(TypedDict):
    """Downstream-relevant slice of target resolution + continuity metadata."""

    resolved_target: Optional[str]
    target_status: str
    target_source: str
    prior_target_reuse: str


class WorkingMemoryIntentBrief(TypedDict, total=False):
    """Unified classifier-derived brief folded into working memory."""

    resolved_user_intent: Optional[str]
    original_goal: Optional[str]
    task_seed: List[str]
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
    explicit_constraints: List[str]
    suggested_category_focus: List[str]
    retrieval_hints: List[str]
    relevant_memory_fragments: List[str]
    request_contract: RequestContractSlice


# ---------------------------------------------------------------------------
# Input normalization helpers (internal).
# ---------------------------------------------------------------------------


_CONTINUATION_MODES = frozenset(
    {
        "new_request",
        "continue_prior_work",
        "continue_prior_step",
        "revise_approach",
        "ambiguous",
    }
)
_EXECUTION_READINESS_VALUES = frozenset({"ready", "blocked", "ambiguous"})
_TARGET_STATUS_VALUES = frozenset({"resolved", "unresolved", "ambiguous"})
_TARGET_SOURCE_VALUES = frozenset(
    {"explicit_current_message", "referential_history", "environment", "none"}
)
_TARGET_CONTINUITY_VALUES = frozenset({"allow", "disallow", "ambiguous"})

_QUESTION_TYPE_VALUES = frozenset({"binary_check", "multi_step", "open_ended"})
_ANSWER_STYLE_VALUES = frozenset({"short", "normal"})
_TERMINAL_WHEN_VALUES = frozenset({"determined", "all_steps_done"})


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` if it is a mapping, else an empty mapping."""
    if isinstance(value, Mapping):
        return value
    return {}


def _as_optional_str(value: Any) -> Optional[str]:
    """Coerce ``value`` to a non-empty stripped string, else ``None``."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _as_str_list(value: Any, *, max_items: int | None = None) -> List[str]:
    """Coerce ``value`` to a list of non-empty stripped strings."""
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                result.append(stripped)
                if max_items is not None and len(result) >= max_items:
                    break
    return result


def _enum_or_default(value: Any, allowed: frozenset, default: str) -> str:
    """Return ``value`` if it is in ``allowed`` (case-insensitive), else ``default``."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in allowed:
            return lowered
    return default


def _read_turn_interpretation(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve turn interpretation payload from metadata."""
    explicit = metadata.get(METADATA_KEY_TURN_INTERPRETATION)
    if isinstance(explicit, Mapping):
        return explicit

    raw_response = metadata.get(METADATA_KEY_CLASSIFIER_RAW_RESPONSE)
    if isinstance(raw_response, Mapping):
        nested = raw_response.get("turn_interpretation")
        if isinstance(nested, Mapping):
            return nested

    return {}


def _build_request_contract_slice(metadata: Mapping[str, Any]) -> RequestContractSlice:
    """Extract the downstream-relevant fields of ``request_contract``."""
    contract = _as_mapping(metadata.get(METADATA_KEY_REQUEST_CONTRACT))
    question_type = contract.get("question_type")
    answer_style = contract.get("answer_style")
    terminal_when = contract.get("terminal_when")

    normalized_question = (
        question_type
        if isinstance(question_type, str)
        and question_type.strip().lower() in _QUESTION_TYPE_VALUES
        else None
    )
    normalized_answer = (
        answer_style
        if isinstance(answer_style, str)
        and answer_style.strip().lower() in _ANSWER_STYLE_VALUES
        else None
    )
    normalized_terminal = (
        terminal_when
        if isinstance(terminal_when, str)
        and terminal_when.strip().lower() in _TERMINAL_WHEN_VALUES
        else None
    )

    return RequestContractSlice(
        question_type=(
            normalized_question.strip().lower()
            if isinstance(normalized_question, str)
            else None
        ),
        answer_style=(
            normalized_answer.strip().lower()
            if isinstance(normalized_answer, str)
            else None
        ),
        terminal_when=(
            normalized_terminal.strip().lower()
            if isinstance(normalized_terminal, str)
            else None
        ),
    )


def _build_target_slice(metadata: Mapping[str, Any]) -> TargetSlice:
    """Extract the downstream-relevant target resolution + continuity slice."""
    resolution = _as_mapping(metadata.get(METADATA_KEY_INTENT_TARGET_RESOLUTION))
    continuity = _as_mapping(metadata.get(METADATA_KEY_INTENT_TARGET_CONTINUITY))

    target_status = _enum_or_default(
        resolution.get("target_status"), _TARGET_STATUS_VALUES, "unresolved"
    )
    target_source = _enum_or_default(
        resolution.get("target_source"), _TARGET_SOURCE_VALUES, "none"
    )
    prior_target_reuse = _enum_or_default(
        continuity.get("status"), _TARGET_CONTINUITY_VALUES, "disallow"
    )

    resolved_target = _as_optional_str(resolution.get("resolved_target"))
    if target_status != "resolved":
        resolved_target = None

    return TargetSlice(
        resolved_target=resolved_target,
        target_status=target_status,
        target_source=target_source,
        prior_target_reuse=prior_target_reuse,
    )


def _ensure_turn_interpretation_metadata(metadata: Dict[str, Any]) -> None:
    """Ensure top-level turn interpretation exists for downstream deterministic reads."""
    raw_response = metadata.get(METADATA_KEY_CLASSIFIER_RAW_RESPONSE)
    if isinstance(raw_response, Mapping):
        nested_interpretation = raw_response.get("turn_interpretation")
        if isinstance(nested_interpretation, Mapping):
            metadata[METADATA_KEY_TURN_INTERPRETATION] = dict(nested_interpretation)
            return
    metadata.setdefault(METADATA_KEY_TURN_INTERPRETATION, {})


# ---------------------------------------------------------------------------
# Public builders.
# ---------------------------------------------------------------------------


def build_working_memory_intent_brief(
    metadata: Mapping[str, Any],
) -> WorkingMemoryIntentBrief:
    """Build the unified intent brief that is folded into working memory."""
    interpretation = _read_turn_interpretation(metadata)
    target = _build_target_slice(metadata)

    return WorkingMemoryIntentBrief(
        resolved_user_intent=_as_optional_str(
            interpretation.get("resolved_user_intent")
        ),
        original_goal=_as_optional_str(interpretation.get("original_goal")),
        task_seed=_as_str_list(interpretation.get("task_seed"), max_items=3),
        overall_goal=_as_optional_str(interpretation.get("overall_goal")),
        continuation_mode=_enum_or_default(
            interpretation.get("continuation_mode"),
            _CONTINUATION_MODES,
            "ambiguous",
        ),
        resolved_step_title=_as_optional_str(interpretation.get("resolved_step_title")),
        resolved_step_detail=_as_optional_str(
            interpretation.get("resolved_step_detail")
        ),
        next_operational_goal=_as_optional_str(
            interpretation.get("next_operational_goal")
        ),
        success_condition=_as_optional_str(interpretation.get("success_condition")),
        execution_readiness=_enum_or_default(
            interpretation.get("execution_readiness"),
            _EXECUTION_READINESS_VALUES,
            "ambiguous",
        ),
        blocking_reason=_as_optional_str(interpretation.get("blocking_reason")),
        resolved_target=target["resolved_target"],
        target_status=target["target_status"],
        target_source=target["target_source"],
        explicit_constraints=_as_str_list(interpretation.get("explicit_constraints")),
        suggested_category_focus=_as_str_list(
            interpretation.get("suggested_category_focus")
        ),
        retrieval_hints=_as_str_list(interpretation.get("retrieval_hints")),
        relevant_memory_fragments=_as_str_list(
            interpretation.get("relevant_memory_fragments")
        ),
        request_contract=_build_request_contract_slice(metadata),
    )


def write_intent_brief_seed(metadata: Dict[str, Any]) -> None:
    """Write the one-hop pre-graph seed used by working-memory fold."""
    _ensure_turn_interpretation_metadata(metadata)
    metadata[METADATA_KEY_INTENT_BRIEF_SEED] = dict(
        build_working_memory_intent_brief(metadata)
    )


def ensure_intent_brief_seed_present(metadata: Dict[str, Any]) -> None:
    """Ensure the seed exists without overwriting an existing valid seed."""
    _ensure_turn_interpretation_metadata(metadata)
    existing = metadata.get(METADATA_KEY_INTENT_BRIEF_SEED)
    if isinstance(existing, Mapping):
        return
    write_intent_brief_seed(metadata)


__all__ = [
    "METADATA_KEY_CLASSIFIER_RAW_RESPONSE",
    "METADATA_KEY_INTENT_BRIEF_SEED",
    "METADATA_KEY_INTENT_TARGET_CONTINUITY",
    "METADATA_KEY_INTENT_TARGET_RESOLUTION",
    "METADATA_KEY_REQUEST_CONTRACT",
    "METADATA_KEY_TURN_INTERPRETATION",
    "RequestContractSlice",
    "TargetSlice",
    "WorkingMemoryIntentBrief",
    "build_working_memory_intent_brief",
    "ensure_intent_brief_seed_present",
    "write_intent_brief_seed",
]
