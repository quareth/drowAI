"""Tests for deterministic network adapters (nmap + masscan + fping)."""

from __future__ import annotations

from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.fping_adapter import FpingKnowledgeAdapter
from backend.services.knowledge.adapters.masscan_adapter import MasscanKnowledgeAdapter
from backend.services.knowledge.adapters.nmap_adapter import NmapKnowledgeAdapter
from backend.services.knowledge.adapters.network_common import normalize_service_version, split_product_hint
from backend.services.knowledge.identity.canonical_keys import build_finding_vulnerability_key
from tests.tools.fixtures.output_fixtures import load_output_fixture


def _build_context(
    *,
    tool_name: str,
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
    execution_metadata: dict = {"tool_metadata": tool_metadata or {}}
    if semantic_observations is not None:
        execution_metadata["semantic_observations"] = semantic_observations
    execution_payload = {
        "execution": {
            "execution_id": "exec-network-1",
            "tool_name": tool_name,
            "execution_metadata": execution_metadata,
        },
        "artifacts": artifacts or [],
    }
    return AdapterContext(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-network-1",
        ingestion_run_id="run-network-1",
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=semantic_observations or [],
        artifact_summaries=artifacts or [],
        evidence_archives=evidence_archives,
        compact_output_hint={"summary": "not authoritative"},
        artifact_reader=artifact_reader,
    )


def test_nmap_adapter_extracts_host_open_port_and_service_from_artifact_fixture() -> None:
    adapter = NmapKnowledgeAdapter()
    nmap_output = load_output_fixture("information_gathering.network_discovery.nmap")
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={},
        artifacts=[{"artifact_id": "artifact-nmap-1", "artifact_kind": "stdout", "content_text": nmap_output}],
    )

    observations = adapter.extract(context)
    host_obs = [item for item in observations if item.observation_type == "network.host_discovered"]
    port_obs = [item for item in observations if item.observation_type == "network.open_port"]
    service_obs = [item for item in observations if item.observation_type == "network.service_detected"]

    assert len(host_obs) == 3
    assert len(port_obs) == 10
    assert len(service_obs) == 10
    assert {item.subject_key for item in host_obs} == {
        "host.ip:192.168.1.1",
        "host.ip:192.168.1.10",
        "host.ip:192.168.1.100",
    }
    assert any(item.subject_key == "service.socket:192.168.1.10/tcp/3389" for item in port_obs)
    assert any(item.payload.get("service_name") == "postgresql" for item in service_obs)
    postgres = next(item for item in service_obs if item.payload.get("service_name") == "postgresql")
    assert postgres.payload.get("product") == "PostgreSQL DB"
    assert postgres.payload.get("version") == "14.0"


def test_masscan_adapter_extracts_host_and_open_port_from_fixture_artifact() -> None:
    adapter = MasscanKnowledgeAdapter()
    masscan_output = load_output_fixture("information_gathering.network_discovery.masscan")
    tool_metadata = {
        "hosts": [
            {"ip": "192.168.1.1"},
            {"ip": "192.168.1.10"},
        ],
        # Keep parse_output-style open_ports shape; adapter should still recover host+port mapping from artifact.
        "open_ports": [
            {"port": 22, "protocol": "tcp", "status": "open"},
            {"port": 80, "protocol": "tcp", "status": "open"},
            {"port": 443, "protocol": "tcp", "status": "open"},
        ],
    }
    context = _build_context(
        tool_name="information_gathering.network_discovery.masscan",
        tool_metadata=tool_metadata,
        artifacts=[{"artifact_id": "artifact-masscan-1", "artifact_kind": "stdout", "content_text": masscan_output}],
    )

    observations = adapter.extract(context)
    host_obs = [item for item in observations if item.observation_type == "network.host_discovered"]
    port_obs = [item for item in observations if item.observation_type == "network.open_port"]

    assert len(host_obs) == 2
    assert len(port_obs) == 3
    assert {item.subject_key for item in host_obs} == {
        "host.ip:192.168.1.1",
        "host.ip:192.168.1.10",
    }
    assert {item.subject_key for item in port_obs} == {
        "service.socket:192.168.1.1/tcp/22",
        "service.socket:192.168.1.1/tcp/80",
        "service.socket:192.168.1.10/tcp/443",
    }


def test_nmap_and_masscan_share_stable_host_and_service_socket_subject_keys() -> None:
    nmap_adapter = NmapKnowledgeAdapter()
    masscan_adapter = MasscanKnowledgeAdapter()

    nmap_context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={
            "hosts": [
                {
                    "ip": "192.168.1.1",
                    "ports": [
                        {"port": 22, "protocol": "tcp", "service": "ssh"},
                        {"port": 80, "protocol": "tcp", "service": "http"},
                    ],
                }
            ]
        },
        artifacts=[{"artifact_id": "artifact-nmap-2", "artifact_kind": "stdout", "content_text": "stub"}],
    )
    masscan_context = _build_context(
        tool_name="information_gathering.network_discovery.masscan",
        tool_metadata={
            "hosts": [{"ip": "192.168.1.1"}],
            "open_ports": [
                {"port": 22, "protocol": "tcp", "status": "open"},
                {"port": 80, "protocol": "tcp", "status": "open"},
            ],
        },
        artifacts=[
            {
                "artifact_id": "artifact-masscan-2",
                "artifact_kind": "stdout",
                "content_text": '[{"ip":"192.168.1.1","ports":[{"port":22,"proto":"tcp","status":"open"},{"port":80,"proto":"tcp","status":"open"}]}]',
            }
        ],
    )

    nmap_obs = nmap_adapter.extract(nmap_context)
    masscan_obs = masscan_adapter.extract(masscan_context)

    nmap_keys = {item.subject_key for item in nmap_obs if item.observation_type in {"network.host_discovered", "network.open_port"}}
    masscan_keys = {item.subject_key for item in masscan_obs if item.observation_type in {"network.host_discovered", "network.open_port"}}

    assert nmap_keys == masscan_keys


def test_nmap_adapter_reconstructs_observations_from_artifact_reader_when_content_missing() -> None:
    adapter = NmapKnowledgeAdapter()
    nmap_output = load_output_fixture("information_gathering.network_discovery.nmap")
    artifacts = [{"artifact_id": "archive-artifact-1", "artifact_kind": "archived_evidence"}]
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={},
        artifacts=artifacts,
        artifact_reader=lambda artifact_id: nmap_output if artifact_id == "archive-artifact-1" else None,
    )

    observations = adapter.extract(context)
    host_keys = {item.subject_key for item in observations if item.observation_type == "network.host_discovered"}
    port_keys = {item.subject_key for item in observations if item.observation_type == "network.open_port"}

    assert "host.ip:192.168.1.1" in host_keys
    assert "host.ip:192.168.1.100" in host_keys
    assert "service.socket:192.168.1.1/tcp/22" in port_keys
    assert "service.socket:192.168.1.100/tcp/5432" in port_keys


def test_nmap_adapter_maps_product_and_version_from_tool_metadata() -> None:
    adapter = NmapKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={
            "hosts": [
                {
                    "ip": "192.168.1.50",
                    "ports": [
                        {
                            "port": 22,
                            "protocol": "tcp",
                            "service": "ssh",
                            "product": "OpenSSH",
                            "version": "8.9p1",
                        }
                    ],
                }
            ]
        },
    )
    observations = adapter.extract(context)
    service_obs = [item for item in observations if item.observation_type == "network.service_detected"]
    assert len(service_obs) == 1
    payload = service_obs[0].payload
    assert payload.get("service_name") == "ssh"
    assert payload.get("product") == "OpenSSH"
    assert payload.get("version") == "8.9p1"
    assert payload.get("product_hint") == "OpenSSH 8.9p1"


def test_nmap_adapter_normalizes_version_qualifier_from_tool_metadata() -> None:
    adapter = NmapKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={
            "hosts": [
                {
                    "ip": "127.0.0.1",
                    "ports": [
                        {
                            "port": 5432,
                            "protocol": "tcp",
                            "service": "postgresql",
                            "product": "PostgreSQL DB",
                            "version": "9.6.0 or later",
                        }
                    ],
                }
            ]
        },
    )
    observations = adapter.extract(context)
    service_obs = [item for item in observations if item.observation_type == "network.service_detected"]
    assert len(service_obs) == 1
    payload = service_obs[0].payload
    assert payload.get("product") == "PostgreSQL DB"
    assert payload.get("version") == "9.6.0"
    assert payload.get("version_raw") == "9.6.0 or later"
    assert payload.get("version_relation") == "gte"


def test_split_product_hint_common_patterns() -> None:
    assert split_product_hint("OpenSSH 8.9p1 Ubuntu 3ubuntu0.1") == ("OpenSSH", "8.9p1")
    assert split_product_hint("nginx 1.18.0") == ("nginx", "1.18.0")
    assert split_product_hint("Microsoft Terminal Services") == ("Microsoft Terminal Services", None)
    assert split_product_hint("") == (None, None)
    assert split_product_hint("PostgreSQL DB 14.0") == ("PostgreSQL DB", "14.0")


def test_normalize_service_version_common_patterns() -> None:
    assert normalize_service_version("9.6.0 or later") == ("9.6.0", "9.6.0 or later", "gte")
    assert normalize_service_version("v2.4.57+") == ("v2.4.57", "v2.4.57+", "gte")
    assert normalize_service_version("1.25.2") == ("1.25.2", None, None)
    assert normalize_service_version("unknown") == ("unknown", None, None)


def test_nmap_adapter_semantic_path_preserves_evidence_refs_and_version_fields() -> None:
    adapter = NmapKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        semantic_observations=[
            {
                "observation_type": "network.service_detected",
                "subject_type": "service.socket",
                "subject_key": "service.socket:10.0.0.5/tcp/5432",
                "payload": {
                    "service_name": "postgresql",
                    "product": "PostgreSQL DB",
                    "version": "9.6.0",
                    "version_raw": "9.6.0 or later",
                    "version_relation": "gte",
                    "product_hint": "PostgreSQL DB 9.6.0 or later",
                    "source": "nmap",
                },
            }
        ],
        artifacts=[
            {"artifact_id": "artifact-nmap-sem-1", "artifact_kind": "tool_file", "relative_path": "artifacts/nmap.xml"}
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    payload = observations[0].payload
    assert payload.get("version") == "9.6.0"
    assert payload.get("version_raw") == "9.6.0 or later"
    assert payload.get("version_relation") == "gte"
    assert payload.get("product_hint") == "PostgreSQL DB 9.6.0 or later"
    assert payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-artifact-nmap-sem-1"}
    ]


def test_nmap_adapter_semantic_path_accepts_finding_domain_subjects() -> None:
    adapter = NmapKnowledgeAdapter()
    finding_key = build_finding_vulnerability_key(
        subject_key="service.socket:10.0.0.5/tcp/21",
        detector_id="nmap/ftp-anon",
    )
    context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.vulnerability",
                "subject_key": finding_key,
                "payload": {
                    "detector_id": "nmap/ftp-anon",
                    "script_id": "ftp-anon",
                    "summary": "Anonymous FTP login allowed (FTP code 230)",
                    "subject_key": "service.socket:10.0.0.5/tcp/21",
                    "severity": "medium",
                    "title": "Anonymous FTP login allowed",
                },
            }
        ],
        artifacts=[{"artifact_id": "artifact-nmap-sem-2", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    finding = observations[0]
    assert finding.observation_type == "finding.vulnerability_detected"
    assert finding.subject_type == "finding.vulnerability"
    assert finding.subject_key == finding_key
    assert finding.payload.get("subject_key") == "service.socket:10.0.0.5/tcp/21"
    assert finding.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-artifact-nmap-sem-2"}
    ]


# ---------------------------------------------------------------------------
# fping adapter coverage (Task 3.1).
#
# The fping adapter is host-only by contract: it converts liveness facts into
# canonical ``network.host_discovered`` observations, never ports/services/
# findings. Extraction priority is semantic_observations -> tool_metadata.
# Raw artifact text is NOT parsed for Knowledge in MVP, so these tests must
# never depend on ``content_text`` to exercise extraction.
# ---------------------------------------------------------------------------


def test_fping_adapter_extracts_host_from_semantic_observations() -> None:
    """Semantic ``network.host_discovered`` rows are accepted as the authoritative source."""
    adapter = FpingKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        semantic_observations=[
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": "host.ip:172.17.0.1",
                "payload": {
                    "source": "fping",
                    "host_status": "up",
                    "probe_protocol": "icmp",
                },
            }
        ],
        # Provide tool_metadata too — the semantic path must win and the
        # adapter must NOT double-emit by also walking tool_metadata.
        tool_metadata={"alive_hosts": ["172.17.0.1"]},
        artifacts=[
            {
                "artifact_id": "artifact-fping-1",
                "artifact_kind": "stdout",
                "relative_path": "artifacts/fping_1.txt",
            }
        ],
    )

    observations = adapter.extract(context)

    assert len(observations) == 1
    obs = observations[0]
    assert obs.observation_type == "network.host_discovered"
    assert obs.subject_type == "host.ip"
    assert obs.subject_key == "host.ip:172.17.0.1"
    assert obs.payload.get("source") == "fping"
    assert obs.payload.get("host_status") == "up"
    assert obs.payload.get("probe_protocol") == "icmp"
    assert obs.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-artifact-fping-1"}
    ]


def test_fping_adapter_semantic_path_requires_valid_host_ip_subject() -> None:
    """Semantic rows stay host-only and IP-only before they can short-circuit fallback."""
    adapter = FpingKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        semantic_observations=[
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.dns",
                "subject_key": "host.dns:scanme.example.com",
                "payload": {"source": "fping"},
            },
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": "host.ip:not.an.ip",
                "payload": {"source": "fping"},
            },
            {
                "observation_type": "network.open_port",
                "subject_type": "service.socket",
                "subject_key": "service.socket:172.17.0.1/tcp/80",
                "payload": {"source": "fping"},
            },
        ],
    )

    observations = adapter.extract(context)

    assert observations == []


def test_fping_adapter_extracts_host_from_tool_metadata_alive_hosts() -> None:
    """When semantic observations are absent, ``tool_metadata.alive_hosts`` is the fallback."""
    adapter = FpingKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        tool_metadata={"alive_hosts": ["172.17.0.1", "10.0.0.5"]},
        semantic_observations=[],
    )

    observations = adapter.extract(context)

    assert len(observations) == 2
    assert all(obs.observation_type == "network.host_discovered" for obs in observations)
    assert all(obs.subject_type == "host.ip" for obs in observations)
    assert {obs.subject_key for obs in observations} == {
        "host.ip:172.17.0.1",
        "host.ip:10.0.0.5",
    }
    for obs in observations:
        assert obs.payload.get("source") == "fping"
        assert obs.payload.get("host_status") == "up"
        assert obs.payload.get("probe_protocol") == "icmp"


def test_fping_adapter_dedupes_repeated_hosts() -> None:
    """Repeated alive hosts collapse to one observation per subject key."""
    adapter = FpingKnowledgeAdapter()

    # Semantic-path duplicates.
    semantic_context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        semantic_observations=[
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": "host.ip:172.17.0.1",
                "payload": {
                    "source": "fping",
                    "host_status": "up",
                    "probe_protocol": "icmp",
                },
            },
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": "host.ip:172.17.0.1",
                "payload": {
                    "source": "fping",
                    "host_status": "up",
                    "probe_protocol": "icmp",
                },
            },
        ],
    )
    semantic_obs = adapter.extract(semantic_context)
    assert len(semantic_obs) == 1
    assert semantic_obs[0].subject_key == "host.ip:172.17.0.1"

    # Metadata-path duplicates.
    metadata_context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        tool_metadata={"alive_hosts": ["172.17.0.1", "172.17.0.1", "  172.17.0.1  "]},
    )
    metadata_obs = adapter.extract(metadata_context)
    assert len(metadata_obs) == 1
    assert metadata_obs[0].subject_key == "host.ip:172.17.0.1"


def test_fping_adapter_ignores_dead_metadata_and_non_ip_hostnames() -> None:
    """Non-IP hostnames and dead/unreachable metadata fields never produce observations."""
    adapter = FpingKnowledgeAdapter()
    context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        tool_metadata={
            # Mix of valid IP, hostname (skipped in MVP), garbage, and an empty
            # entry. Dead/unreachable metadata must not synthesize observations.
            "alive_hosts": ["172.17.0.1", "scanme.example.com", "not.an.ip", "", "999.999.999.999"],
            "unresponsive_count": 5,
            "diagnostics": [
                "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70"
            ],
        },
    )

    observations = adapter.extract(context)

    # Only the single valid IP becomes an observation.
    assert len(observations) == 1
    assert observations[0].subject_key == "host.ip:172.17.0.1"
    # Adapter must remain host-only — no ports/services/findings.
    assert {obs.observation_type for obs in observations} == {"network.host_discovered"}


def test_fping_and_nmap_share_stable_host_subject_key() -> None:
    """fping and nmap MUST produce the same ``host.ip:<ip>`` key for the same host.

    This is the cross-tool identity guarantee that prevents projected asset
    duplication when fping discovers a host and nmap later enriches it.
    """
    fping_adapter = FpingKnowledgeAdapter()
    nmap_adapter = NmapKnowledgeAdapter()

    fping_context = _build_context(
        tool_name="information_gathering.network_discovery.fping",
        tool_metadata={"alive_hosts": ["192.168.1.1"]},
    )
    nmap_context = _build_context(
        tool_name="information_gathering.network_discovery.nmap",
        tool_metadata={
            "hosts": [
                {
                    "ip": "192.168.1.1",
                    "ports": [
                        {"port": 22, "protocol": "tcp", "service": "ssh"},
                    ],
                }
            ]
        },
    )

    fping_host_keys = {
        obs.subject_key
        for obs in fping_adapter.extract(fping_context)
        if obs.observation_type == "network.host_discovered"
    }
    nmap_host_keys = {
        obs.subject_key
        for obs in nmap_adapter.extract(nmap_context)
        if obs.observation_type == "network.host_discovered"
    }

    assert fping_host_keys == {"host.ip:192.168.1.1"}
    assert nmap_host_keys == {"host.ip:192.168.1.1"}
    # Identical key for the same IP: existing projection collapses these into
    # one asset row regardless of tool source.
    assert fping_host_keys == nmap_host_keys
