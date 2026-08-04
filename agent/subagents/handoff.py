"""Canonical subagent handoff model, boundary normalization, and JSON schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HANDOFF_FIELDS = frozenset({"agent_handoff", "subagent", "objective"})


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

    model_config = ConfigDict(extra="forbid")

    @field_validator("subagent", "objective")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        """Reject blank delegation text while preserving the authored value."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def normalize_agent_handoff_entry(value: Any) -> dict[str, str]:
    """Normalize one handoff at graph and backend ingestion boundaries."""
    if not isinstance(value, Mapping):
        return {}
    marker = value.get("agent_handoff")
    subagent = value.get("subagent")
    objective = value.get("objective")
    if not isinstance(marker, str) or marker.strip().lower() != "required":
        return {}
    if not isinstance(subagent, str) or not subagent.strip():
        return {}
    if not isinstance(objective, str) or not objective.strip():
        return {}
    return {
        "agent_handoff": "required",
        "subagent": subagent.strip().lower(),
        "objective": objective.strip(),
    }


def normalize_agent_handoff_entries(
    raw_handoffs: Any,
    *,
    max_handoffs: int | None = None,
    reject_invalid: bool = False,
) -> tuple[dict[str, str], ...]:
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

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
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
        identity = (entry["subagent"], entry["objective"])
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
        },
        "required": ["agent_handoff", "subagent", "objective"],
        "additionalProperties": False,
    }


__all__ = [
    "AgentHandoffEntry",
    "agent_handoff_entry_json_schema",
    "normalize_agent_handoff_entries",
    "normalize_agent_handoff_entry",
]
