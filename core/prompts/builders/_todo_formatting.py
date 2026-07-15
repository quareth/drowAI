"""Shared todo formatting helpers for prompt builders.

This module centralizes prompt-facing todo extraction and status
normalization so builder modules do not keep duplicate copies.
"""

from __future__ import annotations

from typing import Any


def extract_todo_description(item: Any) -> str:
    """Extract todo description from supported prompt representations."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        description = item.get("description") or item.get("text")
        return str(description).strip() if description else ""
    if hasattr(item, "description"):
        description = getattr(item, "description", "")
        return str(description).strip() if description else ""
    return ""


def normalize_progress_status(raw: Any) -> str:
    """Normalize todo status values to prompt-facing progression states."""
    value = raw
    if value is not None and hasattr(value, "value"):
        value = getattr(value, "value")
    status = str(value or "").strip().lower()
    if status == "in_progress":
        return "in_progress"
    if status in {"complete_positive", "complete_negative", "completed"}:
        return "completed"
    if status in {"skipped", "exhausted"}:
        return "skipped"
    return "pending"


def extract_todo_status(item: Any) -> str:
    """Extract normalized progress status from supported todo item shapes."""
    if isinstance(item, dict):
        return normalize_progress_status(item.get("status"))
    return normalize_progress_status(getattr(item, "status", None))


def to_progress_marker(status: str) -> str:
    """Convert normalized status to explicit marker used in prompts."""
    if status == "in_progress":
        return "[in_progress]"
    if status == "completed":
        return "[completed]"
    if status == "skipped":
        return "[skipped]"
    return "[pending]"


__all__ = [
    "extract_todo_description",
    "extract_todo_status",
    "normalize_progress_status",
    "to_progress_marker",
]
