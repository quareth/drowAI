"""Core adapter contracts and deterministic context helpers.

This module defines:
- AdapterContext: normalized inputs for deterministic extraction.
- KnowledgeAdapter: protocol every semantic adapter must implement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..contracts import ObservationCreate

ArtifactReader = Callable[[str], str | None]


@dataclass(slots=True)
class AdapterContext:
    """Normalized input envelope used by deterministic semantic adapters."""

    user_id: int
    engagement_id: int
    task_id: int | None
    source_execution_id: str
    ingestion_run_id: str
    execution_payload: Mapping[str, Any]
    tool_metadata: Mapping[str, Any] = field(default_factory=dict)
    semantic_observations: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    semantic_evidence: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    artifact_summaries: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    evidence_archives: Sequence[Any] = field(default_factory=tuple)
    compact_output_hint: Mapping[str, Any] | None = None
    artifact_reader: ArtifactReader | None = None
    tenant_id: int | None = None

    def source_tool_name(self) -> str:
        """Return normalized source tool identifier if present."""
        execution = self.execution_payload.get("execution")
        if not isinstance(execution, Mapping):
            return ""
        return str(execution.get("tool_name") or "").strip()

    def capability_family(self) -> str:
        """Return normalized capability-family hint from execution metadata."""
        execution = self.execution_payload.get("execution")
        if not isinstance(execution, Mapping):
            return ""
        metadata = execution.get("execution_metadata")
        if not isinstance(metadata, Mapping):
            return ""
        value = metadata.get("capability_family")
        return str(value or "").strip()

    def select_authoritative_input_source(self) -> str:
        """Return deterministic extraction source priority for canonical authority."""
        if self.semantic_observations:
            return "semantic_observations"
        if self.tool_metadata:
            return "tool_metadata"
        if self._has_any_artifact_content():
            return "artifact_content"
        if self.compact_output_hint:
            return "compact_output"
        return "none"

    def _has_any_artifact_content(self) -> bool:
        for artifact in self.artifact_summaries:
            if not isinstance(artifact, Mapping):
                continue
            content = artifact.get("content_text")
            if isinstance(content, str) and content.strip():
                return True
        return self.artifact_reader is not None and len(self.artifact_summaries) > 0


class KnowledgeAdapter(Protocol):
    """Protocol contract for deterministic semantic adapters."""

    tool_names: tuple[str, ...]
    capability_families: tuple[str, ...]

    def supports(self, context: AdapterContext) -> bool:
        """Return True when this adapter can deterministically process the context."""

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        """Extract canonical observations from deterministic inputs."""
