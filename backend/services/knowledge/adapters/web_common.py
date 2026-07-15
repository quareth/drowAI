"""Shared deterministic helpers for web discovery/finding adapters.

This module centralizes canonical URL/key construction and common observation
helpers used by gobuster, nuclei, and sqlmap adapters."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from ..contracts import ObservationCreate
from ..evidence_refs import resolve_refs_from_artifact_summaries
from .base import AdapterContext
from runtime_shared.semantic.web_common import (
    build_finding_subject_key as shared_build_finding_subject_key,
    normalize_url as shared_normalize_url,
    sanitize_token as shared_sanitize_token,
)


def resolve_target_url(context: AdapterContext) -> str:
    """Resolve target URL from execution payload tool arguments when available."""
    execution = context.execution_payload.get("execution")
    if not isinstance(execution, Mapping):
        return ""
    tool_arguments = execution.get("tool_arguments")
    if not isinstance(tool_arguments, Mapping):
        return ""
    return normalize_url(tool_arguments.get("target"))


def sanitize_token(value: Any) -> str:
    """Delegate token sanitization to runtime-shared helper contract."""
    return shared_sanitize_token(value)


def normalize_url(value: Any) -> str:
    """Delegate URL normalization to runtime-shared helper contract."""
    return shared_normalize_url(value)


def build_finding_subject_key(
    *,
    detector_id: str,
    target_url: str,
    parameter: str | None = None,
    variant_id: str | None = None,
) -> str:
    """Delegate finding subject-key construction to runtime-shared helper contract."""
    return shared_build_finding_subject_key(
        detector_id=detector_id,
        target_url=target_url,
        parameter=parameter,
        variant_id=variant_id,
    )


def build_web_path_subject_key(
    *,
    url: str | None = None,
    target_url: str | None = None,
    discovered_path: str | None = None,
) -> str:
    """Build canonical subject key for one discovered web path."""
    if url:
        normalized = normalize_url(url)
        return f"web.path:{normalized}" if normalized else ""

    path = str(discovered_path or "").strip()
    if not path:
        return ""
    if target_url:
        joined_url = urljoin(str(target_url).rstrip("/") + "/", path.lstrip("/"))
        return build_web_path_subject_key(url=joined_url)
    normalized = normalize_url(path)
    return f"web.path:{normalized}"


def build_web_origin_key(url: Any) -> str:
    """Return canonical web origin key derived from URL input."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def resolve_evidence_refs(context: AdapterContext) -> list[dict[str, Any]]:
    """Return durable archive evidence references for archived artifact summaries."""
    return resolve_refs_from_artifact_summaries(
        artifact_summaries=context.artifact_summaries,
        archives=context.evidence_archives,
    )


def collect_artifact_text_blobs(context: AdapterContext) -> list[tuple[str, str]]:
    """Collect textual artifact payloads from inline summaries or artifact reader."""
    blobs: list[tuple[str, str]] = []
    for artifact in context.artifact_summaries:
        if not isinstance(artifact, Mapping):
            continue
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        content_text = artifact.get("content_text")
        if isinstance(content_text, str) and content_text.strip():
            blobs.append((artifact_id or "artifact-inline", content_text))
            continue
        if context.artifact_reader is None or not artifact_id:
            continue
        loaded = context.artifact_reader(artifact_id)
        if isinstance(loaded, str) and loaded.strip():
            blobs.append((artifact_id, loaded))
    return blobs


def make_observation(
    *,
    context: AdapterContext,
    observation_type: str,
    subject_type: str,
    subject_key: str,
    payload: Mapping[str, Any] | None = None,
) -> ObservationCreate:
    """Build one ObservationCreate with shared lineage fields."""
    return ObservationCreate(
        user_id=context.user_id,
        engagement_id=context.engagement_id,
        task_id=context.task_id,
        source_execution_id=context.source_execution_id,
        ingestion_run_id=context.ingestion_run_id,
        observation_type=observation_type,
        subject_type=subject_type,
        subject_key=subject_key,
        assertion_level="observed",
        payload=dict(payload or {}),
    )


def dedupe_observations(observations: Sequence[ObservationCreate]) -> list[ObservationCreate]:
    """Dedupe observations by canonical type+subject+payload tuple."""
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    deduped: list[ObservationCreate] = []
    for item in observations:
        payload_items = tuple(sorted((str(k), str(v)) for k, v in (item.payload or {}).items()))
        marker = (
            str(item.observation_type),
            str(item.subject_type),
            str(item.subject_key),
            payload_items,
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped
