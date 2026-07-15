"""Shared text utilities for prompt builders under ``core/prompts/builders``.

This module hosts tiny, pure helpers intended for cross-builder reuse so we
keep prompt-builder modules DRY without pushing single-use sugar into the
broader ``core/prompts/constants`` surface. Helpers may have a single
consumer at any given moment; living here means any future builder can find
and adopt them without rebuilding the same logic locally.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple


_REFERENCED_PRIOR_TURNS_LABEL = "Referenced Prior Turns:"


def strip_referenced_prior_turns_label(text: Optional[str]) -> str:
    """Strip the canonical ``Referenced Prior Turns:`` label prefix.

    The bundle-projection helpers prepend a heading like
    ``Referenced Prior Turns:\n...`` so callers can either render the
    section themselves (with their own heading) or splice the body
    directly into another section. Builders that splice need the body
    only; this helper removes the heading without touching content.
    """
    if not text:
        return ""
    normalized = str(text).strip()
    if normalized.startswith(_REFERENCED_PRIOR_TURNS_LABEL):
        return normalized[len(_REFERENCED_PRIOR_TURNS_LABEL):].strip()
    return normalized


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Coerce supported values into a mapping view (mapping/Pydantic/object)."""
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, Mapping):
        return value_dict
    return {}


def _read_field(source: Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping or an attribute from a state object."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def derive_user_input_and_goal(facts: Any) -> Tuple[str, str]:
    """Return ``(verbatim_user_input, classifier_derived_user_goal)``.

    The verbatim input is ``facts.message`` -- whatever the user last sent
    (a sentence, a shell paste, a question, "ok", etc.). It is NOT the same
    as the user's goal: when the message is a paste/transcript, treating it
    as the goal misleads downstream prompts into inventing imperatives the
    user never asked for.

    The derived goal comes from the classifier brief folded into working
    memory at ``metadata.working_memory.intent_brief``:
    ``original_goal`` preferred as the immutable turn anchor,
    ``resolved_user_intent`` as fallback, then ``overall_goal``. Either
    side of the tuple may be empty; callers must skip rendering the
    corresponding section when its value is empty rather than emit a
    placeholder that the LLM might interpret as an imperative.
    """
    user_input = str(_read_field(facts, "message", "") or "").strip()
    metadata = _as_mapping(_read_field(facts, "metadata", {}))
    intent_brief = _as_mapping(
        _as_mapping(metadata.get("working_memory", {})).get("intent_brief", {})
    )
    derived = (
        str(intent_brief.get("original_goal") or "").strip()
        or str(intent_brief.get("resolved_user_intent") or "").strip()
        or str(intent_brief.get("overall_goal") or "").strip()
    )
    return user_input, derived


__all__ = [
    "derive_user_input_and_goal",
    "strip_referenced_prior_turns_label",
]
