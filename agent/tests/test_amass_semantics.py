"""Contract tests for Amass semantic projection from normalized metadata."""

from __future__ import annotations

import copy

from agent.semantic.enrichment import validate_semantic_evidence_entries
from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from agent.tools.information_gathering.dns.amass import AmassArgs, AmassTool
from agent.tools.information_gathering.dns.amass_semantics import (
    AMASS_CAPABILITY_FAMILY,
    AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
    AMASS_OBSERVATION_DNS_RESOLVES_TO,
    AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
    AMASS_RELATIONSHIP_TYPE_RESOLVES_TO,
    AMASS_SEMANTIC_SCHEMA_VERSION,
    build_amass_evidence,
    build_amass_observations,
)
from agent.tools.information_gathering.dns.amass_analysis import (
    AMASS_NAMES_BEGIN,
    AMASS_NAMES_END,
    AMASS_RESOLVED_BEGIN,
    AMASS_RESOLVED_END,
)


def _metadata() -> dict[str, object]:
    return {
        "subdomains": [
            {
                "subdomain": "api.example.com",
                "ip": ["192.0.2.20", "2001:db8::5"],
                "record_types": ["A", "AAAA"],
                "source": "amass",
            },
            {
                "subdomain": "unresolved.example.com",
                "ip": [],
                "record_types": [],
                "source": "amass",
            },
            {
                "subdomain": "www.example.com",
                "ip": ["192.0.2.10", "192.0.2.10"],
                "record_types": ["A"],
                "source": "amass",
            },
        ],
        "hosts": [
            {"hostname": "api.example.com", "ip": ["192.0.2.20", "2001:db8::5"]},
            {"hostname": "unresolved.example.com", "ip": []},
            {"hostname": "www.example.com", "ip": ["192.0.2.10"]},
        ],
        "ips": ["192.0.2.10", "192.0.2.20", "2001:db8::5"],
        "names_count": 3,
        "resolved_names_count": 2,
        "unresolved_names_count": 1,
        "ip_count": 3,
        "parse_status": "success",
        "diagnostics": [],
    }


def test_build_amass_observations_projects_dns_ip_and_relationships() -> None:
    observations = build_amass_observations(_metadata())

    assert [item["subject_key"] for item in observations] == [
        "host.dns:api.example.com",
        "host.dns:unresolved.example.com",
        "host.dns:www.example.com",
        "host.ip:192.0.2.10",
        "host.ip:192.0.2.20",
        "host.ip:2001:db8::5",
        "relationship.edge:host.dns:api.example.com:resolves_to:host.ip:192.0.2.20",
        "relationship.edge:host.dns:api.example.com:resolves_to:host.ip:2001:db8::5",
        "relationship.edge:host.dns:www.example.com:resolves_to:host.ip:192.0.2.10",
    ]
    assert [item["observation_type"] for item in observations[:3]] == [
        AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
        AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
        AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
    ]
    assert [item["observation_type"] for item in observations[3:6]] == [
        AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
        AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
        AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
    ]
    assert all(
        item["observation_type"] == AMASS_OBSERVATION_DNS_RESOLVES_TO
        for item in observations[6:]
    )
    assert all(
        item["subject_type"] != "finding.vulnerability"
        and not item["observation_type"].startswith("finding.")
        for item in observations
    )


def test_relationships_have_endpoint_observations_and_record_types() -> None:
    observations = build_amass_observations(_metadata())
    endpoint_keys = {
        item["subject_key"]
        for item in observations
        if item["subject_type"] in {"host.dns", "host.ip"}
    }
    relationship_rows = [
        item for item in observations if item["subject_type"] == "relationship.edge"
    ]

    assert relationship_rows
    for row in relationship_rows:
        payload = row["payload"]
        assert payload["relationship_type"] == AMASS_RELATIONSHIP_TYPE_RESOLVES_TO
        assert payload["source_subject_type"] == "host.dns"
        assert payload["target_subject_type"] == "host.ip"
        assert payload["source_subject_key"] in endpoint_keys
        assert payload["target_subject_key"] in endpoint_keys
        assert payload["tool_source"] == "amass"
        expected_record_type = (
            "AAAA" if payload["target_subject_key"].endswith(":2001:db8::5") else "A"
        )
        assert payload["record_type"] == expected_record_type


def test_unresolved_names_emit_dns_observations_without_relationships() -> None:
    observations = build_amass_observations(_metadata())

    assert {
        item["subject_key"]
        for item in observations
        if item["subject_type"] == "host.dns"
    } == {
        "host.dns:api.example.com",
        "host.dns:unresolved.example.com",
        "host.dns:www.example.com",
    }
    assert not any(
        "unresolved.example.com" in item["subject_key"]
        for item in observations
        if item["subject_type"] == "relationship.edge"
    )


def test_build_amass_observations_is_pure_and_deterministic() -> None:
    metadata = _metadata()
    original = copy.deepcopy(metadata)

    first = build_amass_observations(metadata)
    second = build_amass_observations(metadata)

    assert first == second
    assert metadata == original


def test_build_amass_evidence_is_bounded_valid_and_reconstructable() -> None:
    args = AmassArgs(target="Example.COM.", inactivity_timeout_minutes=12)

    evidence = build_amass_evidence(_metadata(), args)
    valid, dropped = validate_semantic_evidence_entries(evidence)

    assert dropped == []
    assert valid == evidence
    assert {
        (item["type"], item["name"], item["value"]) for item in evidence
    } >= {
        (
            SemanticEvidenceType.TARGET_TEMPLATE.value,
            "root_domain",
            "example.com",
        ),
        (SemanticEvidenceType.VARIANT.value, "scan_mode", "passive"),
        (SemanticEvidenceType.RESULT_SUMMARY.value, "names_total", 3),
        (SemanticEvidenceType.RESULT_SUMMARY.value, "unique_ips", 3),
        (SemanticEvidenceType.DIAGNOSTIC.value, "parse_status", "success"),
    }
    assert len(evidence) <= 6


def test_amass_tool_hooks_delegate_to_semantic_builders() -> None:
    tool = AmassTool()
    args = AmassArgs(target="example.com")
    metadata = _metadata()

    assert tool.emit_semantic_observations(
        stdout="raw output ignored",
        stderr="stderr ignored",
        exit_code=0,
        args=args,
        metadata=metadata,
    ) == build_amass_observations(metadata)
    assert tool.emit_semantic_evidence(
        stdout="raw output ignored",
        stderr="stderr ignored",
        exit_code=0,
        args=args,
        metadata=metadata,
    ) == build_amass_evidence(metadata, args)


def test_amass_tool_parse_output_adds_semantic_metadata_without_observations() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "api.example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "api.example.com 192.0.2.20",
            AMASS_RESOLVED_END,
        ]
    )

    metadata = AmassTool().parse_output(output, "", 0, AmassArgs(target="example.com"))

    assert metadata["semantic_schema_version"] == AMASS_SEMANTIC_SCHEMA_VERSION
    assert metadata["capability_family"] == AMASS_CAPABILITY_FAMILY
    assert "semantic_observations" not in metadata
    assert "semantic_evidence" not in metadata


def test_semantic_constants_are_stable() -> None:
    assert AMASS_SEMANTIC_SCHEMA_VERSION == "amass.v1"
    assert AMASS_CAPABILITY_FAMILY == "dns_enumeration"
