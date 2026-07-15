"""Shared route-label translation helpers for prompt and classifier code.

This module owns the single prompt-facing mapping from internal route ids
to LLM-visible labels used across prompt builders and classifier logic.
"""

from __future__ import annotations

from typing import Any


def llm_facing_route_label(value: Any) -> str:
    """Translate internal capability labels into LLM-facing aliases."""
    lowered = str(value or "").strip().lower()
    if lowered == "simple_tool_execution":
        return "direct_executor"
    if lowered == "deep_reasoning":
        return "plan_executor"
    if lowered in {"normal_chat", "respond_only"}:
        return "simple_chat"
    return str(value)


__all__ = ["llm_facing_route_label"]
