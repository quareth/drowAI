"""Adapter execution authority: registry dispatch, extraction pipeline, and statistics.

Scope:
- Resolve and dispatch semantic adapters for ingestion payloads.
- Build artifact readers for adapter extraction context.
- Execute adapters and legacy extractors, returning observations and stats.

Boundary:
- Owns adapter dispatch and extraction mechanics; no ingestion lifecycle or persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Iterable, Mapping

from backend.services.artifact.memory_service import ArtifactMemoryService, ArtifactReadRequest

from .adapters.base import AdapterContext, ArtifactReader, KnowledgeAdapter
from .adapters import (
    FfufKnowledgeAdapter,
    FpingKnowledgeAdapter,
    GobusterKnowledgeAdapter,
    HydraKnowledgeAdapter,
    MasscanKnowledgeAdapter,
    MsfconsoleKnowledgeAdapter,
    NmapKnowledgeAdapter,
    NucleiKnowledgeAdapter,
    SqlmapKnowledgeAdapter,
    TsharkKnowledgeAdapter,
)
from .adapters.registry import AdapterRegistry
from .contracts import ObservationCreate, parse_semantic_inputs_from_execution


class KnowledgeAdapterRegistryService:
    """Public service seam for resolving and dispatching semantic adapters."""

    def __init__(self, adapters: Iterable[KnowledgeAdapter] | None = None) -> None:
        resolved_adapters = list(adapters) if adapters is not None else self._build_default_adapters()
        self._registry = AdapterRegistry(adapters=resolved_adapters)

    def register_adapter(self, adapter: KnowledgeAdapter) -> None:
        """Register one deterministic adapter implementation."""
        self._registry.register(adapter)

    def build_context(
        self,
        *,
        user_id: int,
        engagement_id: int,
        task_id: int | None,
        source_execution_id: str,
        ingestion_run_id: str,
        execution_payload: Mapping[str, Any],
        tenant_id: int | None = None,
        compact_output_hint: Mapping[str, Any] | None = None,
        artifact_reader: ArtifactReader | None = None,
        evidence_archives: Iterable[Any] | None = None,
    ) -> AdapterContext:
        """Build normalized adapter context from one replay/runtime execution payload."""
        execution = execution_payload.get("execution")
        execution_dict = dict(execution) if isinstance(execution, Mapping) else {}
        parsed_inputs = parse_semantic_inputs_from_execution(execution_dict)
        tool_metadata = dict(parsed_inputs.get("tool_metadata") or {})
        semantic_observations = list(parsed_inputs.get("semantic_observations") or [])
        semantic_evidence = list(parsed_inputs.get("semantic_evidence") or [])

        artifacts = execution_payload.get("artifacts")
        artifact_summaries = list(artifacts) if isinstance(artifacts, list) else []

        return AdapterContext(
            user_id=int(user_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            engagement_id=int(engagement_id),
            task_id=task_id,
            source_execution_id=str(source_execution_id),
            ingestion_run_id=str(ingestion_run_id),
            execution_payload=dict(execution_payload),
            tool_metadata=tool_metadata,
            semantic_observations=semantic_observations,
            semantic_evidence=semantic_evidence,
            artifact_summaries=artifact_summaries,
            evidence_archives=tuple(evidence_archives or ()),
            compact_output_hint=dict(compact_output_hint) if compact_output_hint else None,
            artifact_reader=artifact_reader,
        )

    def resolve_adapters(self, context: AdapterContext) -> list[KnowledgeAdapter]:
        """Resolve adapters for the provided context with deterministic routing."""
        return self._registry.resolve(context)

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        """Execute matching adapters and return merged canonical observations."""
        adapters = self.resolve_adapters(context)
        if not adapters:
            return []
        observations: list[ObservationCreate] = []
        for adapter in adapters:
            observations.extend(adapter.extract(context))
        return observations

    @staticmethod
    def _build_default_adapters() -> list[KnowledgeAdapter]:
        """Return built-in deterministic adapter implementations.

        This list is intentionally expanded by active built-in adapter deliveries.
        Current built-ins cover the canonical adapters wired in this service:
          - nmap          (structured_native, xml)
          - masscan       (structured_native, json)
          - fping         (semantic-first, host-liveness only)
          - ffuf          (web fuzzer/crawler, normalized web.path_discovered)
          - nuclei        (structured_native, jsonl)
          - sqlmap        (structured_native, json)
          - gobuster      (text_native, text)
          - hydra         (semantic-first, confirmed weak-auth findings)
          - msfconsole    (text_native, text)
          - tshark        (semantic-first, redacted PCAP analysis)
        """
        return [
            NmapKnowledgeAdapter(),
            MasscanKnowledgeAdapter(),
            FpingKnowledgeAdapter(),
            FfufKnowledgeAdapter(),
            GobusterKnowledgeAdapter(),
            HydraKnowledgeAdapter(),
            NucleiKnowledgeAdapter(),
            SqlmapKnowledgeAdapter(),
            MsfconsoleKnowledgeAdapter(),
            TsharkKnowledgeAdapter(),
        ]

    def extract_with_stats(
        self,
        *,
        execution_payload: dict[str, Any],
        ingestion_run_id: str,
        user_id: int,
        engagement_id: int,
        task_id: int | None,
        compact_output_hint: Mapping[str, Any] | None,
        legacy_extractors: list[Callable],
        artifact_memory_service: ArtifactMemoryService,
        max_artifact_chars: int,
        tenant_id: int | None = None,
        evidence_archives: Iterable[Any] | None = None,
    ) -> tuple[list[ObservationCreate], dict[str, Any]]:
        """Run registry-first extraction and legacy extractors; return observations and stats."""
        execution = execution_payload.get("execution")
        execution_dict = dict(execution) if isinstance(execution, Mapping) else {}
        artifact_reader = _build_artifact_reader(
            execution_payload=execution_payload,
            task_id=task_id,
            artifact_memory_service=artifact_memory_service,
            max_artifact_chars=max_artifact_chars,
        )
        context = self.build_context(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            task_id=task_id,
            source_execution_id=str(execution_dict.get("execution_id") or ""),
            ingestion_run_id=ingestion_run_id,
            execution_payload=execution_payload,
            compact_output_hint=compact_output_hint,
            artifact_reader=artifact_reader,
            evidence_archives=evidence_archives,
        )
        resolved_adapters = self.resolve_adapters(context)
        adapter_observations: list[ObservationCreate] = []
        for adapter in resolved_adapters:
            adapter_observations.extend(adapter.extract(context))

        # Legacy extractors remain supported for backward compatibility.
        legacy_observations: list[ObservationCreate] = []
        for extractor in legacy_extractors:
            legacy_observations.extend(
                extractor(
                    execution_payload,
                    ingestion_run_id,
                    engagement_id,
                    task_id,
                    compact_output_hint,
                )
            )
        extracted = [*adapter_observations, *legacy_observations]
        stats = _build_extraction_stats(
            context=context,
            resolved_adapters=resolved_adapters,
            adapter_observations=adapter_observations,
            legacy_observations=legacy_observations,
            combined_observations=extracted,
            legacy_extractor_count=len(legacy_extractors),
        )
        return extracted, stats


def _adapter_metric_family(adapter: Any) -> str:
    """Derive adapter metric family from capability families or tool names."""
    capability_families = getattr(adapter, "capability_families", ())
    if isinstance(capability_families, tuple) and capability_families:
        family = str(capability_families[0]).strip().lower()
        if family:
            return family
    tool_names = getattr(adapter, "tool_names", ())
    if isinstance(tool_names, tuple) and tool_names:
        tool_name = str(tool_names[0]).strip().lower()
        if "." in tool_name:
            return tool_name.split(".")[0]
        if tool_name:
            return tool_name
    return "unknown"


def _build_extraction_stats(
    *,
    context: Any,
    resolved_adapters: list[Any],
    adapter_observations: list[ObservationCreate],
    legacy_observations: list[ObservationCreate],
    combined_observations: list[ObservationCreate],
    legacy_extractor_count: int,
) -> dict[str, Any]:
    """Build compact adapter/extraction counters for durable run metadata."""
    by_type: dict[str, int] = {}
    finding_total = 0
    finding_authoritative = 0
    for item in combined_observations:
        key = str(item.observation_type)
        by_type[key] = int(by_type.get(key, 0)) + 1
        if key.startswith("finding."):
            finding_total += 1
            if str(item.assertion_level).strip().lower() in {"observed", "confirmed", "exploited"}:
                finding_authoritative += 1

    source_tool_name = context.source_tool_name()
    family_counts: dict[str, int] = {}
    for adapter in resolved_adapters:
        family = _adapter_metric_family(adapter)
        family_counts[family] = int(family_counts.get(family, 0)) + 1
    zero_observation = len(combined_observations) == 0
    return {
        "source_tool_name": source_tool_name,
        "authoritative_input_source": context.select_authoritative_input_source(),
        "resolved_adapter_count": len(resolved_adapters),
        "resolved_adapters": [adapter.__class__.__name__ for adapter in resolved_adapters],
        "adapter_dispatch_count_by_tool": {source_tool_name: len(resolved_adapters)}
        if source_tool_name
        else {},
        "adapter_dispatch_count_by_family": family_counts,
        "adapter_observation_count": len(adapter_observations),
        "legacy_extractor_count": legacy_extractor_count,
        "legacy_observation_count": len(legacy_observations),
        "observation_count_total": len(combined_observations),
        "observation_count_finding_total": finding_total,
        "observation_count_finding_authoritative": finding_authoritative,
        "observation_count_non_finding_total": len(combined_observations) - finding_total,
        "observation_count_by_type": by_type,
        "zero_observation_run_count": 1 if zero_observation else 0,
        "zero_observation_by_tool": {source_tool_name: 1 if zero_observation else 0}
        if source_tool_name
        else {},
    }


def _build_artifact_reader(
    *,
    execution_payload: Mapping[str, Any],
    task_id: int | None,
    artifact_memory_service: ArtifactMemoryService,
    max_artifact_chars: int,
) -> Callable[[str], str | None] | None:
    """Build closure-based artifact reader for adapter extraction context."""
    artifacts = execution_payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    artifact_index: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        artifact_index[artifact_id] = artifact
    if not artifact_index:
        return None

    def _reader(artifact_id: str) -> str | None:
        key = str(artifact_id or "").strip()
        if not key:
            return None
        artifact = artifact_index.get(key)
        if artifact is None:
            return None

        content_text = artifact.get("content_text")
        if isinstance(content_text, str) and content_text.strip():
            return content_text

        if task_id is not None:
            runtime_read = artifact_memory_service.read_task_artifact(
                artifact_id=key,
                task_id=int(task_id),
                request=ArtifactReadRequest(
                    mode="auto",
                    max_chars=max_artifact_chars,
                ),
            )
            if runtime_read.status in {"ready", "omitted_by_policy"} and runtime_read.content:
                runtime_loaded = str(runtime_read.content)
                if runtime_loaded.strip():
                    return runtime_loaded

        return None

    return _reader
