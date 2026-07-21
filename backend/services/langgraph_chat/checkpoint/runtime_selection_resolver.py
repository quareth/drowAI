"""Resolve authoritative runtime identity from persisted LangGraph state.

The resolver understands only explicit historical checkpoint locations. Modern
deployment identity is authoritative, duplicate mirrors must agree, and a
malformed versioned payload fails closed instead of falling back to mutable
provider/model labels. Live ownership, revision, credential, and egress checks
remain the responsibility of the LLM provider runtime services.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.services.llm_provider.types import ProviderConfigurationError
from core.llm.runtime_selection import (
    deployment_runtime_selection_identity,
    has_versioned_runtime_selection_marker,
    project_checkpoint_runtime_selection,
)

_VERSIONED_SELECTION_PATHS = (
    ("facts", "metadata", "llm_runtime_selection"),
    ("facts", "metadata", "graph_runtime_context", "llm_runtime_selection"),
    ("facts", "llm_runtime_selection"),
    ("facts", "graph_runtime_context", "llm_runtime_selection"),
    ("llm_runtime_selection",),
    ("graph_runtime_context", "llm_runtime_selection"),
)

_LEGACY_HINT_PATHS = (
    *_VERSIONED_SELECTION_PATHS,
    ("facts", "metadata"),
    ("facts", "metadata", "graph_runtime_context"),
    ("facts",),
    ("facts", "graph_runtime_context"),
    (),
    ("graph_runtime_context",),
)


def resolve_checkpoint_runtime_selection(values: Any) -> dict[str, Any] | None:
    """Return one safe checkpoint identity or fail on invalid/conflicting state."""

    if not isinstance(values, Mapping):
        return None

    versioned_candidates = [
        candidate
        for path in _VERSIONED_SELECTION_PATHS
        if has_versioned_runtime_selection_marker(
            candidate := _path_value(values, path)
        )
    ]
    if versioned_candidates:
        return _resolve_versioned_candidates(versioned_candidates)

    legacy_candidates = [
        hint
        for path in _LEGACY_HINT_PATHS
        if (hint := _legacy_hint(_path_value(values, path))) is not None
    ]
    return _resolve_legacy_candidates(legacy_candidates)


def _resolve_versioned_candidates(
    candidates: list[Any],
) -> dict[str, Any]:
    projected: list[dict[str, Any]] = []
    try:
        for candidate in candidates:
            payload = project_checkpoint_runtime_selection(candidate)
            if payload is None:
                raise ValueError("Versioned runtime selection is missing")
            projected.append(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderConfigurationError(
            "Checkpoint deployment runtime selection is invalid and cannot run; "
            "reselect an available LLM deployment, then retry resume."
        ) from exc

    identities = {
        deployment_runtime_selection_identity(payload) for payload in projected
    }
    if len(identities) != 1:
        raise ProviderConfigurationError(
            "Checkpoint contains conflicting deployment runtime selections and "
            "cannot run; reselect an available LLM deployment, then retry resume."
        )
    return projected[0]


def _resolve_legacy_candidates(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    providers = {
        str(candidate["provider"]).lower()
        for candidate in candidates
        if candidate.get("provider") is not None
    }
    models = {
        candidate["model"]
        for candidate in candidates
        if candidate.get("model") is not None
    }
    efforts = {
        candidate["reasoning_effort"]
        for candidate in candidates
        if candidate.get("reasoning_effort") is not None
    }
    if len(providers) > 1 or len(models) > 1 or len(efforts) > 1:
        raise ProviderConfigurationError(
            "Checkpoint contains conflicting legacy runtime selections and cannot "
            "run; reselect an available LLM deployment, then retry resume."
        )
    hint: dict[str, Any] = {}
    if providers:
        hint["provider"] = next(iter(providers))
    if models:
        hint["model"] = next(iter(models))
    if efforts:
        hint["reasoning_effort"] = next(iter(efforts))
    return hint or None


def _legacy_hint(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    provider = _non_empty_string(value.get("provider"))
    model = _non_empty_string(value.get("model"))
    if provider is None and model is None:
        return None
    hint: dict[str, Any] = {}
    if provider is not None:
        hint["provider"] = provider
    if model is not None:
        hint["model"] = model
    reasoning_effort = _non_empty_string(value.get("reasoning_effort"))
    if reasoning_effort is not None:
        hint["reasoning_effort"] = reasoning_effort
    return hint


def _path_value(root: Any, path: tuple[str, ...]) -> Any:
    value = root
    for key in path:
        if isinstance(value, Mapping):
            value = value.get(key)
        else:
            value = getattr(value, key, None)
        if value is None:
            return None
    return value


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = ["resolve_checkpoint_runtime_selection"]
