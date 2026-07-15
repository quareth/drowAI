"""Tests for the deterministic TShark durable knowledge adapter."""

from __future__ import annotations

import json

from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.tshark_adapter import TsharkKnowledgeAdapter


def _build_context(
    *,
    tool_metadata: dict | None = None,
    semantic_observations: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    artifact_reader=None,
) -> AdapterContext:
    evidence_archives = [
        {
            "id": f"archive-{artifact['artifact_id']}",
            "source_artifact_id": artifact["artifact_id"],
            "lineage": {"artifact_id": artifact["artifact_id"]},
        }
        for artifact in artifacts or []
        if isinstance(artifact.get("artifact_id"), str)
    ]
    execution_payload = {
        "execution": {
            "execution_id": "exec-tshark-1",
            "tool_name": "sniffing_spoofing.network_sniffers.tshark",
            "execution_metadata": {
                "tool_metadata": tool_metadata or {},
                "semantic_observations": semantic_observations or [],
            },
        },
        "artifacts": artifacts or [],
    }
    return AdapterContext(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-tshark-1",
        ingestion_run_id="run-tshark-1",
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=semantic_observations or [],
        artifact_summaries=artifacts or [],
        evidence_archives=evidence_archives,
        artifact_reader=artifact_reader,
    )


def _metadata() -> dict:
    return {
        "schema_version": "tshark.v1",
        "analysis_mode": "secret_exposure",
        "pcap": {
            "input_file": "captures/example.pcap",
            "artifact_sha256": "pcap-sha256",
            "packet_count": 3,
            "duration_seconds": 1.5,
        },
        "hosts": ["192.0.2.10"],
        "conversations": [
            {
                "protocol": "tcp",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "dst_port": 80,
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "packet_count": 2,
            }
        ],
        "secret_exposure": [
            {
                "protocol": "http",
                "field": "http.authorization",
                "kind": "authorization_header",
                "frame": "7",
                "stream": "2",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "extraction_filter": "http.authorization",
                "proof_excerpt": "Authorization: <DURABLE_SECRET_MASK:token>",
                "fingerprint": "hmac-sha256:bearer_token:abc123",
                "pcap_artifact_sha256": "pcap-sha256",
            }
        ],
    }


def _semantic_secret_exposure(*, fingerprint: str) -> dict:
    return {
        "observation_type": "finding.vulnerability_detected",
        "subject_type": "finding.vulnerability",
        "subject_key": (
            "finding.vulnerability:service.socket:203.0.113.20/tcp/80:"
            "tshark/credential_exposure_detected/http.authorization"
        ),
        "payload": {
            "detector_id": "tshark/credential_exposure_detected/http.authorization",
            "finding_subtype": "credential_exposure_detected",
            "title": "Credential material exposed in packet capture",
            "severity": "medium",
            "subject_key": "service.socket:203.0.113.20/tcp/80",
            "subject_type": "service.socket",
            "protocol": "http",
            "field": "http.authorization",
            "kind": "authorization_header",
            "frame": "7",
            "stream": "2",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "extraction_filter": "http.authorization",
            "proof_excerpt": "Authorization: <DURABLE_SECRET_MASK:token>",
            "fingerprint": fingerprint,
            "pcap_artifact_sha256": "pcap-sha256",
        },
    }


def test_tshark_adapter_prefers_semantic_observations_and_adds_evidence_refs() -> None:
    adapter = TsharkKnowledgeAdapter()
    artifacts = [{"artifact_id": "artifact-tshark-1", "artifact_kind": "json", "content_text": "{}"}]
    context = _build_context(
        tool_metadata={**_metadata(), "hosts": ["203.0.113.99"]},
        semantic_observations=[
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": "host.ip:192.0.2.55",
                "payload": {"source": "tshark"},
            }
        ],
        artifacts=artifacts,
    )

    assert adapter.supports(context)

    observations = adapter.extract(context)

    assert len(observations) == 1
    assert observations[0].subject_key == "host.ip:192.0.2.55"
    assert observations[0].payload["evidence_refs"] == [
        {"evidence_archive_id": "archive-artifact-tshark-1"}
    ]


def test_tshark_adapter_falls_back_to_masked_metadata_without_raw_pcap_reads() -> None:
    adapter = TsharkKnowledgeAdapter()
    context = _build_context(
        tool_metadata=_metadata(),
        artifact_reader=lambda artifact_id: (_ for _ in ()).throw(AssertionError(artifact_id)),
    )

    observations = adapter.extract(context)

    assert {item.observation_type for item in observations} == {
        "network.host_discovered",
        "network.service_observed",
        "finding.vulnerability_detected",
    }
    passive_service = next(
        item for item in observations if item.observation_type == "network.service_observed"
    )
    assert passive_service.payload["evidence_source"] == "passive_pcap"
    assert passive_service.payload["reachability"] == "unverified"
    finding = next(item for item in observations if item.observation_type == "finding.vulnerability_detected")
    assert finding.subject_key.startswith("finding.vulnerability:service.socket:203.0.113.20/tcp/80:")
    assert finding.payload["finding_subtype"] == "secret_exposure_detected"
    assert "severity" not in finding.payload
    assert "Bearer raw-token" not in str([item.payload for item in observations])


def test_tshark_adapter_does_not_use_application_protocol_as_socket_transport() -> None:
    adapter = TsharkKnowledgeAdapter()
    metadata = _metadata()
    metadata["conversations"] = [
        {
            "protocol": "ftp",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "dst_port": 21,
            "flow_key": "ftp:192.0.2.10:49152->203.0.113.20:21",
            "packet_count": 2,
        }
    ]
    context = _build_context(tool_metadata=metadata)

    observations = adapter.extract(context)
    service_keys = {
        item.subject_key
        for item in observations
        if item.subject_type == "service.socket"
    }

    assert "service.socket:203.0.113.20/ftp/21" not in service_keys
    assert "service.socket:203.0.113.20/tcp/21" in service_keys
    assert all("/ftp/" not in key for key in service_keys)
    service = next(item for item in observations if item.subject_key == "service.socket:203.0.113.20/tcp/21")
    assert service.payload["protocol"] == "tcp"
    assert service.payload["service_name"] == "ftp"
    assert service.payload["application_protocol"] == "ftp"


def test_tshark_adapter_rekeys_semantic_secret_exposures_by_safe_proof() -> None:
    adapter = TsharkKnowledgeAdapter()
    context = _build_context(
        semantic_observations=[
            _semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:abc123"),
            _semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:def456"),
            _semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:abc123"),
        ],
    )

    observations = adapter.extract(context)
    findings = [
        item for item in observations if item.observation_type == "finding.vulnerability_detected"
    ]
    subject_keys = {item.subject_key for item in findings}

    assert len(findings) == 2
    assert len(subject_keys) == 2
    assert all(":secret-exposure/" in key for key in subject_keys)
    assert all("service.socket:203.0.113.20/tcp/80" in key for key in subject_keys)
    assert any("hmac-sha256-bearer_token-abc123" in key for key in subject_keys)
    assert any("hmac-sha256-bearer_token-def456" in key for key in subject_keys)
    assert all(item.payload["exposure_proof_id"].startswith("hmac-sha256:") for item in findings)
    assert "Bearer raw-token" not in str([item.payload for item in findings])


def test_tshark_adapter_accepts_metadata_only_secret_exposure_proof() -> None:
    adapter = TsharkKnowledgeAdapter()
    metadata = _metadata()
    metadata["secret_exposure"] = [
        {
            "protocol": "http",
            "field": "http.authorization",
            "kind": "authorization_header",
            "frame": "7",
            "stream": "2",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "extraction_filter": "http.authorization",
            "proof_mode": "metadata_only",
            "pcap_artifact_sha256": "pcap-sha256",
        }
    ]
    context = _build_context(tool_metadata=metadata)

    observations = adapter.extract(context)
    finding = next(item for item in observations if item.observation_type == "finding.vulnerability_detected")

    assert finding.payload["proof_mode"] == "metadata_only"
    assert finding.payload["exposure_proof_id"] == "pcap-sha256|7|2|http.authorization"
    assert "proof_excerpt" not in finding.payload
    assert "fingerprint" not in finding.payload


def test_tshark_adapter_masks_semantic_row_with_raw_value() -> None:
    adapter = TsharkKnowledgeAdapter()
    semantic_row = _semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:abc123")
    semantic_row["payload"]["raw_value"] = "Bearer should-not-persist"
    context = _build_context(semantic_observations=[semantic_row])

    observations = adapter.extract(context)

    assert observations
    assert "should-not-persist" not in str([item.payload for item in observations])
    assert "<DURABLE_SECRET_MASK:token>" in str([item.payload for item in observations])


def test_tshark_adapter_masks_rows_with_unredacted_secret_fields() -> None:
    adapter = TsharkKnowledgeAdapter()
    metadata = _metadata()
    metadata["secret_exposure"] = [
        {
            "protocol": "http",
            "field": "http.authorization",
            "proof_excerpt": "Bearer raw-token",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
        }
    ]
    context = _build_context(
        tool_metadata=metadata,
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.vulnerability",
                "subject_key": "finding.vulnerability:service.socket:203.0.113.20/tcp/80:tshark/raw",
                "payload": {"password": "cleartext-password"},
            }
        ],
    )

    observations = adapter.extract(context)

    assert {item.observation_type for item in observations} >= {
        "network.host_discovered",
        "network.service_observed",
        "finding.vulnerability_detected",
    }
    assert "raw-token" not in str([item.payload for item in observations])
    assert "cleartext-password" not in str([item.payload for item in observations])


def test_tshark_adapter_masks_unsafe_semantic_excerpt() -> None:
    adapter = TsharkKnowledgeAdapter()
    raw_secret = "Bearer synthetic-raw-token"
    context = _build_context(
        tool_metadata=_metadata(),
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.vulnerability",
                "subject_key": "finding.vulnerability:service.socket:203.0.113.20/tcp/80:tshark/raw",
                "payload": {
                    "detector_id": "tshark/secret_exposure/http.authorization",
                    "finding_subtype": "secret_exposure_detected",
                    "title": "Secret material exposed in packet capture",
                    "protocol": "http",
                    "field": "http.authorization",
                    "kind": "authorization_header",
                    "frame": "7",
                    "stream": "2",
                    "dst": "203.0.113.20",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "proof_excerpt": f"Authorization: {raw_secret}",
                },
            }
        ],
    )

    observations = adapter.extract(context)

    assert any(item.observation_type == "finding.vulnerability_detected" for item in observations)
    finding = next(item for item in observations if item.observation_type == "finding.vulnerability_detected")
    assert finding.payload["proof_excerpt"] == "Authorization: Bearer <DURABLE_SECRET_MASK:token>"
    assert raw_secret not in str([item.payload for item in observations])


def test_tshark_adapter_artifact_fallback_accepts_masked_json_metadata_only() -> None:
    adapter = TsharkKnowledgeAdapter()
    artifacts = [
        {
            "artifact_id": "artifact-tshark-json",
            "artifact_kind": "json",
            "content_text": json.dumps(_metadata()),
        },
        {
            "artifact_id": "artifact-tshark-pcap",
            "artifact_kind": "pcap",
            "content_text": "\x00\x01not-json-pcap-bytes",
        },
    ]
    context = _build_context(tool_metadata={}, artifacts=artifacts)

    observations = adapter.extract(context)

    assert any(item.observation_type == "finding.vulnerability_detected" for item in observations)
    assert all("not-json-pcap-bytes" not in str(item.payload) for item in observations)
