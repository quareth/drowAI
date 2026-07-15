"""Unit tests for PCAP deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.pcap import TSHARK_TOOL_ID, pcap_adapter
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)
from agent.tools.pcap_compaction import build_pcap_compaction


def _pcap_metadata() -> dict[str, object]:
    return {
        "analysis_mode": "secret_exposure",
        "output_format": "json",
        "pcap": {
            "input_file": "/workspace/artifacts/capture.pcap",
            "artifact_sha256": "abc123",
            "packet_count": 2,
        },
        "conversations": [
            {
                "src": "10.0.0.2",
                "dst": "10.0.0.3",
                "protocol": "TCP",
                "packet_count": 2,
                "bytes": 128,
            }
        ],
        "secret_exposure": [
            {
                "kind": "bearer_token",
                "field": "http.authorization",
                "frame": "1",
                "stream": "0",
                "protocol": "http",
                "src": "10.0.0.2",
                "dst": "10.0.0.3",
                "proof_mode": "fingerprint",
                "fingerprint": "hmac-sha256:token-fingerprint",
                "pcap_artifact_sha256": "abc123",
            }
        ],
        "limits": {"lists": {"secret_exposure": {"returned": 1, "total": 1}}},
    }


def test_pcap_adapter_registers_tshark_tool_id() -> None:
    """The tshark tool id resolves to the PCAP deterministic adapter."""

    assert get_adapter(TSHARK_TOOL_ID) is pcap_adapter


def test_pcap_adapter_preserves_tool_authored_compact_fields() -> None:
    """Existing tshark compact summary/findings/evidence are projected unchanged."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=TSHARK_TOOL_ID,
            raw_result={
                "metadata": {
                    "compact_summary": "PCAP compact analysis parsed 2 packets.",
                    "compact_key_findings": [
                        "Secret exposure: bearer_token in http.authorization frame=1.",
                        "Secret exposure: bearer_token in http.authorization frame=1.",
                    ],
                    "compact_decision_evidence": [
                        "Secret exposure: bearer_token in http.authorization frame=1. "
                        "proof=hmac-sha256:token-fingerprint"
                    ],
                    "pcap_compact": {"schema_version": "pcap.compact.v1"},
                }
            },
        )
    )

    assert result.summary == "PCAP compact analysis parsed 2 packets."
    assert result.key_findings == (
        "Secret exposure: bearer_token in http.authorization frame=1.",
    )
    assert result.decision_evidence == (
        "Secret exposure: bearer_token in http.authorization frame=1. "
        "proof=hmac-sha256:token-fingerprint",
    )
    assert result.structured_signals == (
        {"kind": "pcap_compact", "pcap_compact": {"schema_version": "pcap.compact.v1"}},
    )
    assert result.completeness == "partial"
    assert result.lossiness_risk == "low"


def test_pcap_adapter_rebuilds_existing_compact_shape_from_tshark_metadata() -> None:
    """Normalized tshark metadata produces the same compact fields as the builder."""

    metadata = _pcap_metadata()
    expected = build_pcap_compaction(metadata, source_tool=TSHARK_TOOL_ID)

    result = pcap_adapter(
        CompressionInput(
            tool_name=TSHARK_TOOL_ID,
            raw_result={"metadata": metadata},
        )
    )

    assert result.summary == expected["compact_summary"]
    assert result.key_findings == tuple(expected["compact_key_findings"])
    assert result.decision_evidence == tuple(expected["compact_decision_evidence"])
    assert result.structured_signals == (
        {"kind": "pcap_compact", "pcap_compact": expected["pcap_compact"]},
    )
    assert result.structured_signals[0]["pcap_compact"]["pcap"] == {
        "input_file": "/workspace/artifacts/capture.pcap",
        "artifact_sha256": "abc123",
        "packet_count": 2,
        "duration_seconds": None,
    }


def test_pcap_adapter_does_not_promote_raw_stdout_secret_for_metadata_only_proof() -> None:
    """Raw output secrets are not used when semantic metadata omits proof excerpts."""

    result = pcap_adapter(
        CompressionInput(
            tool_name=TSHARK_TOOL_ID,
            raw_result={
                "stdout": "Authorization: Bearer RAW_SECRET_SHOULD_NOT_APPEAR",
                "metadata": {
                    "analysis_mode": "secret_exposure",
                    "pcap": {"packet_count": 1},
                    "secret_exposure": [
                        {
                            "kind": "bearer_token",
                            "field": "http.authorization",
                            "frame": "1",
                            "proof_mode": "metadata_only",
                        }
                    ],
                },
            },
        )
    )

    rendered = " ".join(
        [
            result.summary or "",
            *result.key_findings,
            *result.decision_evidence,
            str(tuple(result.structured_signals)),
        ]
    )
    assert "RAW_SECRET_SHOULD_NOT_APPEAR" not in rendered
    assert "proof=Bearer" not in rendered


def test_pcap_adapter_returns_none_without_pcap_metadata() -> None:
    """Unrelated tool metadata does not produce synthetic PCAP facts."""

    result = pcap_adapter(
        CompressionInput(
            tool_name=TSHARK_TOOL_ID,
            raw_result={"metadata": {"other": "ignored"}},
        )
    )

    assert result.completeness == "none"
    assert result.fallback_reason == "no_pcap_metadata"
