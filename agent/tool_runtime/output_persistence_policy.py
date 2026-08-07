"""Side-effect-free output persistence decisions for tool executions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.tool_runtime.workspace_artifacts import should_persist_workspace_artifact
from runtime_shared.shell_capabilities import (
    SHELL_EXEC_TOOL_ID,
    SHELL_SESSION_TOOL_IDS,
    SHELL_WRITE_STDIN_TOOL_ID,
    ShellCapability,
    normalize_shell_capability,
    resolve_shell_start_capability,
)


@dataclass(frozen=True, slots=True)
class OutputPersistenceDecision:
    """Eligibility for existing durable output paths for one tool result."""

    is_shell_call: bool
    originating_capability: ShellCapability | None
    persist_workspace_artifact: bool
    assessment_evidence_eligible: bool
    knowledge_eligible: bool

    @property
    def retain_durable_output(self) -> bool:
        """Return whether reusable output may enter graph and memory state."""

        return not self.is_shell_call or self.assessment_evidence_eligible


def resolve_output_persistence(
    tool_id: object,
    result_metadata: Mapping[str, Any] | None = None,
) -> OutputPersistenceDecision:
    """Resolve persistence from the selected alias or retained session metadata."""

    normalized_tool_id = str(tool_id or "").strip()
    is_shell_call = normalized_tool_id in SHELL_SESSION_TOOL_IDS
    capability = resolve_shell_start_capability(normalized_tool_id)
    if normalized_tool_id == SHELL_EXEC_TOOL_ID:
        capability = ShellCapability.ASSESSMENT
    elif normalized_tool_id == SHELL_WRITE_STDIN_TOOL_ID:
        capability = _capability_from_result_metadata(result_metadata)

    if is_shell_call:
        assessment_eligible = capability is ShellCapability.ASSESSMENT
        return OutputPersistenceDecision(
            is_shell_call=True,
            originating_capability=capability,
            persist_workspace_artifact=(
                assessment_eligible
                and should_persist_workspace_artifact(normalized_tool_id)
            ),
            assessment_evidence_eligible=assessment_eligible,
            knowledge_eligible=assessment_eligible,
        )

    return OutputPersistenceDecision(
        is_shell_call=False,
        originating_capability=None,
        persist_workspace_artifact=should_persist_workspace_artifact(
            normalized_tool_id
        ),
        assessment_evidence_eligible=True,
        knowledge_eligible=True,
    )


def _capability_from_result_metadata(
    result_metadata: Mapping[str, Any] | None,
) -> ShellCapability | None:
    """Read the bounded runtime-session capability from a result mapping."""

    if not isinstance(result_metadata, Mapping):
        return None
    metadata = result_metadata.get("metadata")
    metadata_view = metadata if isinstance(metadata, Mapping) else result_metadata
    runtime_session = metadata_view.get("runtime_session")
    if not isinstance(runtime_session, Mapping):
        return None
    return normalize_shell_capability(runtime_session.get("originating_capability"))


__all__ = ["OutputPersistenceDecision", "resolve_output_persistence"]
