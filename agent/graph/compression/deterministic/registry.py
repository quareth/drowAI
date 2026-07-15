"""In-memory registry for pure deterministic compression adapters.

This module maps tool ids and family prefixes to callable deterministic
adapters. It intentionally imports only local compression contracts so registry
import stays cheap and does not load tool modules.
"""

from __future__ import annotations

from typing import TypeAlias

from .contracts import (
    CompressionInput,
    DeterministicCompressionAdapter,
    DeterministicCompressionResult,
)


DeterministicCompressor: TypeAlias = DeterministicCompressionAdapter

_NO_ADAPTER_REASON = "no_deterministic_adapter"
_ADAPTERS: dict[str, DeterministicCompressor] = {}


def register_adapter(tool_id: str, adapter: DeterministicCompressor) -> None:
    """Register an adapter for an exact tool id or a family prefix."""

    if not callable(adapter):
        raise TypeError("adapter must be callable")
    _ADAPTERS[_normalize_adapter_key(tool_id)] = adapter


def get_adapter(tool_name: str) -> DeterministicCompressor | None:
    """Return the best adapter for a tool name, preferring exact matches."""

    normalized_tool_name = _normalize_tool_name(tool_name)
    exact_adapter = _ADAPTERS.get(normalized_tool_name)
    if exact_adapter is not None:
        return exact_adapter

    for family_prefix in _family_prefixes(normalized_tool_name):
        adapter = _ADAPTERS.get(family_prefix)
        if adapter is not None:
            return adapter
    return None


def compress_deterministically(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Run a registered adapter or return an explicit no-result fallback."""

    adapter = get_adapter(input_data.tool_name)
    if adapter is None:
        return DeterministicCompressionResult.none(
            fallback_reason=_NO_ADAPTER_REASON,
        )

    try:
        result = adapter(input_data)
    except Exception:
        return DeterministicCompressionResult.none(
            fallback_reason="deterministic_adapter_error",
        )

    if not isinstance(result, DeterministicCompressionResult):
        return DeterministicCompressionResult.none(
            fallback_reason="invalid_deterministic_adapter_result",
        )
    return result


def _normalize_adapter_key(tool_id: str) -> str:
    """Normalize exact ids and family prefixes to dotted-boundary keys."""

    normalized = _normalize_tool_name(tool_id)
    if normalized.endswith(".*"):
        normalized = normalized[:-2]
    if normalized.endswith("."):
        normalized = normalized[:-1]
    if not normalized:
        raise ValueError("tool_id must not be empty")
    return normalized


def _normalize_tool_name(tool_name: str) -> str:
    """Return a stripped non-empty tool name."""

    if not isinstance(tool_name, str):
        raise TypeError("tool_name must be a string")
    normalized = tool_name.strip()
    if not normalized:
        raise ValueError("tool_name must not be empty")
    return normalized


def _family_prefixes(tool_name: str) -> tuple[str, ...]:
    """Return longest-to-shortest dotted family prefixes for a tool name."""

    parts = tool_name.split(".")
    return tuple(
        ".".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)
    )
