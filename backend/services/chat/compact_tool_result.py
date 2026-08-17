"""Normalize compact tool-result payloads for chat persistence and streaming.

This module owns the backend chat boundary for canonical compact envelopes and
their supported shell-lifecycle extensions. It does not publish events or write
to persistence directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.compression.schema import CompactToolOutput

_SHELL_LIFECYCLE_EXTENSION_KEYS = (
    "process_status",
    "session_status",
    "interaction_boundary",
    "session_id",
    "close_reason",
    "lifecycle_event",
    "output_persistence",
    "error",
    "failure_category",
)


def normalize_compact_tool_result(
    *,
    tool: str,
    status: str,
    exit_code: Any,
    summary: Any,
    error: Any,
    compact_tool_result: Any,
) -> dict[str, Any]:
    """Return a canonical compact envelope with supported lifecycle fields."""

    source_payload = (
        compact_tool_result if isinstance(compact_tool_result, Mapping) else {}
    )
    summary_payload = summary if isinstance(summary, Mapping) else {}

    try:
        normalized_exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        normalized_exit_code = None

    source_summary = source_payload.get("summary")
    fallback_summary = summary_payload.get("summary")
    normalized_summary = next(
        (
            value
            for value in (source_summary, fallback_summary, summary)
            if isinstance(value, str) and value
        ),
        "",
    )

    merged_payload: dict[str, Any] = {
        "schema_version": source_payload.get("schema_version", "2.0"),
        "tool": source_payload.get("tool", tool),
        "status": source_payload.get("status", status),
        "success": source_payload.get(
            "success",
            str(status).lower() in {"success", "ok"},
        ),
        "exit_code": normalized_exit_code,
        "summary": normalized_summary,
        "key_findings": source_payload.get(
            "key_findings",
            summary_payload.get("key_findings"),
        ),
        "errors": source_payload.get("errors", summary_payload.get("errors")),
        "report_recommendations": source_payload.get(
            "report_recommendations",
            summary_payload.get("report_recommendations"),
        ),
        "structured_signals": source_payload.get(
            "structured_signals",
            summary_payload.get("structured_signals"),
        ),
        "decision_evidence": source_payload.get(
            "decision_evidence",
            summary_payload.get("decision_evidence"),
        ),
        "lossiness_risk": source_payload.get(
            "lossiness_risk",
            summary_payload.get("lossiness_risk"),
        ),
        "artifact_refs": source_payload.get("artifact_refs") or [],
        "compression": source_payload.get("compression"),
    }
    if error and not merged_payload.get("errors"):
        merged_payload["errors"] = [str(error)]

    normalized = CompactToolOutput.from_dict(merged_payload).to_dict()
    for key in _SHELL_LIFECYCLE_EXTENSION_KEYS:
        value = source_payload.get(key)
        if value is not None:
            normalized[key] = value
    return normalized


def build_terminal_shell_compact_result(
    *,
    tool: str,
    status: str,
    process_status: str,
    session_id: str | None,
    summary: str,
    exit_code: int | None = None,
    error: str | None = None,
    close_reason: str | None = None,
    lifecycle_event: str | None = None,
    failure_category: str | None = None,
    output_persistence: str | None = None,
) -> dict[str, Any]:
    """Build one schema-valid terminal shell lifecycle compact result."""

    lifecycle = {
        "process_status": process_status,
        "session_status": "closed",
        "interaction_boundary": "terminal",
        "session_id": session_id,
        "close_reason": close_reason,
        "lifecycle_event": lifecycle_event,
        "output_persistence": output_persistence,
        "error": error,
        "failure_category": failure_category,
        "lossiness_risk": "low",
    }
    return normalize_compact_tool_result(
        tool=tool,
        status=status,
        exit_code=exit_code,
        summary=summary,
        error=error,
        compact_tool_result={
            key: value for key, value in lifecycle.items() if value is not None
        },
    )


__all__ = [
    "build_terminal_shell_compact_result",
    "normalize_compact_tool_result",
]
