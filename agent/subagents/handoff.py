"""Canonical subagent handoff model, boundary normalization, and JSON schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.skills.contracts import MAX_REQUESTED_SKILLS
from core.skills.identifiers import (
    MAX_SKILL_ID_CHARACTERS,
    SKILL_ID_PATTERN,
    normalize_skill_ids,
)

_HANDOFF_FIELDS = frozenset(
    {"agent_handoff", "subagent", "objective", "skill_ids"}
)


class AgentHandoffEntry(BaseModel):
    """Bounded delegation entry authored by post-action reasoning."""

    agent_handoff: Literal["required"] = Field(
        ...,
        description="Marker indicating that a subagent assignment is required.",
    )
    subagent: str = Field(
        ...,
        min_length=1,
        description="Registered subagent identifier selected by PAR.",
    )
    objective: str = Field(
        ...,
        min_length=1,
        description="Bounded natural-language assignment brief for the subagent.",
    )
    skill_ids: tuple[str, ...] = Field(
        ...,
        max_length=MAX_REQUESTED_SKILLS,
        description="Eligible selectable built-in skill identifiers for this assignment.",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("subagent", "objective")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        """Reject blank delegation text while preserving the authored value."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("skill_ids", mode="before")
    @classmethod
    def _normalize_skill_ids(cls, value: Any) -> tuple[str, ...]:
        """Normalize canonical optional identifiers with stable deduplication."""

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("skill_ids must be an array")
        if len(value) > MAX_REQUESTED_SKILLS:
            raise ValueError(
                f"skill_ids contains more than {MAX_REQUESTED_SKILLS} items"
            )
        try:
            return normalize_skill_ids(value)
        except ValueError as exc:
            raise ValueError(
                "skill_ids contains a non-canonical identifier"
            ) from exc


def normalize_agent_handoff_entry(value: Any) -> dict[str, Any]:
    """Normalize one handoff at graph and backend ingestion boundaries."""
    if not isinstance(value, Mapping):
        return {}
    marker = value.get("agent_handoff")
    subagent = value.get("subagent")
    objective = value.get("objective")
    raw_skill_ids = value.get("skill_ids")
    if not isinstance(marker, str) or marker.strip().lower() != "required":
        return {}
    if not isinstance(subagent, str) or not subagent.strip():
        return {}
    if not isinstance(objective, str) or not objective.strip():
        return {}
    if not isinstance(raw_skill_ids, Sequence) or isinstance(
        raw_skill_ids, (str, bytes, bytearray)
    ):
        return {}
    if len(raw_skill_ids) > MAX_REQUESTED_SKILLS:
        return {}
    try:
        skill_ids = list(normalize_skill_ids(raw_skill_ids))
    except ValueError:
        return {}
    return {
        "agent_handoff": "required",
        "subagent": subagent.strip().lower(),
        "objective": objective.strip(),
        "skill_ids": skill_ids,
    }


def normalize_agent_handoff_entries(
    raw_handoffs: Any,
    *,
    max_handoffs: int | None = None,
    reject_invalid: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Filter an ordered handoff collection with stable first-occurrence dedupe."""
    if max_handoffs is not None and max_handoffs <= 0:
        return ()
    if isinstance(raw_handoffs, Mapping):
        candidates: Sequence[Any] = (raw_handoffs,)
    elif isinstance(raw_handoffs, Sequence) and not isinstance(
        raw_handoffs, (str, bytes, bytearray)
    ):
        candidates = raw_handoffs
    else:
        if reject_invalid:
            raise ValueError("invalid_handoff_plan")
        return ()

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for candidate in candidates:
        if reject_invalid and (
            not isinstance(candidate, Mapping)
            or set(candidate) != _HANDOFF_FIELDS
        ):
            raise ValueError("invalid_handoff_plan")
        entry = normalize_agent_handoff_entry(candidate)
        if not entry:
            if reject_invalid:
                raise ValueError("invalid_handoff_plan")
            continue
        identity = (
            entry["subagent"],
            entry["objective"],
            tuple(entry["skill_ids"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(entry)
        if max_handoffs is not None and len(normalized) >= max_handoffs:
            break
    return tuple(normalized)


def agent_handoff_entry_json_schema(
    subagent_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the strict LLM-visible handoff fragment for an optional registry."""
    subagent_schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if subagent_names is not None:
        normalized_names = list(
            dict.fromkeys(
                name.strip().lower()
                for name in subagent_names
                if isinstance(name, str) and name.strip()
            )
        )
        if normalized_names:
            subagent_schema["enum"] = normalized_names
    return {
        "type": "object",
        "properties": {
            "agent_handoff": {
                "type": "string",
                "enum": ["required"],
            },
            "subagent": subagent_schema,
            "objective": {
                "type": "string",
                "minLength": 1,
            },
            "skill_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": SKILL_ID_PATTERN,
                    "maxLength": MAX_SKILL_ID_CHARACTERS,
                },
                "maxItems": MAX_REQUESTED_SKILLS,
            },
        },
        "required": ["agent_handoff", "subagent", "objective", "skill_ids"],
        "additionalProperties": False,
    }


__all__ = [
    "AgentHandoffEntry",
    "agent_handoff_entry_json_schema",
    "normalize_agent_handoff_entries",
    "normalize_agent_handoff_entry",
]
