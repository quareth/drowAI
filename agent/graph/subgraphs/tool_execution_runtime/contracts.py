"""Shared typed payload contracts for tool-execution runtime modules.

Phase 1 introduces this file as a neutral schema boundary only. Runtime
behavior remains owned by the existing tool execution facade until later
extraction phases delegate logic into this package.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

ApprovalAction = Literal["approve", "skip", "edit"]


class ApprovalPayload(TypedDict, total=False):
    """Normalized approval decision payload."""

    action: ApprovalAction
    edited_parameters: dict[str, Any]
    user_note: str


class RuntimeStreamIdentity(TypedDict, total=False):
    """Canonical stream identity fields carried across runtime modules."""

    conversation_id: str
    turn_id: str
    turn_sequence: int
    tool_call_id: str


class DispatchCacheEntry(TypedDict, total=False):
    """Replayable cache payload for idempotent dispatch reapplication."""

    last_tool_result_compact: dict[str, Any]
    last_tool_result: dict[str, Any]
    tool_history_entry: dict[str, Any]
    action_record: dict[str, Any]
    tool_execution_history: list[dict[str, Any]]
    current_scan_phase: Any
    tool_catalog: list[dict[str, Any]]
    validation_errors: list[Any]
    observation_text: str
    reasoning_additions: list[str]
    exec_record: dict[str, Any]
