"""Service-level tests for shared semantic transport helpers."""

from __future__ import annotations

from agent.semantic.enrichment import (
    build_runtime_semantic_metadata,
    extract_runtime_semantic_inputs,
    extract_runtime_semantic_inputs_with_fallback,
    validate_semantic_evidence_entries,
)
from agent.semantic.evidence_vocabulary import SemanticEvidenceType


def test_build_runtime_semantic_metadata_merges_parsed_existing_and_observations() -> None:
    metadata = build_runtime_semantic_metadata(
        parsed_metadata={
            "parsed_source": "tool.parse_output",
            "capability_family": "network_discovery",
        },
        semantic_observations=[{"observation_type": "network.open_port"}],
        existing_metadata={
            "existing_key": "keep",
            "capability_family": "override_family",
        },
    )

    assert metadata["parsed_source"] == "tool.parse_output"
    assert metadata["existing_key"] == "keep"
    assert metadata["capability_family"] == "override_family"
    assert metadata["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]


def test_build_runtime_semantic_metadata_ignores_invalid_observations() -> None:
    metadata = build_runtime_semantic_metadata(
        parsed_metadata={"legacy": True},
        semantic_observations=["bad", 123],  # type: ignore[list-item]
        existing_metadata={"legacy_key": "preserve"},
    )

    assert metadata["legacy"] is True
    assert metadata["legacy_key"] == "preserve"
    assert "semantic_observations" not in metadata


def test_build_runtime_semantic_metadata_drops_entries_without_required_fields() -> None:
    """Validator is the sole authority: entries missing type/name must not survive."""
    evidence_items = [{"source": f"item-{index}"} for index in range(30)]
    metadata = build_runtime_semantic_metadata(
        parsed_metadata={"parsed": True},
        semantic_observations=None,
        semantic_evidence=evidence_items,
    )

    assert "semantic_evidence" not in metadata


def test_build_runtime_semantic_metadata_delegates_caps_to_validator() -> None:
    """Caps are owned by ``validate_semantic_evidence_entries``; this helper must not apply its own.

    The test supplies enough valid entries to force the validator to bind some cap
    (per-type or global) and asserts the helper's output matches the validator's
    verdict exactly, so a silent duplicate cap would mismatch immediately.
    """
    evidence_items = [
        {
            "type": SemanticEvidenceType.BASELINE.value,
            "name": f"baseline_{index}",
            "value": index,
        }
        for index in range(100)
    ]

    metadata = build_runtime_semantic_metadata(
        parsed_metadata={"parsed": True},
        semantic_observations=None,
        semantic_evidence=evidence_items,
    )

    expected_valid, _ = validate_semantic_evidence_entries(evidence_items)
    assert metadata["semantic_evidence"] == expected_valid
    assert len(metadata["semantic_evidence"]) == len(expected_valid)


def test_build_runtime_semantic_metadata_removes_invalid_existing_semantic_evidence() -> None:
    metadata = build_runtime_semantic_metadata(
        parsed_metadata={"parsed": True},
        semantic_observations=None,
        existing_metadata={"semantic_evidence": "invalid"},
    )

    assert metadata == {"parsed": True}


def test_build_runtime_semantic_metadata_validates_existing_semantic_evidence() -> None:
    """``existing_metadata['semantic_evidence']`` must pass through the validator, not a bypass path."""
    metadata = build_runtime_semantic_metadata(
        parsed_metadata={"parsed": True},
        semantic_observations=None,
        existing_metadata={
            "semantic_evidence": [
                {"source": "unqualified"},  # missing type/name → dropped by validator
                {
                    "type": SemanticEvidenceType.BASELINE.value,
                    "name": "autocalibration",
                    "value": True,
                },
            ]
        },
    )

    assert metadata["semantic_evidence"] == [
        {
            "type": SemanticEvidenceType.BASELINE.value,
            "name": "autocalibration",
            "value": True,
            "detail": {},
        }
    ]


def test_extract_runtime_semantic_inputs_normalizes_supported_fields() -> None:
    """Semantic inputs exit pre-validated: vocabulary-conformant evidence survives, malformed drops."""
    extracted = extract_runtime_semantic_inputs(
        {
            "semantic_observations": [
                {"observation_type": "network.open_port"},
                "invalid",
            ],
            "semantic_evidence": [
                {"detail": "missing-type-and-name"},
                {
                    "type": SemanticEvidenceType.BASELINE.value,
                    "name": "autocalibration",
                    "value": True,
                },
            ],
            "capability_family": " network_discovery ",
            "semantic_schema_version": " nmap.v1 ",
        }
    )

    assert extracted == {
        "semantic_observations": [{"observation_type": "network.open_port"}],
        "semantic_evidence": [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "autocalibration",
                "value": True,
                "detail": {},
            }
        ],
        "capability_family": "network_discovery",
        "semantic_schema_version": "nmap.v1",
    }


_SERVICE_BANNER_EVIDENCE = {
    "type": SemanticEvidenceType.BASELINE.value,
    "name": "service_banner",
    "value": "nginx/1.18",
}
_DNS_LOOKUP_EVIDENCE = {
    "type": SemanticEvidenceType.DIAGNOSTIC.value,
    "name": "dns_lookup",
    "value": "ok",
}


def _validated(entry: dict) -> dict:
    """Return the validator's canonical shape for an evidence entry."""
    return {**entry, "detail": {}}


def test_extract_runtime_semantic_inputs_with_fallback_reads_wrapped_metadata() -> None:
    extracted = extract_runtime_semantic_inputs_with_fallback(
        {
            "tool_metadata": {
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [_SERVICE_BANNER_EVIDENCE],
                "capability_family": "network_discovery",
                "semantic_schema_version": "nmap.v1",
            }
        }
    )

    assert extracted == {
        "semantic_observations": [{"observation_type": "network.open_port"}],
        "semantic_evidence": [_validated(_SERVICE_BANNER_EVIDENCE)],
        "capability_family": "network_discovery",
        "semantic_schema_version": "nmap.v1",
    }


def test_extract_runtime_semantic_inputs_with_fallback_uses_fallback_mapping() -> None:
    extracted = extract_runtime_semantic_inputs_with_fallback(
        {},
        fallback_metadata={
            "semantic_observations": [{"observation_type": "network.host_discovered"}],
            "semantic_evidence": [_DNS_LOOKUP_EVIDENCE],
            "capability_family": "network_discovery",
            "semantic_schema_version": "execution_plane.v1",
        },
    )

    assert extracted == {
        "semantic_observations": [{"observation_type": "network.host_discovered"}],
        "semantic_evidence": [_validated(_DNS_LOOKUP_EVIDENCE)],
        "capability_family": "network_discovery",
        "semantic_schema_version": "execution_plane.v1",
    }


def test_extract_runtime_semantic_inputs_with_fallback_merges_split_envelope_fields() -> None:
    extracted = extract_runtime_semantic_inputs_with_fallback(
        {
            "semantic_observations": [{"observation_type": "network.open_port"}],
            "tool_metadata": {
                "semantic_evidence": [_SERVICE_BANNER_EVIDENCE],
                "capability_family": "network_discovery",
                "semantic_schema_version": "nmap.v1",
            },
        }
    )

    assert extracted == {
        "semantic_observations": [{"observation_type": "network.open_port"}],
        "semantic_evidence": [_validated(_SERVICE_BANNER_EVIDENCE)],
        "capability_family": "network_discovery",
        "semantic_schema_version": "nmap.v1",
    }


def test_extract_runtime_semantic_inputs_drops_malformed_evidence_entries() -> None:
    """Malformed evidence never survives extraction (no silent passthrough)."""
    extracted = extract_runtime_semantic_inputs(
        {
            "semantic_evidence": [
                {"detail": "missing-type-and-name"},
                "not-a-mapping",
                123,
            ]
        }
    )

    assert extracted["semantic_evidence"] == []
