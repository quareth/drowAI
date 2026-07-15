"""Tests for deterministic Hydra backend knowledge adapter behavior."""

from __future__ import annotations

from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.hydra_adapter import HydraKnowledgeAdapter


def _build_context(
    *,
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
    execution_payload = {
        "execution": {
            "execution_id": "exec-hydra-1",
            "tool_name": "password_attacks.online_attacks.hydra",
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
        source_execution_id="exec-hydra-1",
        ingestion_run_id="run-hydra-1",
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=semantic_observations or [],
        artifact_summaries=artifacts or [],
        evidence_archives=evidence_archives,
    )


def _metadata(*, credentials: list[dict] | None = None) -> dict:
    return {
        "semantic_schema_version": "hydra.v1",
        "capability_family": "credential_attack",
        "target_info": {"host": "192.168.1.100", "port": 22},
        "attack_info": {"protocol": "ssh", "service": "ssh"},
        "credentials": credentials
        if credentials is not None
        else [
            {
                "host": "192.168.1.100",
                "port": 22,
                "protocol": "ssh",
                "service": "ssh",
                "username": "admin",
                "account_identifier": "admin",
                "password": "admin123",
            }
        ],
        "statistics": {"successful_login_count": 1},
    }


def test_hydra_adapter_prefers_semantic_observations_over_metadata() -> None:
    adapter = HydraKnowledgeAdapter()
    context = _build_context(
        tool_metadata=_metadata(credentials=[
            {
                "host": "192.168.1.100",
                "port": 22,
                "service": "ssh",
                "username": "metadata-user",
                "password": "metadata-secret",
            }
        ]),
        semantic_observations=[
            {
                "observation_type": "finding.vulnerability_confirmed",
                "subject_type": "finding.vulnerability",
                "subject_key": (
                    "finding.vulnerability:service.socket:192.168.1.100/tcp/22:hydra/weak-auth"
                ),
                "payload": {
                    "source": "hydra",
                    "detector_id": "hydra/weak-auth",
                    "subject_key": "service.socket:192.168.1.100/tcp/22",
                    "severity": "high",
                    "confidence": "confirmed",
                    "successful_login_count": 2,
                    "account_identifier": "semantic-user",
                    "password": "semantic-secret",
                },
            }
        ],
    )

    observations = adapter.extract(context)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.assertion_level == "confirmed"
    assert observation.observation_metadata["source_kind"] == "deterministic"
    assert observation.payload["account_identifier"] == "semantic-user"
    rendered = str(observation.payload)
    assert "metadata-secret" not in rendered
    assert "semantic-secret" not in rendered


def test_hydra_adapter_fallback_converts_metadata_to_confirmed_finding_with_evidence_refs() -> None:
    adapter = HydraKnowledgeAdapter()
    context = _build_context(
        tool_metadata=_metadata(),
        artifacts=[{"artifact_id": "artifact-hydra-1", "artifact_kind": "stdout"}],
    )

    observations = adapter.extract(context)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.observation_type == "finding.vulnerability_confirmed"
    assert observation.subject_key == (
        "finding.vulnerability:service.socket:192.168.1.100/tcp/22:hydra/weak-auth"
    )
    assert observation.assertion_level == "confirmed"
    assert observation.payload["subject_key"] == "service.socket:192.168.1.100/tcp/22"
    assert "severity" not in observation.payload
    assert observation.payload["finding_subtype"] == "credential_compromise_confirmed"
    assert observation.payload["confidence"] == "confirmed"
    assert observation.payload["successful_login_count"] == 1
    assert observation.payload["account_identifier"] == "admin"
    assert observation.payload["evidence_refs"] == [
        {"evidence_archive_id": "archive-artifact-hydra-1"}
    ]
    assert "admin123" not in str(observation.payload)


def test_hydra_adapter_drops_runs_without_successful_login_signal() -> None:
    adapter = HydraKnowledgeAdapter()
    context = _build_context(tool_metadata=_metadata(credentials=[]))

    assert adapter.extract(context) == []
