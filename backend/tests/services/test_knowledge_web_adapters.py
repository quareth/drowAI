"""Tests for deterministic web adapters (gobuster, nuclei, sqlmap)."""

from __future__ import annotations

import time

from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.ffuf_adapter import FfufKnowledgeAdapter
from backend.services.knowledge.adapters.gobuster_adapter import GobusterKnowledgeAdapter
from backend.services.knowledge.adapters.nuclei_adapter import NucleiKnowledgeAdapter
from backend.services.knowledge.adapters.sqlmap_adapter import SqlmapKnowledgeAdapter
from tests.tools.fixtures.output_fixtures import load_output_fixture


def _build_context(
    *,
    tool_name: str,
    target: str,
    source_execution_id: str = "exec-web-1",
    ingestion_run_id: str = "run-web-1",
    tool_metadata: dict | None = None,
    semantic_observations: list[dict] | None = None,
    artifacts: list[dict] | None = None,
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
            "execution_id": source_execution_id,
            "tool_name": tool_name,
            "tool_arguments": {"target": target},
            "execution_metadata": execution_metadata,
        },
        "artifacts": artifacts or [],
    }
    return AdapterContext(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id=source_execution_id,
        ingestion_run_id=ingestion_run_id,
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=semantic_observations or [],
        artifact_summaries=artifacts or [],
        evidence_archives=evidence_archives,
    )


def test_gobuster_adapter_emits_web_path_discovered_from_fixture() -> None:
    adapter = GobusterKnowledgeAdapter()
    output = load_output_fixture("web_applications.web_crawlers.gobuster")
    context = _build_context(
        tool_name="web_applications.web_crawlers.gobuster",
        target="http://example.com",
        tool_metadata={},
        artifacts=[{"artifact_id": "gobuster-artifact-1", "artifact_kind": "stdout", "content_text": output}],
    )

    observations = adapter.extract(context)
    path_obs = [item for item in observations if item.observation_type == "web.path_discovered"]

    assert len(path_obs) >= 10
    assert any(item.subject_key == "web.path:http://example.com/admin" for item in path_obs)
    assert any(item.subject_key == "web.path:http://example.com/robots.txt" for item in path_obs)
    assert any(item.payload.get("status_code") == 200 for item in path_obs)


def test_gobuster_and_ffuf_same_path_share_canonical_web_path_subject_key() -> None:
    """Same discovered path should collapse in WebPathProjector by canonical URL."""
    gobuster_observations = GobusterKnowledgeAdapter().extract(
        _build_context(
            tool_name="web_applications.web_crawlers.gobuster",
            target="https://example.com/base/../",
            tool_metadata={
                "findings": [
                    {
                        "path": "/admin",
                        "status": 200,
                        "size": 512,
                    }
                ]
            },
        )
    )
    ffuf_observations = FfufKnowledgeAdapter().extract(
        _build_context(
            tool_name="web_applications.web_application_fuzzers.ffuf",
            target="https://example.com/FUZZ",
            tool_metadata={
                "results": [
                    {
                        "url": "HTTPS://Example.com:443//admin?debug=1#fragment",
                        "status": 200,
                        "length": 512,
                    }
                ]
            },
        )
    )

    gobuster_path_keys = {
        item.subject_key
        for item in gobuster_observations
        if item.observation_type == "web.path_discovered"
    }
    ffuf_path_keys = {
        item.subject_key
        for item in ffuf_observations
        if item.observation_type == "web.path_discovered"
    }

    assert gobuster_path_keys == ffuf_path_keys == {"web.path:https://example.com/admin"}


def test_nuclei_adapter_emits_vulnerability_detected_from_fixture() -> None:
    adapter = NucleiKnowledgeAdapter()
    output = load_output_fixture("web_applications.web_vulnerability_scanners.nuclei")
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="http://example.com",
        tool_metadata={},
        artifacts=[{"artifact_id": "nuclei-artifact-1", "artifact_kind": "stdout", "content_text": output}],
    )

    observations = adapter.extract(context)
    findings = [item for item in observations if item.observation_type == "finding.vulnerability_detected"]

    assert len(findings) >= 5
    assert any("cve-2021-44228" in item.subject_key for item in findings)
    assert any(item.payload.get("severity") == "critical" for item in findings)
    assert all(item.subject_type == "finding.instance" for item in findings)


def test_nuclei_key_stability_same_template_same_subject() -> None:
    adapter = NucleiKnowledgeAdapter()
    tool_metadata = {
        "results": [
            {
                "template-id": "CVE-2021-44228",
                "severity": "high",
                "matched-at": "http://example.com",
            }
        ]
    }
    ctx1 = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="http://example.com",
        tool_metadata=tool_metadata,
        artifacts=[{"artifact_id": "nuclei-artifact-a", "artifact_kind": "stdout"}],
    )
    ctx2 = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="http://example.com",
        tool_metadata={
            "results": [
                {
                    "template-id": "CVE-2021-44228",
                    "severity": "critical",
                    "matched-at": "http://example.com",
                }
            ]
        },
        artifacts=[{"artifact_id": "nuclei-artifact-b", "artifact_kind": "stdout"}],
    )

    finding1 = adapter.extract(ctx1)[0]
    finding2 = adapter.extract(ctx2)[0]

    assert finding1.subject_key == finding2.subject_key
    assert finding1.payload.get("severity") == "high"
    assert finding2.payload.get("severity") == "critical"


def test_nuclei_rescan_keeps_finding_identity_and_refreshes_lineage() -> None:
    adapter = NucleiKnowledgeAdapter()
    first_context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="http://example.com",
        source_execution_id="exec-web-rescan-1",
        ingestion_run_id="run-web-rescan-1",
        tool_metadata={
            "results": [
                {
                    "template-id": "CVE-2021-44228",
                    "severity": "medium",
                    "matched-at": "http://example.com",
                }
            ]
        },
        artifacts=[{"artifact_id": "nuclei-artifact-rescan-1", "artifact_kind": "stdout"}],
    )
    first_finding = adapter.extract(first_context)[0]

    # Simulate later scan timing so observed_at can advance across rescans.
    time.sleep(0.01)

    second_context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="http://example.com",
        source_execution_id="exec-web-rescan-2",
        ingestion_run_id="run-web-rescan-2",
        tool_metadata={
            "results": [
                {
                    "template-id": "CVE-2021-44228",
                    "severity": "critical",
                    "matched-at": "http://example.com",
                }
            ]
        },
        artifacts=[{"artifact_id": "nuclei-artifact-rescan-2", "artifact_kind": "stdout"}],
    )
    second_finding = adapter.extract(second_context)[0]

    assert first_finding.subject_key == second_finding.subject_key
    assert first_finding.observed_at < second_finding.observed_at
    assert first_finding.payload.get("evidence_refs") != second_finding.payload.get("evidence_refs")
    assert first_finding.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-nuclei-artifact-rescan-1"}
    ]
    assert second_finding.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-nuclei-artifact-rescan-2"}
    ]

    indexed_by_subject = {first_finding.subject_key: first_finding}
    indexed_by_subject[second_finding.subject_key] = second_finding
    assert len(indexed_by_subject) == 1
    assert indexed_by_subject[first_finding.subject_key].payload.get("severity") == "critical"


def test_nuclei_adapter_falls_back_to_normalized_tool_metadata_when_semantic_rows_invalid() -> None:
    adapter = NucleiKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="https://example.com",
        tool_metadata={
            "results": [
                {
                    "target_url": "https://example.com/login",
                    "template_id": "CVE-2024-0001",
                    "matcher": "default-login-page",
                    "severity": "high",
                    "title": "Exposed Default Login Page",
                    "description_summary": "Default admin login page exposed to unauthenticated users.",
                    "classification": {"cve_ids": ["CVE-2024-0001"], "cwe_ids": ["CWE-200"]},
                    "tags": ["panel", "exposure"],
                    "references": ["https://vendor.example/advisory"],
                    "matched_at": "https://example.com/login",
                    "extracted_results": ["admin portal"],
                }
            ]
        },
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "web.path",
                "subject_key": "web.path:https://example.com/invalid",
                "payload": {"source": "invalid-semantic"},
            }
        ],
        artifacts=[{"artifact_id": "nuclei-artifact-normalized", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)

    assert len(observations) == 1
    finding = observations[0]
    assert "cve-2024-0001" in finding.subject_key
    assert finding.payload.get("source") == "nuclei"
    assert finding.payload.get("title") == "Exposed Default Login Page"
    assert finding.payload.get("classification") == {
        "cve_ids": ["CVE-2024-0001"],
        "cwe_ids": ["CWE-200"],
    }
    assert finding.payload.get("tags") == ["panel", "exposure"]
    assert finding.payload.get("references") == ["https://vendor.example/advisory"]
    assert finding.payload.get("matched_at") == "https://example.com/login"
    assert finding.payload.get("extracted_results") == ["admin portal"]
    assert finding.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-nuclei-artifact-normalized"}
    ]


def test_nuclei_adapter_extracts_rich_fields_from_tool_metadata_when_semantic_observations_absent() -> None:
    """Prove adapter fallback from normalized tool_metadata.results when
    semantic_observations is empty (the common old-execution case), not just
    when semantic rows exist but are invalid."""
    adapter = NucleiKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="https://example.com",
        tool_metadata={
            "results": [
                {
                    "target_url": "https://example.com/admin",
                    "template_id": "CVE-2024-9999",
                    "matcher": "admin-panel",
                    "severity": "medium",
                    "confidence": "high",
                    "title": "Admin Panel Detected",
                    "description_summary": "Publicly accessible admin panel.",
                    "classification": {"cve_ids": ["CVE-2024-9999"], "cwe_ids": ["CWE-200"]},
                    "tags": ["panel", "admin"],
                    "references": ["https://cve.example/CVE-2024-9999"],
                    "matched_at": "https://example.com/admin",
                    "extracted_results": ["admin dashboard"],
                }
            ]
        },
        # No semantic observations at all — the standard pre-emission case
        semantic_observations=[],
        artifacts=[{"artifact_id": "nuclei-artifact-absent", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)

    assert len(observations) == 1
    finding = observations[0]
    assert finding.observation_type == "finding.vulnerability_detected"
    assert finding.subject_type == "finding.instance"
    assert "cve-2024-9999" in finding.subject_key
    assert finding.payload.get("source") == "nuclei"
    assert finding.payload.get("title") == "Admin Panel Detected"
    assert finding.payload.get("description_summary") == "Publicly accessible admin panel."
    assert finding.payload.get("classification") == {
        "cve_ids": ["CVE-2024-9999"],
        "cwe_ids": ["CWE-200"],
    }
    assert finding.payload.get("tags") == ["panel", "admin"]
    assert finding.payload.get("references") == ["https://cve.example/CVE-2024-9999"]
    assert finding.payload.get("matched_at") == "https://example.com/admin"
    assert finding.payload.get("extracted_results") == ["admin dashboard"]
    assert finding.payload.get("evidence_refs") == [
        {"evidence_archive_id": "archive-nuclei-artifact-absent"}
    ]


def test_nuclei_adapter_emits_web_path_discovered_only_for_explicit_path_discovery_rows() -> None:
    adapter = NucleiKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="https://example.com",
        tool_metadata={
            "results": [
                {
                    "target_url": "https://example.com/admin",
                    "template_id": "default-login-page",
                    "matcher": "admin-login",
                    "severity": "medium",
                    "path_discovery": True,
                    "status_code": 200,
                    "response_size": 1234,
                }
            ]
        },
        artifacts=[{"artifact_id": "nuclei-artifact-path-explicit", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)
    findings = [item for item in observations if item.observation_type == "finding.vulnerability_detected"]
    path_rows = [item for item in observations if item.observation_type == "web.path_discovered"]

    assert len(findings) == 1
    assert len(path_rows) == 1
    path_observation = path_rows[0]
    assert path_observation.subject_type == "web.path"
    assert path_observation.subject_key == "web.path:https://example.com/admin"
    assert path_observation.payload.get("path") == "/admin"
    assert path_observation.payload.get("status_code") == 200
    assert path_observation.payload.get("response_size") == 1234
    assert path_observation.payload.get("source") == "web_applications.web_vulnerability_scanners.nuclei"


def test_nuclei_adapter_does_not_emit_web_path_discovered_for_ambiguous_rows() -> None:
    adapter = NucleiKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.nuclei",
        target="https://example.com",
        tool_metadata={
            "results": [
                {
                    "target_url": "https://example.com/admin",
                    "template_id": "CVE-2024-9999",
                    "matcher": "cve-check",
                    "severity": "high",
                }
            ]
        },
        artifacts=[{"artifact_id": "nuclei-artifact-path-ambiguous", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)
    findings = [item for item in observations if item.observation_type == "finding.vulnerability_detected"]
    path_rows = [item for item in observations if item.observation_type == "web.path_discovered"]

    assert len(findings) == 1
    assert path_rows == []


def test_nuclei_and_ffuf_same_path_share_canonical_web_path_subject_key() -> None:
    """Same discovered path should collapse in WebPathProjector by canonical URL."""
    ffuf_observations = FfufKnowledgeAdapter().extract(
        _build_context(
            tool_name="web_applications.web_application_fuzzers.ffuf",
            target="https://example.com/FUZZ",
            tool_metadata={
                "results": [
                    {
                        "url": "HTTPS://Example.com:443//admin?debug=1#fragment",
                        "status": 200,
                        "length": 512,
                    }
                ]
            },
        )
    )
    nuclei_observations = NucleiKnowledgeAdapter().extract(
        _build_context(
            tool_name="web_applications.web_vulnerability_scanners.nuclei",
            target="https://example.com",
            tool_metadata={
                "results": [
                    {
                        "target_url": "https://example.com/admin",
                        "template_id": "exposed-admin-panel",
                        "severity": "info",
                        "path_discovery": True,
                        "status_code": 200,
                    }
                ]
            },
        )
    )

    ffuf_path_keys = {
        item.subject_key
        for item in ffuf_observations
        if item.observation_type == "web.path_discovered"
    }
    nuclei_path_keys = {
        item.subject_key
        for item in nuclei_observations
        if item.observation_type == "web.path_discovered"
    }

    assert ffuf_path_keys == nuclei_path_keys == {"web.path:https://example.com/admin"}


def test_sqlmap_adapter_emits_confirmed_findings_from_fixture() -> None:
    adapter = SqlmapKnowledgeAdapter()
    output = load_output_fixture("web_applications.web_vulnerability_scanners.sqlmap")
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.sqlmap",
        target="http://example.com/vuln.php?id=1",
        tool_metadata={"stdout": output, "vulnerabilities": []},
        artifacts=[{"artifact_id": "sqlmap-artifact-1", "artifact_kind": "stdout", "content_text": output}],
    )

    observations = adapter.extract(context)
    confirmed = [item for item in observations if item.observation_type == "finding.vulnerability_confirmed"]

    assert len(confirmed) >= 3
    assert all(item.subject_type == "finding.instance" for item in confirmed)
    assert all(":param-id:" in item.subject_key for item in confirmed)
    assert any("boolean-based-blind" in item.subject_key for item in confirmed)


def test_web_adapters_emit_from_semantic_observations_only() -> None:
    cases = [
        (
            GobusterKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_crawlers.gobuster",
                target="http://example.com",
                semantic_observations=[
                    {
                        "observation_type": "web.path_discovered",
                        "subject_type": "web.path",
                        "subject_key": "web.path:http://example.com/semantic-admin",
                        "payload": {"source": "semantic"},
                    }
                ],
            ),
            "web.path:http://example.com/semantic-admin",
        ),
        (
            NucleiKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_vulnerability_scanners.nuclei",
                target="http://example.com",
                semantic_observations=[
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key": "finding.instance:semantic-nuclei",
                        "payload": {"source": "semantic"},
                    }
                ],
            ),
            "finding.instance:semantic-nuclei",
        ),
        (
            SqlmapKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_vulnerability_scanners.sqlmap",
                target="http://example.com/vuln.php?id=1",
                semantic_observations=[
                    {
                        "observation_type": "finding.vulnerability_confirmed",
                        "subject_type": "finding.instance",
                        "subject_key": "finding.instance:semantic-sqlmap",
                        "payload": {"source": "semantic"},
                    }
                ],
            ),
            "finding.instance:semantic-sqlmap",
        ),
    ]

    for adapter, context, expected_subject_key in cases:
        observations = adapter.extract(context)
        assert len(observations) == 1
        assert observations[0].subject_key == expected_subject_key


def test_web_adapters_prioritize_semantic_over_metadata_and_artifacts() -> None:
    cases = [
        (
            GobusterKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_crawlers.gobuster",
                target="http://example.com",
                tool_metadata={"findings": [{"path": "/metadata-only", "status": 200}]},
                semantic_observations=[
                    {
                        "observation_type": "web.path_discovered",
                        "subject_type": "web.path",
                        "subject_key": "web.path:http://example.com/semantic-only",
                        "payload": {"source": "semantic-priority"},
                    }
                ],
                artifacts=[
                    {
                        "artifact_id": "artifact-gobuster-priority",
                        "artifact_kind": "stdout",
                        "content_text": "/artifact-only (Status: 200) [Size: 123]",
                    }
                ],
            ),
            "web.path:http://example.com/semantic-only",
            "web_applications.web_crawlers.gobuster",
        ),
        (
            NucleiKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_vulnerability_scanners.nuclei",
                target="http://example.com",
                tool_metadata={
                    "results": [
                        {
                            "template-id": "CVE-2021-44228",
                            "matched-at": "http://example.com",
                        }
                    ]
                },
                semantic_observations=[
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key": "finding.instance:semantic-priority-nuclei",
                        "payload": {"source": "semantic-priority"},
                    }
                ],
                artifacts=[
                    {
                        "artifact_id": "artifact-nuclei-priority",
                        "artifact_kind": "stdout",
                        "content_text": "[CVE-2021-44228] [high] http://example.com",
                    }
                ],
            ),
            "finding.instance:semantic-priority-nuclei",
            "semantic-priority",
        ),
        (
            SqlmapKnowledgeAdapter(),
            _build_context(
                tool_name="web_applications.web_vulnerability_scanners.sqlmap",
                target="http://example.com/vuln.php?id=1",
                tool_metadata={"vulnerabilities": [{"parameter": "id", "type": "boolean-based blind"}]},
                semantic_observations=[
                    {
                        "observation_type": "finding.vulnerability_confirmed",
                        "subject_type": "finding.instance",
                        "subject_key": "finding.instance:semantic-priority-sqlmap",
                        "payload": {"source": "semantic-priority"},
                    }
                ],
                artifacts=[
                    {
                        "artifact_id": "artifact-sqlmap-priority",
                        "artifact_kind": "stdout",
                        "content_text": "sqlmap identified the following injection point",
                    }
                ],
            ),
            "finding.instance:semantic-priority-sqlmap",
            "semantic-priority",
        ),
    ]

    for adapter, context, expected_subject_key, expected_source in cases:
        observations = adapter.extract(context)
        assert len(observations) == 1
        assert observations[0].subject_key == expected_subject_key
        assert observations[0].payload.get("source") == expected_source


def test_sqlmap_adapter_falls_back_when_semantic_rows_are_invalid() -> None:
    adapter = SqlmapKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_vulnerability_scanners.sqlmap",
        target="http://example.com/vuln.php?id=1",
        tool_metadata={"vulnerabilities": [{"parameter": "id", "type": "boolean-based blind"}]},
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_confirmed",
                "subject_type": "finding.instance",
                "subject_key": "",
                "payload": {"source": "invalid-semantic"},
            }
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key != ""
    assert observations[0].payload.get("source") == "sqlmap"
