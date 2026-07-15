"""Define contracts for normalized knowledge observations and ingestion runs.

Scope:
- Shared DTOs and validation helpers for ingestion and replay paths.

Responsibilities:
- Enforce assertion levels and namespace formats.
- Canonicalize subject identity keys.
- Build deterministic dedupe keys.

Boundary:
- This module defines semantics and validation only.
- It does not perform database I/O or routing concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .evidence_refs import normalize_canonical_evidence_refs
from runtime_shared.semantic.service_identity import require_service_socket_key


ASSERTION_LEVELS: tuple[str, ...] = ("candidate", "observed", "confirmed", "exploited")
OBSERVATION_SOURCE_KINDS: tuple[str, ...] = ("deterministic", "native_emitter", "llm_candidate")
OBSERVATION_METADATA_FIELDS: tuple[str, ...] = (
    "source_kind",
    "extractor_family",
    "extractor_version",
    "extraction_mode",
    "durable_masking_applied",
    "audit_summary",
)
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SUBJECT_KEY_PATTERN = re.compile(r"^[a-z0-9._:/@#-]{1,512}$")
SEMANTIC_INPUT_SNAPSHOT_VERSION = "1.0"
_OBJECT_KEY_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "object_key",
        "source_object_key",
        "artifact_object_key",
        "evidence_object_key",
    }
)


class IngestionRunStatus(str, Enum):
    """Lifecycle states for one durable ingestion run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ObservationCreate:
    """Service-layer DTO for append-only durable observation writes."""

    user_id: int
    engagement_id: int
    task_id: int | None
    source_execution_id: str
    ingestion_run_id: str
    observation_type: str
    subject_type: str
    subject_key: str
    assertion_level: str
    payload: dict[str, Any] = field(default_factory=dict)
    observation_metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dedupe_key: str | None = None
    tenant_id: int | None = None


@dataclass(frozen=True)
class IngestionRunCreate:
    """Service-layer DTO for durable ingestion-run tracking."""

    user_id: int
    engagement_id: int
    task_id: int | None
    source_execution_id: str
    extractor_family: str
    extractor_version: str
    status: IngestionRunStatus = IngestionRunStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: int | None = None


def validate_assertion_level(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ASSERTION_LEVELS:
        raise ValueError(
            f"Invalid assertion_level '{value}'. Allowed: {', '.join(ASSERTION_LEVELS)}"
        )
    return normalized


def validate_observation_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAMESPACE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Invalid observation_type. Use dotted lowercase namespace format (example: network.open_port)."
        )
    return normalized


def validate_subject_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAMESPACE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Invalid subject_type. Use dotted lowercase namespace format (example: host.ip)."
        )
    return normalized


def canonicalize_subject_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("subject_key cannot be empty")
    if " " in normalized:
        raise ValueError("subject_key cannot contain spaces")
    if not _SUBJECT_KEY_PATTERN.fullmatch(normalized):
        raise ValueError("subject_key contains unsupported characters")
    return normalized


def validate_subject_key_matches_type(*, subject_type: str, subject_key: str) -> tuple[str, str]:
    """Validate subject_type/subject_key pair alignment for canonical identities."""
    validated_type = validate_subject_type(subject_type)
    normalized_key = canonicalize_subject_key(subject_key)
    if not normalized_key.startswith(f"{validated_type}:"):
        raise ValueError("subject_key must be prefixed by subject_type (subject_type:<canonical-id>)")
    if validated_type == "service.socket":
        _validate_service_socket_key(normalized_key)
    return validated_type, normalized_key


def _validate_service_socket_key(value: str) -> None:
    """Validate service socket identities as host plus transport plus port."""
    require_service_socket_key(value)


def build_subject_key(*, subject_type: str, raw_key: str) -> str:
    validated_type = validate_subject_type(subject_type)
    canonical_key = canonicalize_subject_key(raw_key)
    return f"{validated_type}:{canonical_key}"


def build_dedupe_key(
    *,
    observation_type: str,
    subject_type: str,
    subject_key: str,
    assertion_level: str,
    payload: dict[str, Any] | None = None,
) -> str:
    normalized_subject_type, normalized_subject_key = validate_subject_key_matches_type(
        subject_type=subject_type,
        subject_key=subject_key,
    )
    canonical = {
        "observation_type": validate_observation_type(observation_type),
        "subject_type": normalized_subject_type,
        "subject_key": normalized_subject_key,
        "assertion_level": validate_assertion_level(assertion_level),
        "payload": payload or {},
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_observation_create(dto: ObservationCreate) -> ObservationCreate:
    normalized_observation_type = validate_observation_type(dto.observation_type)
    normalized_subject_type, normalized_subject_key = validate_subject_key_matches_type(
        subject_type=dto.subject_type,
        subject_key=dto.subject_key,
    )
    normalized_assertion_level = validate_assertion_level(dto.assertion_level)
    normalized_payload = dict(dto.payload or {})
    if "evidence_refs" in normalized_payload:
        normalized_payload["evidence_refs"] = normalize_canonical_evidence_refs(
            normalized_payload.get("evidence_refs"),
            strict=True,
        )
    computed_dedupe = dto.dedupe_key or build_dedupe_key(
        observation_type=normalized_observation_type,
        subject_type=normalized_subject_type,
        subject_key=normalized_subject_key,
        assertion_level=normalized_assertion_level,
        payload=normalized_payload,
    )
    normalized_metadata = normalize_observation_metadata(dto.observation_metadata)
    if normalized_assertion_level == "candidate":
        _validate_candidate_evidence_refs(normalized_payload)
    return ObservationCreate(
        user_id=dto.user_id,
        engagement_id=dto.engagement_id,
        task_id=dto.task_id,
        source_execution_id=dto.source_execution_id,
        ingestion_run_id=dto.ingestion_run_id,
        observation_type=normalized_observation_type,
        subject_type=normalized_subject_type,
        subject_key=normalized_subject_key,
        assertion_level=normalized_assertion_level,
        payload=normalized_payload,
        observation_metadata=normalized_metadata,
        observed_at=dto.observed_at,
        dedupe_key=computed_dedupe,
        tenant_id=dto.tenant_id,
    )


def normalize_observation_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize and validate authority/audit metadata for durable observations."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("observation_metadata must be a mapping when provided")

    unknown_fields = sorted(
        key for key in metadata.keys() if str(key) not in OBSERVATION_METADATA_FIELDS
    )
    if unknown_fields:
        raise ValueError(
            "observation_metadata contains unsupported fields: " + ", ".join(unknown_fields)
        )

    normalized: dict[str, Any] = {}
    source_kind = metadata.get("source_kind")
    if source_kind is not None:
        normalized_source_kind = str(source_kind).strip().lower()
        if normalized_source_kind not in OBSERVATION_SOURCE_KINDS:
            raise ValueError(
                "Invalid observation_metadata.source_kind "
                f"'{source_kind}'. Allowed: {', '.join(OBSERVATION_SOURCE_KINDS)}"
            )
        normalized["source_kind"] = normalized_source_kind

    for field_name in ("extractor_family", "extractor_version", "extraction_mode"):
        value = metadata.get(field_name)
        if value is None:
            continue
        normalized_value = str(value).strip()
        if not normalized_value:
            raise ValueError(f"observation_metadata.{field_name} cannot be empty")
        normalized[field_name] = normalized_value

    durable_masking_applied = metadata.get("durable_masking_applied")
    if durable_masking_applied is not None:
        if not isinstance(durable_masking_applied, bool):
            raise ValueError("observation_metadata.durable_masking_applied must be a boolean")
        normalized["durable_masking_applied"] = durable_masking_applied

    audit_summary = metadata.get("audit_summary")
    if audit_summary is not None:
        if not isinstance(audit_summary, Mapping):
            raise ValueError("observation_metadata.audit_summary must be a mapping")
        normalized["audit_summary"] = dict(audit_summary)

    return normalized


def _validate_candidate_evidence_refs(payload: Mapping[str, Any] | None) -> None:
    """Enforce evidence-link requirement for candidate observations."""
    payload_map = payload if isinstance(payload, Mapping) else {}
    evidence_refs = payload_map.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ValueError(
            "candidate observations require payload.evidence_refs with at least one "
            "entry containing non-empty evidence_archive_id and excerpt"
        )

    for ref in evidence_refs:
        if not isinstance(ref, Mapping):
            continue
        evidence_archive_id = str(ref.get("evidence_archive_id") or "").strip()
        excerpt = str(ref.get("excerpt") or "").strip()
        if evidence_archive_id and excerpt:
            return

    raise ValueError(
        "candidate observations require payload.evidence_refs with at least one "
        "entry containing non-empty evidence_archive_id and excerpt"
    )


def build_semantic_input_snapshot(
    *,
    execution: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build compact replay-oriented semantic lineage snapshot from execution payload."""
    parsed_inputs = parse_semantic_inputs_from_execution(execution)
    tool_metadata = _strip_object_key_fields(dict(parsed_inputs.get("tool_metadata") or {}))
    semantic_observations = _strip_object_key_fields(
        list(parsed_inputs.get("semantic_observations") or [])
    )
    semantic_evidence = _strip_object_key_fields(list(parsed_inputs.get("semantic_evidence") or []))

    artifact_refs: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        artifact_ref = {
            "artifact_id": artifact.get("artifact_id"),
            "artifact_kind": artifact.get("artifact_kind"),
            "relative_path": artifact.get("relative_path"),
            "mime_type": artifact.get("mime_type"),
            "byte_size": artifact.get("byte_size"),
            "content_sha256": artifact.get("content_sha256"),
        }
        compact_ref = {
            str(key): value
            for key, value in artifact_ref.items()
            if value is not None and str(value).strip() != ""
        }
        if compact_ref:
            artifact_refs.append(compact_ref)

    snapshot: dict[str, Any] = {
        "snapshot_schema_version": SEMANTIC_INPUT_SNAPSHOT_VERSION,
        "source_tool_name": str(execution.get("tool_name") or ""),
        "tool_metadata": tool_metadata,
        "semantic_observations": semantic_observations,
        "semantic_evidence": semantic_evidence,
        "artifact_refs": artifact_refs,
    }
    capability_family = parsed_inputs.get("capability_family")
    if isinstance(capability_family, str) and capability_family.strip():
        snapshot["capability_family"] = capability_family.strip()
    semantic_schema_version = parsed_inputs.get("semantic_schema_version")
    if isinstance(semantic_schema_version, str) and semantic_schema_version.strip():
        snapshot["semantic_schema_version"] = semantic_schema_version.strip()
    return snapshot


def _strip_object_key_fields(value: Any) -> Any:
    """Drop object-key fields from model-facing semantic snapshot payloads."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            key_str = str(key)
            if key_str.strip().lower() in _OBJECT_KEY_FIELD_NAMES:
                continue
            sanitized[key_str] = _strip_object_key_fields(nested)
        return sanitized
    if isinstance(value, list):
        return [_strip_object_key_fields(item) for item in value]
    return value


def parse_semantic_inputs_from_execution(execution: Mapping[str, Any]) -> dict[str, Any]:
    """Extract normalized semantic transport inputs from one execution payload."""
    execution_metadata = execution.get("execution_metadata")
    execution_metadata_dict = (
        dict(execution_metadata) if isinstance(execution_metadata, Mapping) else {}
    )
    tool_metadata_raw = execution_metadata_dict.get("tool_metadata")
    tool_metadata = dict(tool_metadata_raw) if isinstance(tool_metadata_raw, Mapping) else {}
    nested_tool_metadata = tool_metadata.get("metadata")
    if isinstance(nested_tool_metadata, Mapping):
        merged_tool_metadata = dict(nested_tool_metadata)
        for key, value in tool_metadata.items():
            if str(key) == "metadata":
                continue
            merged_tool_metadata[str(key)] = value
        tool_metadata = merged_tool_metadata

    semantic_observations_raw = execution_metadata_dict.get("semantic_observations")
    if not isinstance(semantic_observations_raw, list):
        semantic_observations_raw = tool_metadata.get("semantic_observations")
    semantic_observations = (
        list(semantic_observations_raw) if isinstance(semantic_observations_raw, list) else []
    )
    semantic_evidence_raw = execution_metadata_dict.get("semantic_evidence")
    if not isinstance(semantic_evidence_raw, list):
        semantic_evidence_raw = tool_metadata.get("semantic_evidence")
    semantic_evidence = list(semantic_evidence_raw) if isinstance(semantic_evidence_raw, list) else []
    capability_family = execution_metadata_dict.get("capability_family")
    if not isinstance(capability_family, str) or not capability_family.strip():
        capability_family = tool_metadata.get("capability_family")
    if not isinstance(capability_family, str):
        capability_family = None
    else:
        capability_family = capability_family.strip() or None
    semantic_schema_version = execution_metadata_dict.get("semantic_schema_version")
    if not isinstance(semantic_schema_version, str):
        semantic_schema_version = tool_metadata.get("semantic_schema_version")
    if not isinstance(semantic_schema_version, str):
        semantic_schema_version = None
    elif not semantic_schema_version.strip():
        semantic_schema_version = None
    else:
        semantic_schema_version = semantic_schema_version.strip()
    return {
        "tool_metadata": tool_metadata,
        "semantic_observations": semantic_observations,
        "semantic_evidence": semantic_evidence,
        "capability_family": capability_family,
        "semantic_schema_version": semantic_schema_version,
    }


def build_replay_execution_metadata_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Map stored semantic input snapshot back into execution_metadata shape for replay."""
    payload = dict(snapshot or {})
    execution_metadata: dict[str, Any] = {}
    tool_metadata = payload.get("tool_metadata")
    if isinstance(tool_metadata, Mapping):
        execution_metadata["tool_metadata"] = dict(tool_metadata)
    semantic_observations = payload.get("semantic_observations")
    if isinstance(semantic_observations, list):
        execution_metadata["semantic_observations"] = list(semantic_observations)
    semantic_evidence = payload.get("semantic_evidence")
    if isinstance(semantic_evidence, list):
        execution_metadata["semantic_evidence"] = list(semantic_evidence)
    capability_family = payload.get("capability_family")
    if isinstance(capability_family, str) and capability_family.strip():
        execution_metadata["capability_family"] = capability_family.strip()
    semantic_schema_version = payload.get("semantic_schema_version")
    if isinstance(semantic_schema_version, str) and semantic_schema_version.strip():
        execution_metadata["semantic_schema_version"] = semantic_schema_version.strip()
    else:
        snapshot_schema_version = payload.get("snapshot_schema_version")
        if isinstance(snapshot_schema_version, str) and snapshot_schema_version.strip():
            execution_metadata["semantic_schema_version"] = snapshot_schema_version.strip()
    return execution_metadata
