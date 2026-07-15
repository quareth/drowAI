"""Shared semantic transport helpers for runtime tool execution.

This module centralizes backend-free semantic envelope assembly and extraction
for agent runtime metadata. It does not perform tool-specific parsing.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from agent.semantic import evidence_vocabulary
from agent.semantic.evidence_vocabulary import (
    SemanticEvidenceType,
    _SEMANTIC_EVIDENCE_GLOBAL_LIMIT,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets

_SEMANTIC_SCALAR_TYPES = (str, int, float, bool, type(None))


def build_runtime_semantic_metadata(
    *,
    parsed_metadata: Mapping[str, Any] | None,
    semantic_observations: Sequence[Mapping[str, Any]] | None,
    existing_metadata: Mapping[str, Any] | None = None,
    semantic_evidence: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the flat runtime semantic envelope while preserving legacy keys.

    Evidence normalization is delegated to ``validate_semantic_evidence_entries``
    so this helper has no independent policy (no silent drops, no independent
    cap) and matches the locked "validator is the sole authority" contract.
    """
    parsed = dict(parsed_metadata) if isinstance(parsed_metadata, Mapping) else {}
    merged_metadata: dict[str, Any] = dict(parsed)
    if isinstance(existing_metadata, Mapping):
        merged_metadata.update(dict(existing_metadata))

    if _is_non_empty_mapping_sequence(semantic_observations):
        observations = [
            dict(item) for item in semantic_observations if isinstance(item, Mapping)
        ]
        masked_observations = mask_durable_secrets(
            observations,
            source="runtime_semantic_observations",
        )
        merged_metadata["semantic_observations"] = (
            masked_observations if isinstance(masked_observations, list) else []
        )

    candidate_evidence: Sequence[Any] | None = semantic_evidence
    if candidate_evidence is None:
        runtime_evidence = merged_metadata.get("semantic_evidence")
        if isinstance(runtime_evidence, Sequence) and not isinstance(runtime_evidence, (str, bytes)):
            candidate_evidence = runtime_evidence

    validated_evidence, _ = validate_semantic_evidence_entries(candidate_evidence)
    if validated_evidence:
        masked_evidence = mask_durable_secrets(
            validated_evidence,
            source="runtime_semantic_evidence",
        )
        merged_metadata["semantic_evidence"] = (
            masked_evidence if isinstance(masked_evidence, list) else []
        )
    elif "semantic_evidence" in merged_metadata:
        merged_metadata.pop("semantic_evidence", None)

    return merged_metadata


def extract_runtime_semantic_inputs(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract normalized semantic transport fields from flat runtime metadata.

    Evidence normalization is delegated to ``validate_semantic_evidence_entries``
    so consumers (compressor, tool_processor) receive a list that already
    matches the vocabulary/schema/cap contract without a second policy layer.
    """
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    semantic_observations_raw = metadata_dict.get("semantic_observations")
    semantic_observations = (
        [dict(item) for item in semantic_observations_raw if isinstance(item, Mapping)]
        if isinstance(semantic_observations_raw, Sequence) and not isinstance(semantic_observations_raw, (str, bytes))
        else []
    )
    raw_evidence = metadata_dict.get("semantic_evidence")
    evidence_candidate = (
        raw_evidence
        if isinstance(raw_evidence, Sequence) and not isinstance(raw_evidence, (str, bytes))
        else None
    )
    semantic_evidence, _ = validate_semantic_evidence_entries(evidence_candidate)
    masked_observations = mask_durable_secrets(
        semantic_observations,
        source="runtime_semantic_observations_extract",
    )
    masked_evidence = mask_durable_secrets(
        semantic_evidence,
        source="runtime_semantic_evidence_extract",
    )
    capability_family = metadata_dict.get("capability_family")
    semantic_schema_version = metadata_dict.get("semantic_schema_version")
    return {
        "semantic_observations": masked_observations if isinstance(masked_observations, list) else [],
        "semantic_evidence": masked_evidence if isinstance(masked_evidence, list) else [],
        "capability_family": capability_family.strip()
        if isinstance(capability_family, str) and capability_family.strip()
        else None,
        "semantic_schema_version": semantic_schema_version.strip()
        if isinstance(semantic_schema_version, str) and semantic_schema_version.strip()
        else None,
    }


def extract_runtime_semantic_inputs_with_fallback(
    metadata: Mapping[str, Any] | None,
    *,
    wrapped_tool_metadata_key: str = "tool_metadata",
    fallback_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract semantic inputs while merging flat and wrapped compatibility envelopes."""
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    primary_inputs = extract_runtime_semantic_inputs(metadata_dict)

    wrapped_inputs = _empty_semantic_inputs()
    wrapped_tool_metadata = metadata_dict.get(wrapped_tool_metadata_key)
    if isinstance(wrapped_tool_metadata, Mapping):
        wrapped_inputs = extract_runtime_semantic_inputs(wrapped_tool_metadata)

    fallback_dict = dict(fallback_metadata) if isinstance(fallback_metadata, Mapping) else {}
    fallback_inputs = (
        extract_runtime_semantic_inputs(fallback_dict)
        if fallback_dict
        else _empty_semantic_inputs()
    )

    fallback_wrapped_inputs = _empty_semantic_inputs()
    wrapped_fallback = fallback_dict.get(wrapped_tool_metadata_key)
    if isinstance(wrapped_fallback, Mapping):
        fallback_wrapped_inputs = extract_runtime_semantic_inputs(wrapped_fallback)

    return _merge_semantic_input_candidates(
        primary_inputs,
        wrapped_inputs,
        fallback_inputs,
        fallback_wrapped_inputs,
    )


def validate_semantic_evidence_entries(
    entries: Sequence[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (valid_entries, dropped_entries) without raising.

    Valid entries are fully normalized: bounded, typed per SemanticEvidenceType,
    canonicalized around top-level ``name``/``value``, detail-schema-conformant,
    per-type capped, and globally capped. Downstream consumers must treat the
    returned valid list as final and must not re-apply policy.
    """
    if not entries:
        return [], []

    evidence_detail_schema = evidence_vocabulary.EVIDENCE_DETAIL_SCHEMA
    evidence_per_type_limit = evidence_vocabulary.EVIDENCE_PER_TYPE_LIMIT
    semantic_evidence_limit = _SEMANTIC_EVIDENCE_GLOBAL_LIMIT
    semantic_evidence_name_max_len = evidence_vocabulary.SEMANTIC_EVIDENCE_NAME_MAX_LEN
    semantic_evidence_value_max_len = evidence_vocabulary.SEMANTIC_EVIDENCE_VALUE_MAX_LEN
    semantic_evidence_detail_max_keys = (
        evidence_vocabulary.SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS
    )
    semantic_evidence_detail_value_max_len = (
        evidence_vocabulary.SEMANTIC_EVIDENCE_DETAIL_VALUE_MAX_LEN
    )

    normalized_candidates: list[tuple[SemanticEvidenceType, dict[str, Any], dict[str, Any]]] = []
    dropped_entries: list[dict[str, Any]] = []

    for raw_entry in entries:
        dropped_snapshot = _snapshot_dropped_entry(raw_entry)
        if not isinstance(raw_entry, Mapping):
            dropped_entries.append(dropped_snapshot)
            continue

        entry_type = _parse_semantic_evidence_type(raw_entry.get("type"))
        if entry_type is None:
            dropped_entries.append(dropped_snapshot)
            continue

        normalized_name = _normalize_evidence_name(
            raw_entry.get("name"),
            max_len=semantic_evidence_name_max_len,
        )
        if normalized_name is None:
            dropped_entries.append(dropped_snapshot)
            continue

        normalized_entry: dict[str, Any] = {
            "type": entry_type.value,
            "name": normalized_name,
            "value": _normalize_scalar_value(
                raw_entry.get("value"),
                max_len=semantic_evidence_value_max_len,
                coerce_invalid_to_none=True,
            ),
            "detail": {},
        }
        if isinstance(raw_entry.get("detail"), Mapping):
            normalized_entry["detail"] = _normalize_evidence_detail(
                raw_entry["detail"],
                allowed_keys=evidence_detail_schema[entry_type],
                detail_max_keys=semantic_evidence_detail_max_keys,
                detail_value_max_len=semantic_evidence_detail_value_max_len,
            )

        source = raw_entry.get("source")
        if isinstance(source, str) and source.strip():
            normalized_entry["source"] = source.strip()

        normalized_candidates.append((entry_type, normalized_entry, dropped_snapshot))

    per_type_counts = {evidence_type: 0 for evidence_type in SemanticEvidenceType}
    valid_entries: list[dict[str, Any]] = []
    for entry_type, normalized_entry, dropped_snapshot in normalized_candidates:
        if per_type_counts[entry_type] >= evidence_per_type_limit[entry_type]:
            dropped_entries.append(dropped_snapshot)
            continue

        if len(valid_entries) >= semantic_evidence_limit:
            dropped_entries.append(dropped_snapshot)
            continue

        per_type_counts[entry_type] += 1
        valid_entries.append(normalized_entry)

    return valid_entries, dropped_entries


def render_semantic_observations_for_prompt(
    observations: Sequence[Mapping[str, Any]] | None,
) -> str:
    """Format validated observations as a bounded JSON string, or ''.

    Input is assumed pre-validated. This function only formats.
    """
    if not observations:
        return ""

    canonical_observations: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            return ""
        try:
            canonical_observations.append(_canonicalize_prompt_mapping(observation))
        except TypeError:
            return ""

    if not canonical_observations:
        return ""
    return json.dumps(canonical_observations, ensure_ascii=True, separators=(",", ":"))


def render_semantic_evidence_for_prompt(
    evidence: Sequence[Mapping[str, Any]] | None,
) -> str:
    """Format validated evidence grouped by type as a bounded JSON string, or ''.

    Input is assumed pre-validated (see validate_semantic_evidence_entries).
    Grouping order matches SemanticEvidenceType declaration order for
    deterministic prompt bytes. This function only formats.
    """
    if not evidence:
        return ""

    grouped: dict[SemanticEvidenceType, list[dict[str, Any]]] = {
        evidence_type: [] for evidence_type in SemanticEvidenceType
    }
    for entry in evidence:
        if not isinstance(entry, Mapping):
            return ""
        raw_type = entry.get("type")
        if not isinstance(raw_type, str):
            return ""
        try:
            evidence_type = SemanticEvidenceType(raw_type)
        except ValueError:
            return ""
        try:
            grouped[evidence_type].append(_canonicalize_prompt_mapping(entry))
        except TypeError:
            return ""

    ordered_grouped: dict[str, list[dict[str, Any]]] = {}
    for evidence_type in SemanticEvidenceType:
        entries = grouped[evidence_type]
        if entries:
            ordered_grouped[evidence_type.value] = entries

    if not ordered_grouped:
        return ""
    return json.dumps(ordered_grouped, ensure_ascii=True, separators=(",", ":"))


def _canonicalize_prompt_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return mapping with deterministic key ordering for prompt rendering."""
    canonical: dict[str, Any] = {}
    for key in sorted(mapping.keys()):
        if not isinstance(key, str):
            raise TypeError("Prompt renderer supports string mapping keys only")
        canonical[key] = _canonicalize_prompt_value(mapping[key])
    return canonical


def _canonicalize_prompt_value(value: Any) -> Any:
    """Return recursively canonicalized prompt value."""
    if isinstance(value, Mapping):
        return _canonicalize_prompt_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize_prompt_value(item) for item in value]
    return value


def _parse_semantic_evidence_type(raw_type: Any) -> SemanticEvidenceType | None:
    """Return enum member when raw type matches a known evidence type."""
    if not isinstance(raw_type, str) or not raw_type.strip():
        return None
    try:
        return SemanticEvidenceType(raw_type.strip())
    except ValueError:
        return None


def _normalize_evidence_name(raw_name: Any, *, max_len: int) -> str | None:
    """Return bounded name or None when missing/blank."""
    if not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not name:
        return None
    return name[:max_len]


def _normalize_scalar_value(
    raw_value: Any,
    *,
    max_len: int,
    coerce_invalid_to_none: bool,
) -> Any:
    """Normalize scalar values and bound string lengths."""
    if not isinstance(raw_value, _SEMANTIC_SCALAR_TYPES):
        return None if coerce_invalid_to_none else raw_value
    if isinstance(raw_value, str):
        return raw_value[:max_len]
    return raw_value


def _normalize_evidence_detail(
    raw_detail: Mapping[str, Any],
    *,
    allowed_keys: frozenset[str],
    detail_max_keys: int,
    detail_value_max_len: int,
) -> dict[str, Any]:
    """Return detail containing only allowed scalar keys and bounded values."""
    normalized_detail: dict[str, Any] = {}
    for key, value in raw_detail.items():
        if len(normalized_detail) >= detail_max_keys:
            break
        if not isinstance(key, str) or key not in allowed_keys:
            continue
        if not isinstance(value, _SEMANTIC_SCALAR_TYPES):
            continue
        normalized_detail[key] = _normalize_scalar_value(
            value,
            max_len=detail_value_max_len,
            coerce_invalid_to_none=False,
        )
    return normalized_detail


def _snapshot_dropped_entry(raw_entry: Any) -> dict[str, Any]:
    """Return a serializable dropped-entry snapshot for logging and tests."""
    if isinstance(raw_entry, Mapping):
        return dict(raw_entry)
    return {"_invalid_shape": True, "raw": repr(raw_entry)[:256]}


def _empty_semantic_inputs() -> dict[str, Any]:
    """Return an empty semantic input envelope."""
    return {
        "semantic_observations": [],
        "semantic_evidence": [],
        "capability_family": None,
        "semantic_schema_version": None,
    }


def _merge_semantic_input_candidates(
    *candidates: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge split semantic fields from candidate envelopes by first non-empty field."""
    merged = _empty_semantic_inputs()
    for candidate in candidates:
        semantic_observations = candidate.get("semantic_observations")
        if (
            not merged["semantic_observations"]
            and isinstance(semantic_observations, list)
            and semantic_observations
        ):
            merged["semantic_observations"] = list(semantic_observations)

        semantic_evidence = candidate.get("semantic_evidence")
        if (
            not merged["semantic_evidence"]
            and isinstance(semantic_evidence, list)
            and semantic_evidence
        ):
            merged["semantic_evidence"] = list(semantic_evidence)

        capability_family = candidate.get("capability_family")
        if (
            merged["capability_family"] is None
            and isinstance(capability_family, str)
            and capability_family.strip()
        ):
            merged["capability_family"] = capability_family.strip()

        semantic_schema_version = candidate.get("semantic_schema_version")
        if (
            merged["semantic_schema_version"] is None
            and isinstance(semantic_schema_version, str)
            and semantic_schema_version.strip()
        ):
            merged["semantic_schema_version"] = semantic_schema_version.strip()

    return merged


def _is_non_empty_mapping_sequence(value: Sequence[Mapping[str, Any]] | None) -> bool:
    """Return True when value is a non-empty sequence containing mappings."""
    if not value:
        return False
    return any(isinstance(item, Mapping) for item in value)


__all__ = [
    "build_runtime_semantic_metadata",
    "extract_runtime_semantic_inputs",
    "extract_runtime_semantic_inputs_with_fallback",
    "render_semantic_observations_for_prompt",
    "render_semantic_evidence_for_prompt",
    "validate_semantic_evidence_entries",
]
