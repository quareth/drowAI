"""Tests for knowledge contract validators and deterministic helper behavior."""

from __future__ import annotations

from core.llm.structured_schemas import (
    GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT,
    POST_TOOL_DECISION_STRUCTURED_OUTPUT,
)

from backend.services.knowledge.contracts import (
    ASSERTION_LEVELS,
    IngestionRunStatus,
    ObservationCreate,
    build_replay_execution_metadata_from_snapshot,
    build_semantic_input_snapshot,
    build_dedupe_key,
    build_subject_key,
    normalize_observation_create,
    parse_semantic_inputs_from_execution,
    validate_assertion_level,
    validate_observation_type,
    validate_subject_type,
)


def test_validate_assertion_level_accepts_tenant_baseline_levels() -> None:
    for level in ASSERTION_LEVELS:
        assert validate_assertion_level(level) == level


def test_validate_assertion_level_rejects_invalid_value() -> None:
    try:
        validate_assertion_level("trusted")
        assert False, "Expected ValueError for invalid assertion level"
    except ValueError as exc:
        assert "Invalid assertion_level" in str(exc)


def test_validate_observation_type_requires_dotted_namespace() -> None:
    assert validate_observation_type("network.open_port") == "network.open_port"
    try:
        validate_observation_type("open_port")
        assert False, "Expected ValueError for missing namespace"
    except ValueError as exc:
        assert "dotted lowercase namespace format" in str(exc)


def test_validate_subject_type_requires_dotted_namespace() -> None:
    assert validate_subject_type("host.ip") == "host.ip"
    try:
        validate_subject_type("host")
        assert False, "Expected ValueError for missing namespace"
    except ValueError as exc:
        assert "dotted lowercase namespace format" in str(exc)


def test_subject_key_helper_is_deterministic() -> None:
    first = build_subject_key(subject_type="host.ip", raw_key="10.0.0.1")
    second = build_subject_key(subject_type="host.ip", raw_key="10.0.0.1")
    assert first == second == "host.ip:10.0.0.1"


def test_dedupe_key_helper_is_deterministic_for_same_inputs() -> None:
    key_one = build_dedupe_key(
        observation_type="network.open_port",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.1",
        assertion_level="observed",
        payload={"port": 80, "protocol": "tcp"},
    )
    key_two = build_dedupe_key(
        observation_type="network.open_port",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.1",
        assertion_level="observed",
        payload={"protocol": "tcp", "port": 80},
    )
    assert key_one == key_two
    assert len(key_one) == 64


def test_normalize_observation_create_validates_and_derives_dedupe_key() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="NETWORK.OPEN_PORT",
        subject_type="HOST.IP",
        subject_key="HOST.IP:10.0.0.2",
        assertion_level="Observed",
        payload={"port": 443},
    )
    normalized = normalize_observation_create(dto)

    assert normalized.observation_type == "network.open_port"
    assert normalized.subject_type == "host.ip"
    assert normalized.subject_key == "host.ip:10.0.0.2"
    assert normalized.assertion_level == "observed"
    assert normalized.dedupe_key is not None
    assert len(normalized.dedupe_key) == 64


def test_normalize_observation_create_rejects_subject_type_key_mismatch() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="network.open_port",
        subject_type="host.ip",
        subject_key="service.socket:10.0.0.2/tcp/443",
        assertion_level="observed",
        payload={"port": 443},
    )
    try:
        normalize_observation_create(dto)
        assert False, "Expected ValueError for subject_type/subject_key mismatch"
    except ValueError as exc:
        assert "subject_key must be prefixed by subject_type" in str(exc)


def test_dedupe_key_rejects_subject_type_key_mismatch() -> None:
    try:
        build_dedupe_key(
            observation_type="network.open_port",
            subject_type="host.ip",
            subject_key="service.socket:10.0.0.1/tcp/443",
            assertion_level="observed",
            payload={"port": 443, "protocol": "tcp"},
        )
        assert False, "Expected ValueError for subject_type/subject_key mismatch"
    except ValueError as exc:
        assert "subject_key must be prefixed by subject_type" in str(exc)


def test_ingestion_run_status_exposes_tenant_baseline_status_contract() -> None:
    assert {status.value for status in IngestionRunStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
    }


def test_build_semantic_input_snapshot_extracts_replay_critical_fields() -> None:
    snapshot = build_semantic_input_snapshot(
        execution={
            "tool_name": "network.nmap",
            "execution_metadata": {
                "tool_metadata": {
                    "open_ports": [22, 80],
                    "semantic_schema_version": "nmap.v1",
                    "object_key": "tenants/1/tasks/1/runtime/secret.txt",
                    "nested": {"source_object_key": "tenants/1/tasks/1/runtime/secret-2.txt"},
                },
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [
                    {
                        "evidence_kind": "service_banner",
                        "port": 22,
                        "artifact_object_key": "tenants/1/tasks/1/runtime/secret-3.txt",
                    }
                ],
                "semantic_schema_version": "nmap.v1",
                "capability_family": "network_discovery",
            },
        },
        artifacts=[
            {
                "artifact_id": "artifact-1",
                "artifact_kind": "stdout",
                "relative_path": "artifacts/nmap.txt",
                "mime_type": "text/plain",
                "byte_size": 321,
                "content_sha256": "a" * 64,
                "content_text": "should-not-be-copied",
            }
        ],
    )

    assert snapshot["snapshot_schema_version"] == "1.0"
    assert snapshot["source_tool_name"] == "network.nmap"
    assert snapshot["tool_metadata"] == {
        "open_ports": [22, 80],
        "semantic_schema_version": "nmap.v1",
        "nested": {},
    }
    assert snapshot["semantic_observations"] == [{"observation_type": "network.open_port"}]
    assert snapshot["semantic_evidence"] == [{"evidence_kind": "service_banner", "port": 22}]
    assert snapshot["semantic_schema_version"] == "nmap.v1"
    assert snapshot["capability_family"] == "network_discovery"
    assert snapshot["artifact_refs"] == [
        {
            "artifact_id": "artifact-1",
            "artifact_kind": "stdout",
            "relative_path": "artifacts/nmap.txt",
            "mime_type": "text/plain",
            "byte_size": 321,
            "content_sha256": "a" * 64,
        }
    ]


def test_parse_semantic_inputs_from_execution_falls_back_to_tool_metadata_semantic_observations() -> None:
    parsed = parse_semantic_inputs_from_execution(
        {
            "execution_metadata": {
                "tool_metadata": {
                    "parsed_source": "nmap.parse_output",
                    "semantic_observations": [{"observation_type": "network.open_port"}],
                },
                "capability_family": "network_discovery",
            }
        }
    )
    assert parsed["tool_metadata"]["parsed_source"] == "nmap.parse_output"
    assert parsed["semantic_observations"] == [{"observation_type": "network.open_port"}]
    assert parsed["capability_family"] == "network_discovery"


def test_parse_semantic_inputs_from_execution_preserves_tool_metadata_schema_version() -> None:
    parsed = parse_semantic_inputs_from_execution(
        {
            "execution_metadata": {
                "tool_metadata": {
                    "semantic_schema_version": "nmap.v1",
                    "semantic_observations": [{"observation_type": "network.open_port"}],
                },
                "capability_family": "network_discovery",
            }
        }
    )
    assert parsed["tool_metadata"]["semantic_schema_version"] == "nmap.v1"
    assert parsed["capability_family"] == "network_discovery"
    assert parsed["semantic_schema_version"] == "nmap.v1"


def test_parse_semantic_inputs_from_execution_unwraps_runner_local_metadata() -> None:
    parsed = parse_semantic_inputs_from_execution(
        {
            "execution_metadata": {
                "tool_metadata": {
                    "metadata": {
                        "hosts": [{"ip": "127.0.0.1"}],
                        "semantic_schema_version": "generic.v1",
                        "capability_family": "network_discovery",
                    },
                    "transport": "pty",
                },
            }
        }
    )

    assert parsed["tool_metadata"]["hosts"] == [{"ip": "127.0.0.1"}]
    assert parsed["tool_metadata"]["transport"] == "pty"
    assert "metadata" not in parsed["tool_metadata"]
    assert parsed["capability_family"] == "network_discovery"
    assert parsed["semantic_schema_version"] == "generic.v1"


def test_parse_semantic_inputs_from_execution_round_trips_semantic_evidence() -> None:
    parsed = parse_semantic_inputs_from_execution(
        {
            "execution_metadata": {
                "tool_metadata": {"open_ports": [443]},
                "semantic_evidence": [{"evidence_kind": "tls_cert", "port": 443}],
                "capability_family": "network_discovery",
            }
        }
    )
    assert parsed["semantic_evidence"] == [{"evidence_kind": "tls_cert", "port": 443}]
    assert parsed["capability_family"] == "network_discovery"


def test_build_replay_execution_metadata_from_snapshot_maps_supported_fields_only() -> None:
    execution_metadata = build_replay_execution_metadata_from_snapshot(
        {
            "snapshot_schema_version": "1.0",
            "tool_metadata": {"hosts": [{"ip": "10.0.0.9"}]},
            "semantic_observations": [{"observation_type": "network.host_discovered"}],
            "semantic_evidence": [{"evidence_kind": "dns_name", "value": "example.test"}],
            "capability_family": "network_discovery",
            "semantic_schema_version": "nmap.v1",
            "artifact_refs": [{"artifact_id": "artifact-1"}],
        }
    )
    assert execution_metadata == {
        "tool_metadata": {"hosts": [{"ip": "10.0.0.9"}]},
        "semantic_observations": [{"observation_type": "network.host_discovered"}],
        "semantic_evidence": [{"evidence_kind": "dns_name", "value": "example.test"}],
        "capability_family": "network_discovery",
        "semantic_schema_version": "nmap.v1",
    }


def test_candidate_extractor_schema_is_strict_and_rejects_extra_properties() -> None:
    schema = GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema
    assert schema["additionalProperties"] is False

    observation_item = schema["properties"]["candidate_observations"]["items"]
    assert observation_item["additionalProperties"] is False

    attributes = observation_item["properties"]["attributes"]
    assert attributes["type"] == "array"
    assert attributes["items"]["additionalProperties"] is False

    evidence_item = observation_item["properties"]["evidence_refs"]["items"]
    assert evidence_item["additionalProperties"] is False

    analyst_note_item = schema["properties"]["analyst_notes"]["items"]
    assert analyst_note_item["additionalProperties"] is False


def test_candidate_extractor_schema_restricts_assertion_level_to_candidate_only() -> None:
    schema = GENERIC_CANDIDATE_EXTRACTOR_STRUCTURED_OUTPUT.schema
    assertion_level = schema["properties"]["candidate_observations"]["items"]["properties"][
        "assertion_level"
    ]
    assert assertion_level["enum"] == ["candidate"]


def test_post_tool_decision_schema_candidate_refs_allow_artifact_identity() -> None:
    schema = POST_TOOL_DECISION_STRUCTURED_OUTPUT.schema
    candidate_item = schema["properties"]["candidate_observations"]["items"]
    evidence_item = candidate_item["properties"]["evidence_refs"]["items"]
    assert set(evidence_item["properties"].keys()) >= {
        "evidence_archive_id",
        "source_artifact_id",
        "excerpt",
    }
    assert evidence_item["required"] == ["evidence_archive_id", "source_artifact_id", "excerpt"]
    assert evidence_item["anyOf"] == [
        {
            "type": "object",
            "properties": {
                "evidence_archive_id": {"type": "string", "minLength": 1},
                "source_artifact_id": {"type": ["string", "null"], "minLength": 1},
                "excerpt": {"type": "string", "minLength": 1},
            },
            "required": ["evidence_archive_id", "source_artifact_id", "excerpt"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "evidence_archive_id": {"type": ["string", "null"], "minLength": 1},
                "source_artifact_id": {"type": "string", "minLength": 1},
                "excerpt": {"type": "string", "minLength": 1},
            },
            "required": ["evidence_archive_id", "source_artifact_id", "excerpt"],
            "additionalProperties": False,
        },
    ]


def test_normalize_observation_create_preserves_valid_authority_metadata() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="finding.vulnerability_detected",
        subject_type="finding.instance",
        subject_key="finding.instance:cve-2021-44228:http://10.10.10.5/",
        assertion_level="candidate",
        payload={
            "title": "Possible Log4Shell exposure",
            "evidence_refs": [
                {
                    "evidence_archive_id": "archive-1",
                    "excerpt": "suspicious response marker",
                }
            ],
        },
        observation_metadata={
            "source_kind": "llm_candidate",
            "extractor_family": "llm.candidate_extraction",
            "extractor_version": "1.0",
            "extraction_mode": "candidate_fallback",
            "durable_masking_applied": True,
            "audit_summary": {"policy_decision": "run"},
        },
    )
    normalized = normalize_observation_create(dto)
    assert normalized.observation_metadata["source_kind"] == "llm_candidate"
    assert normalized.observation_metadata["extractor_family"] == "llm.candidate_extraction"
    assert normalized.observation_metadata["extractor_version"] == "1.0"
    assert normalized.observation_metadata["extraction_mode"] == "candidate_fallback"
    assert normalized.observation_metadata["durable_masking_applied"] is True
    assert normalized.observation_metadata["audit_summary"] == {"policy_decision": "run"}


def test_normalize_observation_create_rejects_invalid_source_kind_metadata() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="finding.vulnerability_detected",
        subject_type="finding.instance",
        subject_key="finding.instance:cve-2021-44228:http://10.10.10.5/",
        assertion_level="candidate",
        payload={
            "evidence_refs": [
                {
                    "evidence_archive_id": "archive-1",
                    "excerpt": "candidate evidence",
                }
            ]
        },
        observation_metadata={"source_kind": "runtime_summary"},
    )
    try:
        normalize_observation_create(dto)
        assert False, "Expected ValueError for invalid observation_metadata.source_kind"
    except ValueError as exc:
        assert "Invalid observation_metadata.source_kind" in str(exc)


def test_normalize_observation_create_rejects_candidate_without_evidence_refs() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="finding.vulnerability_detected",
        subject_type="finding.instance",
        subject_key="finding.instance:cve-2021-44228:http://10.10.10.5/",
        assertion_level="candidate",
        payload={"title": "Missing evidence refs", "evidence_refs": []},
        observation_metadata={
            "source_kind": "llm_candidate",
            "extractor_family": "llm.candidate_extraction",
            "extractor_version": "1.0",
            "extraction_mode": "candidate_fallback",
        },
    )
    try:
        normalize_observation_create(dto)
        assert False, "Expected ValueError for candidate observation missing evidence refs"
    except ValueError as exc:
        assert "payload.evidence_refs" in str(exc)


def test_normalize_observation_create_rejects_unknown_metadata_fields() -> None:
    dto = ObservationCreate(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-123",
        ingestion_run_id="run-1",
        observation_type="network.open_port",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.10",
        assertion_level="observed",
        payload={},
        observation_metadata={"unknown_field": "x"},
    )
    try:
        normalize_observation_create(dto)
        assert False, "Expected ValueError for unsupported observation_metadata field"
    except ValueError as exc:
        assert "unsupported fields" in str(exc)
