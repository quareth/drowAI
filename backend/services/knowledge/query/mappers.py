"""Payload mappers and derived-state helpers for knowledge queries.

This module shapes model rows into API payload dictionaries and owns pure
derived behavior such as exposure/exploitation state and deterministic sorting."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ....models import (
    Engagement,
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeService,
    KnowledgeWebPath,
)
from ..evidence_refs import normalize_canonical_evidence_refs
from ..severity_policy import severity_sort_score
from .contracts import TRUE_VALUES


def serialize_datetime(value: datetime | None) -> str | None:
    """Return stable ISO datetime strings for payload responses."""
    if value is None:
        return None
    return value.isoformat()


def datetime_sort_rank(value: datetime | None) -> float:
    """Return sortable numeric rank for datetime ordering helpers."""
    if value is None:
        return 0.0
    return float(value.timestamp())


def finding_is_exploited(*, finding: KnowledgeFinding) -> bool:
    """Return whether a finding is exploited from status/assertion/metadata."""
    if finding_is_candidate(finding=finding):
        return False
    status = str(finding.status or "").strip().lower()
    assertion_level = str(finding.assertion_level or "").strip().lower()
    if status == "exploited" or assertion_level == "exploited":
        return True
    metadata_state = dict((finding.finding_metadata or {})).get("state")
    if isinstance(metadata_state, dict):
        exploited_flag = metadata_state.get("exploited")
        if isinstance(exploited_flag, bool):
            return exploited_flag
        if isinstance(exploited_flag, str):
            return exploited_flag.strip().lower() in TRUE_VALUES
    return False


def finding_is_open(*, finding: KnowledgeFinding) -> bool:
    """Return whether a finding should count as open for summary rollups."""
    if finding_is_candidate(finding=finding):
        return False
    if finding_is_exploited(finding=finding):
        return True
    status = str(finding.status or "").strip().lower()
    return status in {"open", "confirmed", "triaged", "in_progress"}


def finding_is_candidate(*, finding: KnowledgeFinding) -> bool:
    """Return whether a finding is low-authority candidate-only knowledge."""
    status = str(finding.status or "").strip().lower()
    assertion_level = str(finding.assertion_level or "").strip().lower()
    if status == "candidate" or assertion_level == "candidate":
        return True
    metadata = dict(finding.finding_metadata or {})
    authority = dict(metadata.get("authority") or {})
    if bool(authority.get("candidate_only")):
        return True
    source_kind = str(authority.get("source_kind") or "").strip().lower()
    return source_kind == "llm_candidate"


def finding_sort_key(sort_value: str):
    """Return deterministic sort key callable for finding rows."""

    def _key(row: KnowledgeFinding) -> tuple[Any, ...]:
        severity_score = severity_sort_score(row.severity)
        last_seen_rank = datetime_sort_rank(row.last_seen_at)
        if sort_value == "last_seen_asc":
            return (last_seen_rank, str(row.id))
        if sort_value == "severity_desc":
            return (-severity_score, -last_seen_rank, str(row.id))
        if sort_value == "severity_asc":
            return (severity_score, -last_seen_rank, str(row.id))
        return (-last_seen_rank, str(row.id))

    return _key


def asset_sort_key(sort_value: str):
    """Return deterministic sort key callable for asset rows."""

    def _key(row: KnowledgeAsset) -> tuple[Any, ...]:
        last_seen_rank = datetime_sort_rank(row.last_seen_at)
        asset_type = str(row.asset_type or "")
        if sort_value == "last_seen_asc":
            return (last_seen_rank, str(row.id))
        if sort_value == "asset_type_asc":
            return (asset_type.lower(), -last_seen_rank, str(row.id))
        if sort_value == "asset_type_desc":
            return ("".join(chr(255 - ord(ch)) for ch in asset_type.lower()), -last_seen_rank, str(row.id))
        return (-last_seen_rank, str(row.id))

    return _key


def evidence_sort_key(sort_value: str):
    """Return deterministic sort key callable for evidence rows."""

    def _key(row: KnowledgeEvidenceArchive) -> tuple[Any, ...]:
        metadata = dict(row.archive_metadata or {})
        lineage = dict(row.lineage_snapshot or {})
        source_tool = str(extract_source_tool(lineage=lineage, metadata=metadata) or "").lower()
        observed_rank = datetime_sort_rank(row.created_at)
        if sort_value == "observed_asc":
            return (observed_rank, str(row.id))
        if sort_value == "source_tool_asc":
            return (source_tool, -observed_rank, str(row.id))
        if sort_value == "source_tool_desc":
            return ("".join(chr(255 - ord(ch)) for ch in source_tool), -observed_rank, str(row.id))
        return (-observed_rank, str(row.id))

    return _key


def extract_source_tool(*, lineage: dict[str, object], metadata: dict[str, object]) -> str | None:
    """Return a normalized source tool from lineage or metadata fields."""
    source_tool = (
        lineage.get("source_tool")
        or lineage.get("tool_name")
        or lineage.get("source_tool_name")
        or metadata.get("source_tool")
        or metadata.get("tool_name")
        or metadata.get("source_tool_name")
        or metadata.get("source")
    )
    normalized = str(source_tool).strip() if source_tool is not None else ""
    return normalized or None


def serialize_engagement(row: Engagement) -> dict[str, object]:
    """Serialize one engagement row into the existing API payload shape."""
    return {
        "id": int(row.id),
        "user_id": int(row.user_id),
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "metadata": dict(row.engagement_metadata or {}),
        "created_at": serialize_datetime(row.created_at),
        "updated_at": serialize_datetime(row.updated_at),
    }


def serialize_finding_base(row: KnowledgeFinding) -> dict[str, object]:
    """Serialize shared finding fields used in list and detail payloads."""
    metadata = dict(row.finding_metadata or {})
    authority = metadata.get("authority")
    authority_map = authority if isinstance(authority, dict) else {}
    return {
        "id": str(row.id),
        "finding_key": row.finding_key,
        "finding_type": row.finding_type,
        "subject_type": row.subject_type,
        "subject_key": row.subject_key,
        "asset_id": str(row.asset_id) if row.asset_id is not None else None,
        "service_id": str(row.service_id) if row.service_id is not None else None,
        "title": row.title,
        "severity": row.severity,
        "status": row.status,
        "assertion_level": row.assertion_level,
        "confidence": row.confidence,
        "first_seen_at": serialize_datetime(row.first_seen_at),
        "last_seen_at": serialize_datetime(row.last_seen_at),
        "is_exploited": finding_is_exploited(finding=row),
        "is_open": finding_is_open(finding=row),
        "is_candidate": finding_is_candidate(finding=row),
        "source_tool": _extract_finding_source_tool(metadata),
        "authority_source_kind": str(authority_map.get("source_kind") or "")
        or None,
    }


def _extract_finding_source_tool(metadata: dict[str, object]) -> str | None:
    """Return the finding's source tool from normalized finding metadata."""
    state = metadata.get("state")
    state_map = state if isinstance(state, dict) else {}
    source_tool = (
        metadata.get("source_tool")
        or metadata.get("tool_name")
        or metadata.get("source_tool_name")
        or metadata.get("source")
        or state_map.get("source_tool")
        or state_map.get("tool_name")
        or state_map.get("source_tool_name")
        or state_map.get("source")
    )
    normalized = str(source_tool).strip() if source_tool is not None else ""
    return normalized or None


def _coerce_non_negative_int(value: object) -> int | None:
    """Parse an optional non-negative integer from metadata values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = int(normalized)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _extract_asset_count_from_metadata(metadata: dict[str, object]) -> int | None:
    """Extract affected-asset count hints from normalized finding metadata."""
    state = metadata.get("state")
    state_map = state if isinstance(state, dict) else {}

    integer_hints = (
        metadata.get("affected_asset_count"),
        metadata.get("affected_assets_count"),
        metadata.get("asset_count"),
        state_map.get("affected_asset_count"),
        state_map.get("affected_assets_count"),
        state_map.get("asset_count"),
    )
    for hint in integer_hints:
        parsed = _coerce_non_negative_int(hint)
        if parsed is not None:
            return parsed

    list_hints = (
        metadata.get("affected_asset_ids"),
        metadata.get("affected_assets"),
        metadata.get("asset_ids"),
        state_map.get("affected_asset_ids"),
        state_map.get("affected_assets"),
        state_map.get("asset_ids"),
    )
    for hint in list_hints:
        if isinstance(hint, list):
            return len(hint)

    return None


def finding_affected_asset_count(row: KnowledgeFinding) -> int:
    """Return best-effort affected asset count for findings list/detail UX."""
    metadata = dict(row.finding_metadata or {})
    count = _extract_asset_count_from_metadata(metadata)
    if count is not None:
        return count
    return 1 if row.asset_id is not None else 0


def serialize_finding_list_item(row: KnowledgeFinding) -> dict[str, object]:
    """Serialize finding list row with evidence references and count."""
    evidence_refs = normalize_canonical_evidence_refs(
        (dict(row.evidence_summary or {})).get("evidence_refs"),
        strict=False,
    )
    payload = serialize_finding_base(row)
    payload.update(
        {
            "evidence_count": len(evidence_refs),
            "affected_asset_count": finding_affected_asset_count(row),
            "asset": serialize_asset_summary(row.asset),
            "service": serialize_service_summary(row.service),
            "evidence_refs": evidence_refs,
        }
    )
    return payload


def serialize_asset_summary(row: KnowledgeAsset | None) -> dict[str, object] | None:
    """Serialize compact asset summary for finding/relationship detail views."""
    if row is None:
        return None
    return {
        "id": str(row.id),
        "asset_key": row.asset_key,
        "asset_type": row.asset_type,
        "display_name": row.display_name,
        "ip_address": row.ip_address,
        "hostname": row.hostname,
        "status": row.status,
        "last_seen_at": serialize_datetime(row.last_seen_at),
    }


def serialize_service_summary(row: KnowledgeService | None) -> dict[str, object] | None:
    """Serialize compact service summary for finding/asset detail views."""
    if row is None:
        return None
    return {
        "id": str(row.id),
        "service_key": row.service_key,
        "asset_id": str(row.asset_id) if row.asset_id is not None else None,
        "protocol": row.protocol,
        "port": row.port,
        "service_name": row.service_name,
        "product": row.product,
        "version": row.version,
        "status": row.status,
        "last_seen_at": serialize_datetime(row.last_seen_at),
        "metadata": dict(row.service_metadata or {}),
    }


def serialize_asset_list_item(
    *,
    asset: KnowledgeAsset,
    finding_rows: list[KnowledgeFinding],
    service_count: int,
) -> dict[str, object]:
    """Serialize one asset row including derived risk rollup fields."""
    return {
        "id": str(asset.id),
        "asset_key": asset.asset_key,
        "asset_type": asset.asset_type,
        "display_name": asset.display_name,
        "ip_address": asset.ip_address,
        "hostname": asset.hostname,
        "status": asset.status,
        "first_seen_at": serialize_datetime(asset.first_seen_at),
        "last_seen_at": serialize_datetime(asset.last_seen_at),
        "max_confidence": asset.max_confidence,
        "metadata": dict(asset.asset_metadata or {}),
        "finding_count": len(finding_rows),
        "is_vulnerable": any(finding_is_open(finding=item) for item in finding_rows),
        "is_exploited": any(finding_is_exploited(finding=item) for item in finding_rows),
        "service_count": int(service_count),
    }


def serialize_evidence(row: KnowledgeEvidenceArchive) -> dict[str, object]:
    """Serialize one evidence archive row for engagement evidence listings."""
    metadata = dict(row.archive_metadata or {})
    lineage = dict(row.lineage_snapshot or {})
    source_tool = extract_source_tool(lineage=lineage, metadata=metadata)
    evidence_type = metadata.get("evidence_type") or metadata.get("type") or lineage.get("artifact_kind")
    return {
        "id": str(row.id),
        "task_id": int(row.task_id) if row.task_id is not None else None,
        "source_execution_id": str(row.source_execution_id),
        "source_artifact_id": str(row.source_artifact_id) if row.source_artifact_id is not None else None,
        "storage_mode": row.storage_mode,
        "content_sha256": row.content_sha256,
        "byte_size": row.byte_size,
        "mime_type": row.mime_type,
        "source_tool": str(source_tool) if source_tool is not None else None,
        "evidence_type": str(evidence_type) if evidence_type is not None else None,
        "lineage": lineage,
        "metadata": metadata,
        "created_at": serialize_datetime(row.created_at),
    }


def fallback_graph_node(subject_key: str) -> dict[str, object]:
    """Create a deterministic fallback node when a relationship references unknown nodes."""
    key = str(subject_key or "").strip()
    lowered = key.lower()
    node_type = "unknown"
    if lowered.startswith("host.ip:") or lowered.startswith("host.dns:"):
        node_type = "asset"
    elif lowered.startswith("service.socket:"):
        node_type = "service"
    elif lowered.startswith("finding."):
        node_type = "finding"
    return {
        "id": key,
        "subject_key": key,
        "node_type": node_type,
        "label": key,
        "metadata": {},
    }


def serialize_web_surface_origin_summary(
    *,
    origin_key: str,
    total_paths: int,
    visible_paths: int,
    hidden_noisy: int,
    calibrated_warnings: int,
    producers: list[str],
    first_seen_at: datetime | None,
    last_seen_at: datetime | None,
) -> dict[str, object]:
    """Serialize one service-bound origin summary row."""
    return {
        "origin_key": origin_key,
        "total_paths": int(total_paths),
        "visible_paths": int(visible_paths),
        "hidden_noisy": int(hidden_noisy),
        "calibrated_warnings": int(calibrated_warnings),
        "producers": list(producers),
        "first_seen_at": serialize_datetime(first_seen_at),
        "last_seen_at": serialize_datetime(last_seen_at),
    }


def serialize_web_surface_path_item(row: KnowledgeWebPath) -> dict[str, object]:
    """Serialize one durable service-bound web path row."""
    return {
        "canonical_url": row.canonical_url,
        "path": row.path,
        "last_status_code": row.last_status_code,
        "last_response_size": row.last_response_size,
        "calibrated_baseline": bool(row.calibrated_baseline),
        "noise_score": float(row.noise_score or 0.0),
        "producers": dict(row.producer_summary or {}),
        "first_seen_at": serialize_datetime(row.first_seen_at),
        "last_seen_at": serialize_datetime(row.last_seen_at),
    }
