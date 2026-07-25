"""Tests for deterministic adapter registry routing and input priority."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.tools.information_gathering.dns.amass_semantics import (
    build_amass_observations,
)
from backend.services.knowledge.adapters.amass_adapter import (
    AMASS_TOOL_ID,
    AmassKnowledgeAdapter,
)
from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.ffuf_adapter import FfufKnowledgeAdapter
from backend.services.knowledge.adapters.gobuster_adapter import GobusterKnowledgeAdapter
from backend.services.knowledge.adapters.hydra_adapter import HydraKnowledgeAdapter
from backend.services.knowledge.adapters.tshark_adapter import TsharkKnowledgeAdapter
from backend.services.knowledge.adapter_registry import KnowledgeAdapterRegistryService
from backend.services.knowledge.contracts import ObservationCreate


class _ToolNameAdapter:
    tool_names = ("information_gathering.network_discovery.nmap",)
    capability_families = ()

    def supports(self, context: AdapterContext) -> bool:
        return context.source_tool_name() == "information_gathering.network_discovery.nmap"

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        return [
            ObservationCreate(
                user_id=context.user_id,
                engagement_id=context.engagement_id,
                task_id=context.task_id,
                source_execution_id=context.source_execution_id,
                ingestion_run_id=context.ingestion_run_id,
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.0.0.5",
                assertion_level="observed",
                payload={"source": "tool_name_adapter"},
                observed_at=datetime.now(timezone.utc),
            )
        ]


class _CapabilityAdapter:
    tool_names = ()
    capability_families = ("network_discovery",)

    def supports(self, context: AdapterContext) -> bool:
        return context.capability_family() == "network_discovery"

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        return [
            ObservationCreate(
                user_id=context.user_id,
                engagement_id=context.engagement_id,
                task_id=context.task_id,
                source_execution_id=context.source_execution_id,
                ingestion_run_id=context.ingestion_run_id,
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.0.0.5/tcp/443",
                assertion_level="observed",
                payload={"source": "capability_adapter"},
                observed_at=datetime.now(timezone.utc),
            )
        ]


def _build_execution_payload(
    *,
    tool_name: str,
    tool_arguments: dict | None = None,
    capability_family: str | None = None,
    semantic_observations: list[dict] | None = None,
    semantic_evidence: list[dict] | None = None,
    tool_metadata: dict | None = None,
) -> dict:
    execution_metadata = {
        "tool_metadata": tool_metadata or {},
    }
    if capability_family is not None:
        execution_metadata["capability_family"] = capability_family
    if semantic_observations is not None:
        execution_metadata["semantic_observations"] = semantic_observations
    if semantic_evidence is not None:
        execution_metadata["semantic_evidence"] = semantic_evidence
    return {
        "execution": {
            "execution_id": "exec-1",
            "tool_name": tool_name,
            "tool_arguments": tool_arguments or {},
            "execution_metadata": execution_metadata,
        },
        "artifacts": [
            {
                "artifact_id": "artifact-1",
                "artifact_kind": "stdout",
                "content_text": "tool output",
            }
        ],
    }


def test_registry_prefers_tool_name_routing_over_capability_fallback() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[_CapabilityAdapter(), _ToolNameAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-1",
        ingestion_run_id="run-1",
        execution_payload=_build_execution_payload(
            tool_name="information_gathering.network_discovery.nmap",
            capability_family="network_discovery",
        ),
    )

    adapters = registry.resolve_adapters(context)
    assert len(adapters) == 1
    assert isinstance(adapters[0], _ToolNameAdapter)

    observations = registry.extract(context)
    assert len(observations) == 1
    assert observations[0].payload["source"] == "tool_name_adapter"


def test_registry_uses_capability_fallback_when_no_tool_match_exists() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[_CapabilityAdapter(), _ToolNameAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-2",
        ingestion_run_id="run-2",
        execution_payload=_build_execution_payload(
            tool_name="information_gathering.network_discovery.masscan",
            capability_family="network_discovery",
        ),
    )

    adapters = registry.resolve_adapters(context)
    assert len(adapters) == 1
    assert isinstance(adapters[0], _CapabilityAdapter)

    observations = registry.extract(context)
    assert len(observations) == 1
    assert observations[0].payload["source"] == "capability_adapter"


def test_registry_returns_zero_adapters_for_unsupported_tool_cleanly() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[_CapabilityAdapter(), _ToolNameAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-3",
        ingestion_run_id="run-3",
        execution_payload=_build_execution_payload(
            tool_name="unsupported.tool",
            capability_family="unsupported_family",
        ),
    )

    assert registry.resolve_adapters(context) == []
    assert registry.extract(context) == []


def test_context_input_priority_never_uses_compact_output_when_richer_inputs_exist() -> None:
    registry = KnowledgeAdapterRegistryService()
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-4",
        ingestion_run_id="run-4",
        execution_payload=_build_execution_payload(
            tool_name="web_applications.web_vulnerability_scanners.nuclei",
            capability_family="web_scanning",
            semantic_observations=[{"observation_type": "finding.vulnerability_detected"}],
            tool_metadata={"vulnerabilities": [{"id": "CVE-2025-0001"}]},
        ),
        compact_output_hint={"vulnerabilities": [{"summary": "from compact"}]},
    )

    assert context.select_authoritative_input_source() == "semantic_observations"


def test_build_context_preserves_semantic_evidence() -> None:
    registry = KnowledgeAdapterRegistryService()
    evidence_rows = [{"evidence_kind": "service_banner", "port": 443}]
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-5a",
        ingestion_run_id="run-5a",
        execution_payload=_build_execution_payload(
            tool_name="information_gathering.network_discovery.nmap",
            semantic_evidence=evidence_rows,
        ),
    )

    assert context.semantic_evidence == evidence_rows


def test_registry_does_not_fallback_to_ffuf_for_capability_only_match() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[FfufKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-ffuf-fallback-1",
        ingestion_run_id="run-ffuf-fallback-1",
        execution_payload=_build_execution_payload(
            tool_name="unsupported.web.enumerator",
            capability_family="web_enumeration",
            tool_metadata={"results": [{"url": "https://example.com/unexpected"}]},
        ),
    )

    assert registry.resolve_adapters(context) == []
    assert registry.extract(context) == []


def test_registry_does_not_cross_route_concrete_adapters_on_capability_only_match() -> None:
    registry = KnowledgeAdapterRegistryService()
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-5",
        ingestion_run_id="run-5",
        execution_payload=_build_execution_payload(
            tool_name="custom.unknown.scanner",
            capability_family="web_scanning",
            tool_metadata={},
        ),
    )

    # Unknown tool names should not route to concrete adapters that happen to
    # share a capability family (for example nuclei/sqlmap both using
    # web_scanning), otherwise unsupported tools can emit false observations.
    assert registry.resolve_adapters(context) == []
    assert registry.extract(context) == []


def test_registry_resolves_ffuf_by_tool_name() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[_CapabilityAdapter(), FfufKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-ffuf-6",
        ingestion_run_id="run-ffuf-6",
        execution_payload=_build_execution_payload(
            tool_name="web_applications.web_application_fuzzers.ffuf",
            capability_family="web_enumeration",
            tool_metadata={
                "results": [{"url": "https://example.com/path-1", "status": 200, "length": 123}]
            },
        ),
    )

    adapters = registry.resolve_adapters(context)
    assert len(adapters) == 1
    assert isinstance(adapters[0], FfufKnowledgeAdapter)

    observations = registry.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:https://example.com/path-1"


def test_default_registry_resolves_tshark_by_tool_name_without_cross_routing() -> None:
    registry = KnowledgeAdapterRegistryService()
    tshark_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-tshark-registry-1",
        ingestion_run_id="run-tshark-registry-1",
        execution_payload=_build_execution_payload(
            tool_name="sniffing_spoofing.network_sniffers.tshark",
            capability_family="packet_analysis",
            tool_metadata={"schema_version": "tshark.v1", "hosts": ["192.0.2.10"]},
        ),
    )
    unknown_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-unknown-registry-1",
        ingestion_run_id="run-unknown-registry-1",
        execution_payload=_build_execution_payload(
            tool_name="sniffing_spoofing.network_sniffers.unknown",
            capability_family="packet_analysis",
            tool_metadata={"schema_version": "tshark.v1", "hosts": ["192.0.2.10"]},
        ),
    )

    tshark_adapters = registry.resolve_adapters(tshark_context)
    assert len(tshark_adapters) == 1
    assert isinstance(tshark_adapters[0], TsharkKnowledgeAdapter)
    assert registry.resolve_adapters(unknown_context) == []


def test_default_registry_resolves_hydra_by_tool_name_without_cross_routing() -> None:
    registry = KnowledgeAdapterRegistryService()
    hydra_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-hydra-registry-1",
        ingestion_run_id="run-hydra-registry-1",
        execution_payload=_build_execution_payload(
            tool_name="password_attacks.online_attacks.hydra",
            capability_family="credential_attack",
            tool_metadata={
                "semantic_schema_version": "hydra.v1",
                "credentials": [
                    {"host": "192.168.1.100", "port": 22, "service": "ssh", "username": "admin"}
                ],
            },
        ),
    )
    unknown_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-unknown-hydra-registry-1",
        ingestion_run_id="run-unknown-hydra-registry-1",
        execution_payload=_build_execution_payload(
            tool_name="password_attacks.online_attacks.unknown",
            capability_family="credential_attack",
            tool_metadata={"semantic_schema_version": "hydra.v1"},
        ),
    )

    hydra_adapters = registry.resolve_adapters(hydra_context)
    assert len(hydra_adapters) == 1
    assert isinstance(hydra_adapters[0], HydraKnowledgeAdapter)
    assert registry.resolve_adapters(unknown_context) == []


def test_gobuster_adapter_matches_locked_web_surface_contract() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[GobusterKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-gobuster-contract-1",
        ingestion_run_id="run-gobuster-contract-1",
        execution_payload=_build_execution_payload(
            tool_name="web_applications.web_crawlers.gobuster",
            tool_arguments={"target": "http://example.com"},
            semantic_observations=[
                {
                    "observation_type": "web.path_discovered",
                    "subject_type": "web.path",
                    "subject_key": "web.path:https://do-not-trust.invalid/raw",
                    "payload": {
                        "url": "HTTP://Example.com:80//Admin?debug=1#frag",
                        "target_url": "http://example.com/FUZZ",
                        "status_code": 200,
                        "response_size": 4321,
                    },
                }
            ],
        ),
    )

    observations = registry.extract(context)
    assert len(observations) == 1
    observation = observations[0]
    assert observation.subject_key == "web.path:http://example.com/Admin"
    assert observation.payload.get("target_url") == "http://example.com/FUZZ"
    assert observation.payload.get("path") == "/Admin"
    assert observation.payload.get("status_code") == 200
    assert observation.payload.get("response_size") == 4321
    assert observation.payload.get("source") == "web_applications.web_crawlers.gobuster"


def test_default_registry_resolves_amass_by_exact_tool_name_only() -> None:
    registry = KnowledgeAdapterRegistryService()
    amass_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-registry-1",
        ingestion_run_id="run-amass-registry-1",
        execution_payload=_build_execution_payload(
            tool_name=AMASS_TOOL_ID,
            capability_family="dns_enumeration",
            tool_metadata={"subdomains": []},
        ),
    )
    unknown_context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-registry-2",
        ingestion_run_id="run-amass-registry-2",
        execution_payload=_build_execution_payload(
            tool_name="information_gathering.dns.unknown",
            capability_family="dns_enumeration",
            tool_metadata={"subdomains": [{"subdomain": "api.example.com"}]},
        ),
    )

    adapters = registry.resolve_adapters(amass_context)
    assert len(adapters) == 1
    assert isinstance(adapters[0], AmassKnowledgeAdapter)
    assert registry.resolve_adapters(unknown_context) == []


def test_amass_adapter_semantic_rows_are_authoritative_and_receive_evidence_refs() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[AmassKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-semantic-1",
        ingestion_run_id="run-amass-semantic-1",
        execution_payload=_build_execution_payload(
            tool_name=AMASS_TOOL_ID,
            semantic_observations=[
                {
                    "observation_type": "dns.name_discovered",
                    "subject_type": "host.dns",
                    "subject_key": "host.dns:api.example.com",
                    "payload": {"tool_source": "amass", "dns_name": "api.example.com"},
                },
                {
                    "observation_type": "dns.address_resolved",
                    "subject_type": "host.ip",
                    "subject_key": "host.ip:192.0.2.20",
                    "payload": {"tool_source": "amass", "address": "192.0.2.20"},
                },
                {
                    "observation_type": "relationship.resolves_to",
                    "subject_type": "relationship.edge",
                    "subject_key": (
                        "relationship.edge:host.dns:api.example.com:"
                        "resolves_to:host.ip:192.0.2.20"
                    ),
                    "payload": {
                        "source_subject_type": "host.dns",
                        "source_subject_key": "host.dns:api.example.com",
                        "relationship_type": "resolves_to",
                        "target_subject_type": "host.ip",
                        "target_subject_key": "host.ip:192.0.2.20",
                        "tool_source": "amass",
                    },
                },
            ],
            tool_metadata={
                "subdomains": [{"subdomain": "fallback.example.com", "ip": ["192.0.2.21"]}]
            },
        ),
        evidence_archives=[
            {
                "id": "archive-artifact-1",
                "source_artifact_id": "artifact-1",
                "lineage": {"artifact_id": "artifact-1"},
            }
        ],
    )

    observations = registry.extract(context)

    assert [item.subject_key for item in observations] == [
        "host.dns:api.example.com",
        "host.ip:192.0.2.20",
        "relationship.edge:host.dns:api.example.com:resolves_to:host.ip:192.0.2.20",
    ]
    assert all(
        item.payload.get("evidence_refs") == [{"evidence_archive_id": "archive-artifact-1"}]
        for item in observations
    )


def test_amass_adapter_falls_back_to_current_normalized_metadata_shape() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[AmassKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-fallback-1",
        ingestion_run_id="run-amass-fallback-1",
        execution_payload=_build_execution_payload(
            tool_name=AMASS_TOOL_ID,
            tool_metadata={
                "subdomains": [
                    {
                        "subdomain": "API.Example.COM.",
                        "ip": ["192.0.2.20", "2001:0db8::5", "192.0.2.20"],
                    },
                    {"subdomain": "unresolved.example.com", "ip": []},
                ],
                "hosts": [
                    {"hostname": "api.example.com", "ip": ["192.0.2.20"]},
                    {"hostname": "www.example.com", "ip": ["192.0.2.10"]},
                ],
                "ips": ["192.0.2.10", "192.0.2.20", "2001:db8::5"],
                "parse_status": "success",
            },
        ),
    )

    observations = registry.extract(context)

    assert [item.subject_key for item in observations] == [
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
    relationship = next(
        item for item in observations if item.observation_type == "relationship.resolves_to"
    )
    assert relationship.payload["source_subject_key"] == "host.dns:api.example.com"
    assert relationship.payload["target_subject_key"] == "host.ip:192.0.2.20"
    assert relationship.payload["relationship_type"] == "resolves_to"


def test_amass_semantic_and_metadata_fallback_projection_remain_in_parity() -> None:
    """Both consumers preserve the same normalized fact identities and values."""

    metadata = {
        "subdomains": [
            {
                "subdomain": "API.Example.COM.",
                "ip": ["2001:0db8::5", "192.0.2.20", "invalid"],
                "discovery_role": "scope_seed",
                "result_scope": "input",
            },
            {
                "subdomain": "api.example.com",
                "ip": ["192.0.2.20"],
                "discovery_role": "newly_discovered",
                "result_scope": "updated",
            },
            {"subdomain": "unresolved.example.com", "ip": []},
            {"subdomain": "invalid name", "ip": ["192.0.2.99"]},
        ],
        "hosts": [{"hostname": "www.example.com", "ip": ["192.0.2.10"]}],
        "ips": ["192.0.2.10", "2001:db8::5"],
    }
    registry = KnowledgeAdapterRegistryService(adapters=[AmassKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-parity-1",
        ingestion_run_id="run-amass-parity-1",
        execution_payload=_build_execution_payload(
            tool_name=AMASS_TOOL_ID,
            tool_metadata=metadata,
        ),
    )

    semantic = build_amass_observations(metadata)
    fallback = registry.extract(context)

    semantic_by_identity = {
        (
            str(row["observation_type"]),
            str(row["subject_type"]),
            str(row["subject_key"]),
        ): row["payload"]
        for row in semantic
    }
    fallback_by_identity = {
        (row.observation_type, row.subject_type, row.subject_key): row.payload
        for row in fallback
    }
    assert tuple(semantic_by_identity) == tuple(fallback_by_identity)
    for identity, fallback_payload in fallback_by_identity.items():
        expected_payload = dict(semantic_by_identity[identity])
        expected_payload.pop("record_types", None)
        assert fallback_payload == expected_payload


def test_amass_adapter_rejects_incompatible_metadata_without_artifact_parsing() -> None:
    registry = KnowledgeAdapterRegistryService(adapters=[AmassKnowledgeAdapter()])
    context = registry.build_context(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-amass-unsafe-1",
        ingestion_run_id="run-amass-unsafe-1",
        execution_payload=_build_execution_payload(
            tool_name=AMASS_TOOL_ID,
            tool_metadata={"subdomains": "api.example.com"},
        ),
        compact_output_hint={"subdomains": [{"subdomain": "compact.example.com"}]},
        artifact_reader=lambda _artifact_id: (
            "__DROWAI_AMASS_V5_NAMES_BEGIN__\nraw.example.com\n"
            "__DROWAI_AMASS_V5_NAMES_END__"
        ),
    )

    assert registry.extract(context) == []
