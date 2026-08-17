"""Build bounded tool-outcome context shared across subagent handoffs.

This module projects canonical compact evidence plus the matching serialized
planner call into the small outcome shape consumed by subagent phase memory.
It never parses rendered prompts or raw shell output and does not own tool
execution, assignment routing, or recovery decisions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from agent.tool_runtime.batch.plan_view import (
    SerializedToolCallView,
    serialized_tool_calls_from_metadata,
)
from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    preferred_compact_evidence_row,
    select_compact_evidence_for_reasoning,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.shell_capabilities import SHELL_SESSION_START_TOOL_IDS


SUBAGENT_TOOL_OUTCOME_SECTION_HEADING = "Subagent Tool Outcome"
SUBAGENT_PRIOR_TOOL_OUTCOMES_CONTEXT_KEY = "prior_tool_outcomes"
_OUTCOME_LIST_LIMIT = 3
_OUTCOME_TEXT_LIMIT = 600
_SHELL_INVOCATION_FIELDS = ("command", "cwd", "interactive")


def latest_tool_outcome_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return the latest compact outcome with its canonical invocation."""

    evidence, _ = select_compact_evidence_for_reasoning(metadata)
    if evidence is None:
        return {}
    return project_tool_batch_outcome(
        evidence,
        tool_calls=serialized_tool_calls_from_metadata(metadata),
    )


def project_tool_batch_outcome(
    evidence: EvidenceView,
    *,
    tool_calls: Sequence[SerializedToolCallView] = (),
) -> dict[str, Any]:
    """Project one existing evidence batch without generating summaries."""

    calls_by_id = {
        call.tool_call_id: call for call in tool_calls if call.tool_call_id
    }
    if evidence.raw.get("execution_session_aggregate") is True:
        origin = evidence.rows[0] if evidence.rows else {}
        terminal = preferred_compact_evidence_row(evidence) or origin
        calls = [
            _project_tool_call(
                origin,
                result_row=terminal,
                planned_call=_match_planned_call(origin, tool_calls, calls_by_id),
            )
        ] if origin else []
    else:
        calls = [
            _project_tool_call(
                row,
                planned_call=_match_planned_call(row, tool_calls, calls_by_id),
            )
            for row in evidence.rows
        ]
    calls = [call for call in calls if call.get("tool")]
    outcome: dict[str, Any] = {
        "status": str(evidence.status or "unknown"),
        "success": bool(evidence.success),
        "calls": calls,
    }
    deferred = _bounded_outcome_list(evidence.deferred_followups)
    if deferred:
        outcome["deferred_followups"] = deferred
    return outcome


def outcome_section_payload(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize one projected outcome for the existing phase ledger."""

    return {
        "sections": [
            {
                "heading": SUBAGENT_TOOL_OUTCOME_SECTION_HEADING,
                "body": json.dumps(
                    dict(outcome),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ]
    }


def _match_planned_call(
    row: Mapping[str, Any],
    tool_calls: Sequence[SerializedToolCallView],
    calls_by_id: Mapping[str, SerializedToolCallView],
) -> SerializedToolCallView | None:
    call_id = str(row.get("tool_call_id") or "").strip()
    if call_id and call_id in calls_by_id:
        return calls_by_id[call_id]
    if len(tool_calls) != 1:
        return None
    candidate = tool_calls[0]
    row_tool_id = str(row.get("tool_id") or row.get("tool") or "").strip()
    return candidate if not row_tool_id or candidate.tool_id == row_tool_id else None


def _project_tool_call(
    origin_row: Mapping[str, Any],
    *,
    result_row: Mapping[str, Any] | None = None,
    planned_call: SerializedToolCallView | None = None,
) -> dict[str, Any]:
    terminal = result_row or origin_row
    compact = terminal.get("compact_tool_result")
    compact_result = compact if isinstance(compact, Mapping) else {}
    success_value = (
        terminal.get("success")
        if "success" in terminal
        else compact_result.get("success", False)
    )
    tool_id = str(
        origin_row.get("tool_id")
        or origin_row.get("tool")
        or compact_result.get("tool")
        or ""
    ).strip()
    call: dict[str, Any] = {
        "tool": tool_id,
        "intent": _bounded_outcome_text(origin_row.get("intent")),
        "invocation": _project_invocation(tool_id, planned_call),
        "status": str(
            terminal.get("status") or compact_result.get("status") or "unknown"
        ).strip(),
        "success": bool(success_value),
        "failure_category": _bounded_outcome_text(
            terminal.get("failure_category") or origin_row.get("failure_category")
        ),
        "summary": _bounded_outcome_text(
            compact_result.get("summary") or terminal.get("summary")
        ),
    }
    exit_code = compact_result.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        call["exit_code"] = exit_code
    for source_key, target_key in (
        ("key_findings", "key_findings"),
        ("errors", "errors"),
        ("artifact_refs", "artifact_refs"),
    ):
        values = _bounded_outcome_list(compact_result.get(source_key))
        if values:
            call[target_key] = values
    for key in ("process_status", "session_status"):
        value = _bounded_outcome_text(compact_result.get(key))
        if value:
            call[key] = value
    if str(compact_result.get("process_status") or "").strip().lower() == "running":
        session_id = _bounded_outcome_text(compact_result.get("session_id"))
        if session_id:
            call["session_id"] = session_id
        if "stdin_available" in compact_result:
            call["stdin_available"] = bool(compact_result.get("stdin_available"))
    return {
        key: value
        for key, value in call.items()
        if value not in (None, "", [], {})
    }


def _project_invocation(
    tool_id: str,
    planned_call: SerializedToolCallView | None,
) -> dict[str, Any]:
    if planned_call is None or tool_id not in SHELL_SESSION_START_TOOL_IDS:
        return {}
    projected = {
        key: planned_call.parameters[key]
        for key in _SHELL_INVOCATION_FIELDS
        if key in planned_call.parameters
    }
    masked = mask_durable_secrets(projected, source="subagent_tool_invocation")
    if not isinstance(masked, Mapping):
        return {}
    bounded: dict[str, Any] = {}
    for key in _SHELL_INVOCATION_FIELDS:
        value = masked.get(key)
        if isinstance(value, str):
            bounded[key] = _bounded_outcome_text(value)
        elif isinstance(value, bool):
            bounded[key] = value
    return bounded


def _bounded_outcome_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= _OUTCOME_TEXT_LIMIT:
        return text
    return f"{text[:_OUTCOME_TEXT_LIMIT].rstrip()}...[truncated]"


def _bounded_outcome_list(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    projected: list[Any] = []
    for item in value[:_OUTCOME_LIST_LIMIT]:
        if isinstance(item, Mapping):
            projected.append(dict(item))
        else:
            text = _bounded_outcome_text(item)
            if text:
                projected.append(text)
    return projected


__all__ = [
    "SUBAGENT_PRIOR_TOOL_OUTCOMES_CONTEXT_KEY",
    "SUBAGENT_TOOL_OUTCOME_SECTION_HEADING",
    "latest_tool_outcome_from_metadata",
    "outcome_section_payload",
    "project_tool_batch_outcome",
]
