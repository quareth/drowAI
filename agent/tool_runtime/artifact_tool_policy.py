"""Artifact tool metadata policy for planner/catalog gating.

This module preserves task-scoped artifact signal extraction for telemetry and
legacy metadata, while `artifact.search` and `artifact.read` remain hidden from
LLM-facing planner catalogs. The tools stay registered and directly callable by
internal runtime/backend flows, but exposure policy must not add them to model
selection lists.

Text-signal source (runner_control follow-up, Fix 1):

The artifact-id collector and evidence-gap detector read their free-form
text signal exclusively from structured metadata fields and the
classifier-derived ``intent_brief`` (``resolved_user_intent``,
``next_operational_goal``, ``success_condition``,
``explicit_constraints``, ``relevant_memory_fragments``,
``retrieval_hints``). The legacy ``history`` transcript parameter has
been removed from both public functions; the brief carries equivalent
classifier-derived signal with none of the transcript fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence

ARTIFACT_SEARCH_TOOL_ID = "artifact.search"
ARTIFACT_READ_TOOL_ID = "artifact.read"
_ARTIFACT_TOOL_IDS = frozenset({ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID})

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

_PRIOR_OUTPUT_PHRASES = (
    "evidence gap",
    "missing evidence",
    "prior output",
    "previous output",
    "saved output",
    "saved file",
    "earlier output",
    "past output",
)
_ARTIFACT_SIGNAL_TERMS = ("artifact", "saved", "output", "evidence", "prior", "previous")


@dataclass(frozen=True, slots=True)
class ArtifactToolExposure:
    """Resolved artifact-tool exposure decision for the current planning cycle."""

    allow_search: bool
    allow_read: bool
    has_persisted_artifacts: bool
    known_artifact_ids: tuple[str, ...]
    evidence_gap_signal: bool

    def as_metadata(self) -> dict[str, Any]:
        """Return a JSON-serializable payload for planner metadata/debugging."""
        return {
            "allow_search": self.allow_search,
            "allow_read": self.allow_read,
            "has_persisted_artifacts": self.has_persisted_artifacts,
            "known_artifact_ids": list(self.known_artifact_ids),
            "evidence_gap_signal": self.evidence_gap_signal,
        }


def apply_artifact_tool_exposure(
    tool_ids: Sequence[str],
    *,
    exposure: ArtifactToolExposure,
    available_tool_ids: Optional[Sequence[str]] = None,
) -> list[str]:
    """Strip artifact DB tools from LLM-facing lists while preserving order."""
    _ = exposure
    _ = available_tool_ids
    return iter_non_artifact_tools(tool_ids)


def resolve_artifact_tool_exposure(
    *,
    task_id: Optional[int],
    metadata: Optional[Mapping[str, Any]],
    user_message: str = "",
    next_tool_hint: str = "",
    intent_brief: Optional[Mapping[str, Any]] = None,
) -> ArtifactToolExposure:
    """Resolve artifact evidence signals without making tools model-visible.

    Text-signal sources are the structured metadata fields
    (``next_tool_hint``, ``current_goal``, ``planner_reasoning``), the
    current-turn ``user_message`` / ``next_tool_hint`` hints, and the
    classifier-derived ``intent_brief`` (``resolved_user_intent``,
    ``next_operational_goal``, ``success_condition``,
    ``explicit_constraints``, ``relevant_memory_fragments``,
    ``retrieval_hints``). The legacy ``history`` transcript parameter
    is gone — downstream callers pass the brief instead.
    """
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}
    brief_mapping = (
        intent_brief
        if isinstance(intent_brief, Mapping)
        else None
    )
    known_artifact_ids = tuple(
        _collect_known_artifact_ids(
            metadata_mapping,
            intent_brief=brief_mapping,
            user_message=user_message,
            next_tool_hint=next_tool_hint,
        )
    )
    evidence_gap_signal = _detect_evidence_gap_signal(
        metadata_mapping,
        intent_brief=brief_mapping,
        user_message=user_message,
        next_tool_hint=next_tool_hint,
    )
    _ = task_id
    return ArtifactToolExposure(
        allow_search=False,
        allow_read=False,
        has_persisted_artifacts=False,
        known_artifact_ids=known_artifact_ids,
        evidence_gap_signal=evidence_gap_signal,
    )


def resolve_and_apply_exposure(
    *,
    context: dict[str, Any],
    resolved_tools: list[str],
    available_tool_ids: list[str],
    user_message: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """Resolve artifact exposure from context and apply it to a tool list.

    The classifier-derived ``intent_brief`` is read from
    ``context["intent_brief"]`` (a Mapping). The ``context``
    dict no longer exposes a ``history`` transcript channel to this
    policy — any poisoned ``history`` key in ``context`` is ignored.
    """
    exposure_payload = context.get("artifact_tool_exposure")
    exposure: ArtifactToolExposure
    if isinstance(exposure_payload, dict):
        exposure = ArtifactToolExposure(
            allow_search=bool(exposure_payload.get("allow_search")),
            allow_read=bool(exposure_payload.get("allow_read")),
            has_persisted_artifacts=bool(exposure_payload.get("has_persisted_artifacts")),
            known_artifact_ids=tuple(
                str(item)
                for item in (exposure_payload.get("known_artifact_ids") or [])
                if isinstance(item, str) and item.strip()
            ),
            evidence_gap_signal=bool(exposure_payload.get("evidence_gap_signal")),
        )
    else:
        task_id_value = context.get("task_id")
        try:
            parsed_task_id = int(task_id_value) if task_id_value is not None else None
        except Exception:
            parsed_task_id = None
        brief_payload = context.get("intent_brief")
        brief_mapping = brief_payload if isinstance(brief_payload, Mapping) else None
        exposure = resolve_artifact_tool_exposure(
            task_id=parsed_task_id,
            metadata=context,
            user_message=str(user_message or ""),
            next_tool_hint=str(context.get("next_tool_hint") or ""),
            intent_brief=brief_mapping,
        )
        context["artifact_tool_exposure"] = exposure.as_metadata()

    filtered_tools = apply_artifact_tool_exposure(
        resolved_tools,
        exposure=exposure,
        available_tool_ids=available_tool_ids,
    )
    return filtered_tools, exposure.as_metadata()


def task_has_persisted_artifacts(task_id: Optional[int]) -> bool:
    """Return whether at least one persisted artifact exists for the task."""
    try:
        parsed_task_id = int(task_id) if task_id is not None else 0
    except Exception:
        return False
    if parsed_task_id <= 0:
        return False

    try:
        from backend.database import SessionLocal
        from backend.services.artifact.memory_service import ArtifactMemoryService
    except Exception:
        return False

    db = None
    try:
        db = SessionLocal()
        service = ArtifactMemoryService(db)
        return service.task_has_persisted_artifacts(
            task_id=parsed_task_id,
        )
    except Exception:
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _collect_known_artifact_ids(
    metadata: Mapping[str, Any],
    *,
    intent_brief: Optional[Mapping[str, Any]] = None,
    user_message: str = "",
    next_tool_hint: str = "",
) -> list[str]:
    """Collect deterministic known artifact ids from state metadata and text context.

    Text signal sources are the structured metadata fields
    (``next_tool_hint``, ``current_goal``, ``planner_reasoning``), the
    current-turn hints (``user_message``, ``next_tool_hint``), and the
    classifier-derived ``intent_brief`` fields that can
    plausibly carry artifact IDs in free-form text: ``resolved_user_intent``,
    ``next_operational_goal``, ``success_condition``,
    ``explicit_constraints``, ``relevant_memory_fragments``, and
    ``retrieval_hints``.
    """
    collected: list[str] = []

    compact_result = metadata.get("last_tool_result_compact")
    if isinstance(compact_result, Mapping):
        _extract_ids_from_artifact_refs(compact_result.get("artifact_refs"), collected)

    working_memory = metadata.get("working_memory")
    if isinstance(working_memory, Mapping):
        collections = working_memory.get("collections")
        if isinstance(collections, list):
            for item in collections:
                if not isinstance(item, Mapping):
                    continue
                artifact_ref = item.get("artifact_ref")
                _extract_ids_from_artifact_refs([artifact_ref], collected)

    explicit_lists = (
        metadata.get("known_artifact_ids"),
        metadata.get("artifact_ids"),
    )
    for raw_list in explicit_lists:
        if isinstance(raw_list, list):
            for raw_id in raw_list:
                _append_if_artifact_id(collected, raw_id)

    text_candidates: list[str] = []
    text_candidates.extend(
        [
            str(user_message or ""),
            str(next_tool_hint or ""),
            str(metadata.get("next_tool_hint") or ""),
            str(metadata.get("current_goal") or ""),
            str(metadata.get("planner_reasoning") or ""),
        ]
    )
    text_candidates.extend(_brief_text_candidates(intent_brief))
    for text in text_candidates:
        for match in _UUID_RE.findall(text):
            _append_if_artifact_id(collected, match)

    return collected


def _brief_text_candidates(
    intent_brief: Optional[Mapping[str, Any]],
) -> List[str]:
    """Project the brief into a flat list of text candidates for scanning.

    Emits one candidate per scalar brief field and one per list entry
    for the list-shaped fields. The returned list never contains
    ``None`` entries; empty strings are harmless downstream.
    """
    if not isinstance(intent_brief, Mapping):
        return []
    scalar_fields = (
        "resolved_user_intent",
        "next_operational_goal",
        "success_condition",
    )
    list_fields = (
        "explicit_constraints",
        "relevant_memory_fragments",
        "retrieval_hints",
    )
    candidates: List[str] = []
    for field_name in scalar_fields:
        value = intent_brief.get(field_name)
        if isinstance(value, str):
            candidates.append(value)
    for field_name in list_fields:
        value = intent_brief.get(field_name)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    candidates.append(item)
    return candidates


def _extract_ids_from_artifact_refs(raw_refs: Any, collected: list[str]) -> None:
    if not isinstance(raw_refs, list):
        return
    for item in raw_refs:
        if not isinstance(item, Mapping):
            continue
        _append_if_artifact_id(collected, item.get("artifact_id"))


def _append_if_artifact_id(collected: list[str], raw_value: Any) -> None:
    if not isinstance(raw_value, str):
        return
    value = raw_value.strip()
    if not value:
        return
    if not _UUID_RE.fullmatch(value):
        return
    if value not in collected:
        collected.append(value)


def _detect_evidence_gap_signal(
    metadata: Mapping[str, Any],
    *,
    intent_brief: Optional[Mapping[str, Any]] = None,
    user_message: str = "",
    next_tool_hint: str = "",
) -> bool:
    """Return True when context suggests prior artifact evidence should be consulted.

    Text signal sources mirror ``_collect_known_artifact_ids``: structured
    metadata, current-turn hints, and the classifier-derived
    ``intent_brief`` text fields. No transcript read.
    """
    text_chunks = [
        str(user_message or ""),
        str(next_tool_hint or ""),
        str(metadata.get("next_tool_hint") or ""),
        str(metadata.get("current_goal") or ""),
        str(metadata.get("planner_reasoning") or ""),
    ]
    text_chunks.extend(_brief_text_candidates(intent_brief))

    normalized = " ".join(text_chunks).lower()
    if not normalized.strip():
        return False
    if any(phrase in normalized for phrase in _PRIOR_OUTPUT_PHRASES):
        return True
    if "artifact" in normalized and any(term in normalized for term in _ARTIFACT_SIGNAL_TERMS):
        return True
    return False


def is_artifact_tool(tool_id: str) -> bool:
    """Return True when tool id belongs to artifact retrieval tools."""
    return str(tool_id) in _ARTIFACT_TOOL_IDS


def iter_non_artifact_tools(tool_ids: Iterable[str]) -> list[str]:
    """Return input tool ids with artifact tools stripped."""
    return [str(tool_id) for tool_id in tool_ids if not is_artifact_tool(str(tool_id))]


__all__ = [
    "ARTIFACT_READ_TOOL_ID",
    "ARTIFACT_SEARCH_TOOL_ID",
    "ArtifactToolExposure",
    "apply_artifact_tool_exposure",
    "is_artifact_tool",
    "iter_non_artifact_tools",
    "resolve_and_apply_exposure",
    "resolve_artifact_tool_exposure",
    "task_has_persisted_artifacts",
]
