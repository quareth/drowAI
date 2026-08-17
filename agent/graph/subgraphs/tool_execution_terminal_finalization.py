"""Finalize durable assessment-shell evidence at the terminal session boundary.

This module adapts a completed shell-session aggregate to the existing shared
artifact/provenance helpers. Utility sessions never enter this boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from agent.tool_runtime import ToolExecutionOutcome
from agent.tool_runtime.output_persistence_policy import resolve_output_persistence
from agent.tool_runtime.workspace_artifacts import should_persist_workspace_artifact
from backend.services.metrics.utils import safe_inc

from .tool_execution_runtime.artifact_and_provenance import (
    collect_persistable_tool_artifact_paths,
    collect_provenance_artifact_refs,
    enrich_artifact_refs_with_provenance,
    finalize_provenance_execution,
    get_provenance_service,
    resolve_execution_artifact_workspace,
)
from .tool_execution_runtime.result_state_projection import (
    project_artifact_refs_for_memory,
)

logger = logging.getLogger(__name__)


def finalize_terminal_shell_assessment(
    aggregate: Mapping[str, Any],
    *,
    session: Mapping[str, Any],
    transcript: Mapping[str, Any],
    facts: Any,
) -> Mapping[str, Any]:
    """Finalize one assessment execution and attach its stable artifact refs."""

    tool_name = str(session.get("originating_tool_id") or "").strip()
    persistence = resolve_output_persistence(tool_name)
    if not persistence.assessment_evidence_eligible:
        return aggregate

    rows = [dict(row) for row in aggregate.get("results", []) if isinstance(row, Mapping)]
    if not rows:
        return aggregate
    terminal_row = dict(rows[-1])
    raw_compact = terminal_row.get("compact_tool_result")
    if not isinstance(raw_compact, Mapping):
        return aggregate
    terminal_compact = dict(raw_compact)

    execution_id = str(session.get("provenance_execution_id") or "").strip() or None
    tool_call_id = (
        str(session.get("originating_tool_call_id") or "").strip() or None
    )
    turn_sequence = facts.metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        turn_sequence = None
    workspace_path = resolve_execution_artifact_workspace(
        workspace_path=(
            str(facts.metadata.get("workspace_path") or "").strip() or None
        ),
        facts=facts,
    )

    persisted_refs: list[dict[str, Any]] = []
    if execution_id is not None:
        command = str(transcript.get("originating_command") or "").strip()
        parameters = {"command": command}
        if bool(transcript.get("interactive")):
            parameters["interactive"] = True
        result = dict(terminal_compact)
        result["command_text"] = command or None
        outcome = ToolExecutionOutcome(
            tool_id=tool_name,
            parameters=parameters,
            catalog=[],
            result=result,
            summary=str(terminal_compact.get("summary") or ""),
            reasoning=[],
            duration=0.0,
        )
        persisted_refs = finalize_provenance_execution(
            get_provenance_service_fn=lambda: get_provenance_service(logger=logger),
            execution_id=execution_id,
            outcome=outcome,
            facts=facts,
            tool_name=tool_name,
            tool_call_id=tool_call_id or "",
            turn_sequence=turn_sequence,
            workspace_path=workspace_path,
            artifact_path=None,
            should_persist_artifact_outputs_fn=should_persist_workspace_artifact,
            build_command_for_display_fn=lambda _tool, params: str(
                params.get("command") or ""
            ),
            collect_persistable_tool_artifact_paths_fn=(
                collect_persistable_tool_artifact_paths
            ),
            collect_provenance_artifact_refs_fn=(
                collect_provenance_artifact_refs
            ),
            logger=logger,
            safe_inc_fn=safe_inc,
            persistence_decision=persistence,
        )

    artifact_refs = project_artifact_refs_for_memory(
        compact_result=terminal_compact,
        raw_artifacts=terminal_compact.get("artifacts"),
        artifact_path=None,
        persisted_artifact_refs=persisted_refs,
        retain_durable_output=True,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        execution_id=execution_id,
        turn_sequence=turn_sequence,
        enrich_artifact_refs_with_provenance_fn=(
            enrich_artifact_refs_with_provenance
        ),
    )
    if artifact_refs:
        terminal_compact["artifact_refs"] = artifact_refs
    terminal_row["compact_tool_result"] = terminal_compact
    rows[-1] = terminal_row
    finalized = dict(aggregate)
    finalized["results"] = rows
    return finalized


__all__ = ["finalize_terminal_shell_assessment"]
