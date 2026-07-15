"""Tests for canonical key builders and subject mapping rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
import pytest

from backend.services.knowledge.identity_service import KnowledgeIdentityService
from backend.services.knowledge.identity.canonical_keys import (
    build_finding_vulnerability_key,
    build_host_dns_key,
    build_host_ip_key,
    build_relationship_edge_key,
    build_service_socket_key,
    build_web_url_key,
    normalize_web_url,
)
from backend.services.knowledge.contracts import ObservationCreate as _ObservationCreate
from backend.services.knowledge.contracts import validate_subject_key_matches_type

ObservationCreate = partial(_ObservationCreate, user_id=1)


def test_host_key_builders_normalize_ip_and_dns() -> None:
    assert build_host_ip_key("10.10.10.5") == "host.ip:10.10.10.5"
    assert build_host_dns_key("App.Example.COM.") == "host.dns:app.example.com"


def test_service_socket_key_builder_normalizes_protocol_and_port() -> None:
    assert (
        build_service_socket_key(ip="10.10.10.5", protocol="TCP", port="443")
        == "service.socket:10.10.10.5/tcp/443"
    )


def test_subject_mapping_rejects_application_protocol_service_socket_keys() -> None:
    with pytest.raises(ValueError):
        validate_subject_key_matches_type(
            subject_type="service.socket",
            subject_key="service.socket:10.10.10.5/ftp/21",
        )


def test_web_url_normalization_is_stable_case_folded_and_path_normalized() -> None:
    first = normalize_web_url("HTTPS://Target.Example//admin/../login/?q=1#frag")
    second = normalize_web_url("https://target.example/login")
    assert first == second == "https://target.example/login"


def test_web_url_key_builder_returns_prefixed_canonical_key() -> None:
    assert build_web_url_key("https://target.example/admin") == "web.url:https://target.example/admin"


def test_finding_vulnerability_key_uses_subject_plus_detector_not_title_text() -> None:
    key = build_finding_vulnerability_key(
        subject_key="service.socket:10.10.10.5/tcp/443",
        detector_id="Nuclei/CVE-2023-1234",
    )
    assert key == "finding.vulnerability:service.socket:10.10.10.5/tcp/443:nuclei/cve-2023-1234"


def test_relationship_edge_key_builder_is_deterministic() -> None:
    first = build_relationship_edge_key(
        source_subject_key="service.socket:10.10.10.5/tcp/443",
        relationship_type="contains",
        target_subject_key="web.url:https://target.example/admin",
    )
    second = build_relationship_edge_key(
        source_subject_key="service.socket:10.10.10.5/tcp/443",
        relationship_type="contains",
        target_subject_key="web.url:https://target.example/admin",
    )
    assert first == second
    assert (
        first
        == "relationship.edge:service.socket:10.10.10.5/tcp/443:contains:web.url:https://target.example/admin"
    )


@pytest.mark.parametrize(
    "builder_call",
    [
        lambda: build_host_ip_key("bad-ip"),
        lambda: build_host_dns_key("bad host"),
        lambda: build_service_socket_key(ip="10.10.10.5", protocol="icmp", port=80),
        lambda: build_web_url_key("/relative/path"),
        lambda: build_finding_vulnerability_key(subject_key="", detector_id="nuclei"),
        lambda: build_relationship_edge_key(
            source_subject_key="host.ip:10.10.10.5",
            relationship_type="Contains Edge",
            target_subject_key="web.url:https://target.example",
        ),
    ],
)
def test_canonical_key_builders_reject_invalid_inputs(builder_call) -> None:
    with pytest.raises(ValueError):
        builder_call()


def test_subject_mapping_rule_enforces_subject_type_prefix_alignment() -> None:
    subject_type, subject_key = validate_subject_key_matches_type(
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.5",
    )
    assert subject_type == "host.ip"
    assert subject_key == "host.ip:10.10.10.5"

    with pytest.raises(ValueError):
        validate_subject_key_matches_type(
            subject_type="host.ip",
            subject_key="service.socket:10.10.10.5/tcp/443",
        )


def test_identity_service_rejects_subject_type_key_mismatch() -> None:
    service = KnowledgeIdentityService()
    observation = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-mismatch",
        ingestion_run_id="run-mismatch",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="service.socket:10.10.10.5/tcp/443",
        assertion_level="observed",
        payload={},
        observed_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError):
        service.resolve_observations([observation])


def test_identity_service_dedupes_cross_tool_host_and_service_overlap() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-nmap",
            ingestion_run_id="run-a",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.5",
            assertion_level="observed",
            observed_at=now,
            payload={"source": "nmap", "evidence_refs": [{"evidence_archive_id": "ev-a1"}]},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-masscan",
            ingestion_run_id="run-b",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.5",
            assertion_level="observed",
            observed_at=now + timedelta(seconds=5),
            payload={"source": "masscan", "evidence_refs": [{"evidence_archive_id": "ev-a2"}]},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-nmap",
            ingestion_run_id="run-a",
            observation_type="network.open_port",
            subject_type="service.socket",
            subject_key="service.socket:10.10.10.5/tcp/443",
            assertion_level="observed",
            observed_at=now,
            payload={"source": "nmap"},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-masscan",
            ingestion_run_id="run-b",
            observation_type="network.open_port",
            subject_type="service.socket",
            subject_key="service.socket:10.10.10.5/tcp/443",
            assertion_level="observed",
            observed_at=now + timedelta(seconds=5),
            payload={"source": "masscan"},
        ),
    ]

    result = service.resolve_observations(observations)
    host_marker = "asset:host.ip:10.10.10.5"
    service_marker = "service:service.socket:10.10.10.5/tcp/443"

    assert host_marker in result.merge_decisions
    assert service_marker in result.merge_decisions
    assert result.merge_decisions[host_marker].observation_count == 2
    assert result.merge_decisions[service_marker].observation_count == 2
    assert len(result.merge_decisions[host_marker].evidence_refs) == 2


def test_identity_service_dedupes_repeated_findings_on_same_subject() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    finding_key = build_finding_vulnerability_key(
        subject_key="service.socket:10.10.10.5/tcp/443",
        detector_id="nuclei/cve-2023-1234",
    )
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-1",
            ingestion_run_id="run-1",
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            assertion_level="observed",
            observed_at=now,
            payload={"confidence": "medium", "evidence_refs": [{"evidence_archive_id": "ev-f1"}]},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-2",
            ingestion_run_id="run-2",
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            assertion_level="observed",
            observed_at=now + timedelta(minutes=1),
            payload={"confidence": "high", "evidence_refs": [{"evidence_archive_id": "ev-f2"}]},
        ),
    ]

    result = service.resolve_observations(observations)
    marker = f"finding:{finding_key}"
    decision = result.merge_decisions[marker]

    assert decision.observation_count == 2
    assert decision.confidence == "high"
    assert decision.first_seen_at == now
    assert decision.last_seen_at == now + timedelta(minutes=1)
    assert len(decision.evidence_refs) == 2


def test_identity_service_relationship_key_is_stable_for_repeated_web_path_edges() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    relationship_key = build_relationship_edge_key(
        source_subject_key="service.socket:10.10.10.5/tcp/443",
        relationship_type="contains",
        target_subject_key="web.url:https://target.example/admin",
    )
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-gobuster-1",
            ingestion_run_id="run-g-1",
            observation_type="relationship.contains",
            subject_type="relationship.edge",
            subject_key=relationship_key,
            assertion_level="observed",
            observed_at=now,
            payload={
                "source_subject_key": "service.socket:10.10.10.5/tcp/443",
                "relationship_type": "contains",
                "target_subject_key": "web.url:https://target.example/admin",
            },
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-gobuster-2",
            ingestion_run_id="run-g-2",
            observation_type="relationship.contains",
            subject_type="relationship.edge",
            subject_key=relationship_key,
            assertion_level="observed",
            observed_at=now + timedelta(seconds=30),
            payload={
                "source_subject_key": "service.socket:10.10.10.5/tcp/443",
                "relationship_type": "contains",
                "target_subject_key": "web.url:https://target.example/admin",
            },
        ),
    ]

    result = service.resolve_observations(observations)
    marker = f"relationship:{relationship_key}"
    decision = result.merge_decisions[marker]
    assert decision.observation_count == 2
    assert decision.first_seen_at == now
    assert decision.last_seen_at == now + timedelta(seconds=30)


def test_identity_service_preserves_service_fingerprint_contradictions_in_metadata() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-svc-1",
            ingestion_run_id="run-s-1",
            observation_type="network.service_detected",
            subject_type="service.socket",
            subject_key="service.socket:10.10.10.5/tcp/80",
            assertion_level="observed",
            observed_at=now,
            payload={"service_name": "http"},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-svc-2",
            ingestion_run_id="run-s-2",
            observation_type="network.service_detected",
            subject_type="service.socket",
            subject_key="service.socket:10.10.10.5/tcp/80",
            assertion_level="observed",
            observed_at=now + timedelta(seconds=20),
            payload={"service_name": "nginx"},
        ),
    ]

    result = service.resolve_observations(observations)
    marker = "service:service.socket:10.10.10.5/tcp/80"
    decision = result.merge_decisions[marker]
    assert decision.metadata["state"]["service_name"] == "nginx"
    contradictions = list(decision.metadata.get("contradictions") or [])
    assert len(contradictions) == 1
    assert contradictions[0]["field"] == "service_name"
    assert contradictions[0]["previous"] == "http"
    assert contradictions[0]["incoming"] == "nginx"


def test_identity_service_preserves_host_status_contradictions_in_metadata() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-host-1",
            ingestion_run_id="run-h-1",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.5",
            assertion_level="observed",
            observed_at=now,
            payload={"host_status": "down"},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-host-2",
            ingestion_run_id="run-h-2",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.5",
            assertion_level="observed",
            observed_at=now + timedelta(seconds=30),
            payload={"host_status": "up"},
        ),
    ]

    result = service.resolve_observations(observations)
    marker = "asset:host.ip:10.10.10.5"
    decision = result.merge_decisions[marker]
    assert decision.metadata["state"]["host_status"] == "up"
    contradictions = list(decision.metadata.get("contradictions") or [])
    assert len(contradictions) == 1
    assert contradictions[0]["field"] == "host_status"
    assert contradictions[0]["previous"] == "down"
    assert contradictions[0]["incoming"] == "up"


def test_identity_service_merges_confidence_with_corroboration_and_dedupes_evidence_refs() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    finding_key = "finding.vulnerability:service.socket:10.10.10.5/tcp/443:nuclei/cve-2023-1234"
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-f-1",
            ingestion_run_id="run-f-1",
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            assertion_level="observed",
            observed_at=now,
            payload={"confidence": "medium", "evidence_refs": [{"evidence_archive_id": "ev-f1"}]},
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-f-2",
            ingestion_run_id="run-f-2",
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            assertion_level="observed",
            observed_at=now + timedelta(seconds=40),
            payload={
                "confidence": "high",
                "evidence_refs": [{"evidence_archive_id": "ev-f1"}, {"evidence_archive_id": "ev-f2"}],
            },
        ),
    ]

    result = service.resolve_observations(observations)
    marker = f"finding:{finding_key}"
    decision = result.merge_decisions[marker]
    assert decision.confidence == "high"
    assert len(decision.evidence_refs) == 2


def test_identity_service_rebuilds_relationship_key_from_payload_even_when_prefixed_key_is_noncanonical() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    canonical_key = build_relationship_edge_key(
        source_subject_key="host.ip:10.10.10.5",
        relationship_type="exploits",
        target_subject_key="host.ip:10.10.10.7",
    )
    observations = [
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-rel-1",
            ingestion_run_id="run-rel-1",
            observation_type="relationship.exploits",
            subject_type="relationship.edge",
            subject_key=canonical_key,
            assertion_level="observed",
            observed_at=now,
            payload={
                "source_subject_key": "host.ip:10.10.10.5",
                "relationship_type": "exploits",
                "target_subject_key": "host.ip:10.10.10.7",
            },
        ),
        ObservationCreate(
            engagement_id=1,
            task_id=10,
            source_execution_id="exec-rel-2",
            ingestion_run_id="run-rel-2",
            observation_type="relationship.exploits",
            subject_type="relationship.edge",
            subject_key="relationship.edge:host.ip:10.10.10.5:host.ip:10.10.10.7:exploits",
            assertion_level="observed",
            observed_at=now + timedelta(seconds=1),
            payload={
                "source_subject_key": "host.ip:10.10.10.5",
                "relationship_type": "exploits",
                "target_subject_key": "host.ip:10.10.10.7",
            },
        ),
    ]

    result = service.resolve_observations(observations)
    marker = f"relationship:{canonical_key}"
    assert marker in result.merge_decisions
    assert result.merge_decisions[marker].observation_count == 2


def test_identity_service_rejects_relationship_without_canonical_payload_fields() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)
    observation = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-rel-invalid",
        ingestion_run_id="run-rel-invalid",
        observation_type="relationship.exploits",
        subject_type="relationship.edge",
        subject_key="relationship.edge:host.ip:10.10.10.5:exploits:host.ip:10.10.10.7",
        assertion_level="observed",
        observed_at=now,
        payload={},
    )

    with pytest.raises(ValueError):
        service.resolve_observations([observation])


def test_identity_service_merge_is_stable_when_input_order_changes_for_same_timestamp() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)

    down = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-order-a",
        ingestion_run_id="run-order-a",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.9",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "down"},
    )
    up = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-order-b",
        ingestion_run_id="run-order-b",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.9",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "up"},
    )

    forward = service.resolve_observations([down, up]).merge_decisions["asset:host.ip:10.10.10.9"]
    reverse = service.resolve_observations([up, down]).merge_decisions["asset:host.ip:10.10.10.9"]

    assert forward.metadata == reverse.metadata


def test_identity_service_merge_is_replay_stable_when_only_ingestion_run_id_changes() -> None:
    service = KnowledgeIdentityService()
    now = datetime.now(timezone.utc)

    down_run_a = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-replay-stable",
        ingestion_run_id="run-z",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.10",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "down"},
    )
    up_run_a = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-replay-stable",
        ingestion_run_id="run-a",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.10",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "up"},
    )
    first = service.resolve_observations([down_run_a, up_run_a]).merge_decisions["asset:host.ip:10.10.10.10"]

    down_run_b = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-replay-stable",
        ingestion_run_id="run-0001",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.10",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "down"},
    )
    up_run_b = ObservationCreate(
        engagement_id=1,
        task_id=10,
        source_execution_id="exec-replay-stable",
        ingestion_run_id="run-9999",
        observation_type="network.host_discovered",
        subject_type="host.ip",
        subject_key="host.ip:10.10.10.10",
        assertion_level="observed",
        observed_at=now,
        payload={"host_status": "up"},
    )
    second = service.resolve_observations([down_run_b, up_run_b]).merge_decisions["asset:host.ip:10.10.10.10"]

    assert first.metadata == second.metadata
