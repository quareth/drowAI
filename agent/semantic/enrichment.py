"""Shared semantic transport helpers for runtime tool execution.

This module centralizes backend-free semantic envelope assembly and extraction
for agent runtime metadata. It does not perform tool-specific parsing.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from runtime_shared.semantic.pentest_facts import (
    SemanticEvidenceType,
    validate_semantic_evidence,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets


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
    """Adapt shared evidence validation to the agent's mutable return contract."""

    if not entries:
        return [], []

    accepted, diagnostics = validate_semantic_evidence(entries)
    valid_entries = [dict(entry) for entry in accepted]
    dropped_entries = [
        _snapshot_dropped_entry(entries[diagnostic.input_position])
        for diagnostic in diagnostics
        if diagnostic.input_position is not None
    ]
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
