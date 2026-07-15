"""Shared deterministic helpers for network discovery adapters.

This module keeps common parsing and ObservationCreate construction logic used
by network-focused adapters (for example nmap and masscan)."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..contracts import ObservationCreate
from ..evidence_refs import resolve_refs_from_artifact_summaries
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    normalize_port,
    normalize_transport_protocol,
)
from .base import AdapterContext
from runtime_shared.semantic.network_common import (
    normalize_service_version as shared_normalize_service_version,
)

_VERSION_TOKEN_RE = re.compile(r"^(?:\d+(?:\.\d+)*(?:[-_][a-z0-9]+)*[a-z0-9]*|v\d+(?:\.\d+)*)$", re.IGNORECASE)


def build_host_subject_key(ip: str) -> str:
    """Return canonical host subject key for one IP."""
    return f"host.ip:{str(ip).strip().lower()}"


def build_service_subject_key(ip: str, protocol: str, port: int) -> str:
    """Return canonical service subject key for one socket."""
    normalized_protocol = normalize_transport_protocol(protocol)
    if normalized_protocol is None:
        raise ValueError("service.socket protocol must be tcp or udp")
    return build_service_socket_key(ip=str(ip), protocol=normalized_protocol, port=int(port))


def split_product_hint(hint: str) -> tuple[str | None, str | None]:
    """Best-effort split of a service banner into product and version."""
    raw = str(hint or "").strip()
    if not raw:
        return (None, None)
    tokens = raw.split()
    if not tokens:
        return (None, None)
    for idx, token in enumerate(tokens):
        if _VERSION_TOKEN_RE.match(token):
            product = " ".join(tokens[:idx]).strip()
            version = token.strip() or None
            if product:
                return (product, version)
            return (raw, None)
    return (raw, None)


def normalize_service_version(value: Any) -> tuple[str | None, str | None, str | None]:
    """Delegate service version normalization to runtime-shared helper contract."""
    return shared_normalize_service_version(value)


def resolve_evidence_refs(context: AdapterContext) -> list[dict[str, Any]]:
    """Return durable archive evidence references for archived artifact summaries."""
    return resolve_refs_from_artifact_summaries(
        artifact_summaries=context.artifact_summaries,
        archives=context.evidence_archives,
    )


def collect_artifact_text_blobs(
    context: AdapterContext,
) -> list[tuple[str, str]]:
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
        if context.artifact_reader is None:
            continue
        if not artifact_id:
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


def dedupe_observations(
    observations: Sequence[ObservationCreate],
) -> list[ObservationCreate]:
    """Dedupe observations by canonical type+subject+payload tuple."""
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    deduped: list[ObservationCreate] = []
    for item in observations:
        payload_items = tuple(
            sorted((str(k), str(v)) for k, v in (item.payload or {}).items())
        )
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
