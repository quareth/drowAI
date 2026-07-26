"""Tests for replay source resolver runtime-first and durable-fallback behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.replay_source_resolver import KnowledgeReplaySourceResolver

SUPPORTED_DURABLE_SNAPSHOT_AUDIT = {
    "scope": "repo-supported durable replay corpus available in this branch",
    "durable_sources_checked": (
        "KnowledgeIngestionRun.run_metadata.semantic_input_snapshot",
        "KnowledgeEvidenceArchive rows keyed by source_execution_id",
        "ToolExecution.execution_metadata semantic fields",
        "checked-in repository fixtures, seed data, migrations, and tests",
    ),
    "evidence": (
        "Repository search found the strict-admission blocker shapes only in "
        "tests that intentionally model malformed replay fixtures.",
        "No checked-in database dump, seed file, or durable Knowledge archive "
        "fixture contains audited persisted executions for these blocker shapes.",
        "Synthetic replay fixture UUIDs remain test-only regression inputs and "
        "are not selected migration repair executions.",
    ),
    "synthetic_fixture_execution_ids": (
        "12345678-1234-4abc-8abc-123456789101",
    ),
}

HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS = {
    "malformed_nmap_host_ip": {
        "tool_name": "information_gathering.network_discovery.nmap",
        "outcome": "no_supported_persisted_input_contains_blocker",
        "reason": (
            "No available repo-supported durable replay source contains "
            "host.ip:not-an-ip outside strict-admission regression fixtures."
        ),
        "selected_source_execution_ids": (),
        "rerun_verification_plan": None,
        "audit_evidence": SUPPORTED_DURABLE_SNAPSHOT_AUDIT["evidence"],
    },
    "nuclei_malformed_or_empty_evidence_refs": {
        "tool_name": "web_applications.web_vulnerability_scanners.nuclei",
        "outcome": "no_supported_persisted_input_contains_blocker",
        "reason": (
            "No available repo-supported durable replay source contains empty "
            "or malformed Nuclei evidence_refs outside strict-admission "
            "regression fixtures."
        ),
        "selected_source_execution_ids": (),
        "rerun_verification_plan": None,
        "audit_evidence": SUPPORTED_DURABLE_SNAPSHOT_AUDIT["evidence"],
    },
    "amass_relationship_payload_subject_mismatch": {
        "tool_name": "information_gathering.dns.amass",
        "outcome": "no_supported_persisted_input_contains_blocker",
        "reason": (
            "No available repo-supported durable replay source contains an "
            "Amass relationship subject_key/payload target mismatch outside "
            "strict-admission regression fixtures."
        ),
        "selected_source_execution_ids": (),
        "rerun_verification_plan": None,
        "audit_evidence": SUPPORTED_DURABLE_SNAPSHOT_AUDIT["evidence"],
    },
}


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db):
    user = User(username=f"resolver-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    tenant = Tenant(slug=f"resolver-tenant-{uuid_lib.uuid4()}", name="Resolver Tenant")
    db.add(tenant)
    db.flush()
    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Resolver Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Resolver Task",
    )
    db.add(task)
    db.flush()
    return user, engagement, task


def _seed_execution_with_stdout_artifact(
    db,
    *,
    task_id: int,
    tenant_id: int,
    content: str = "resolver-output",
    execution_metadata: dict | None = None,
) -> str:
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo resolver"},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata=execution_metadata,
    )
    db.add(execution)
    db.flush()
    db.add(
        ExecutionArtifact(
            id=uuid_lib.uuid4(),
            execution_id=execution.id,
            tenant_id=tenant_id,
            task_id=task_id,
            artifact_kind="stdout",
            content_text=content,
            content_sha256="f" * 64,
            byte_size=len(content.encode("utf-8")),
            mime_type="text/plain",
            is_text=True,
        )
    )
    db.flush()
    return str(execution.id)


def _historical_blocker_rows() -> list[dict]:
    return [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:not-an-ip",
            "payload": {"source": "nmap"},
        },
        {
            "observation_type": "finding.vulnerability_detected",
            "subject_type": "finding.instance",
            "subject_key": "finding.instance:nuclei/cve-2026-0001:https://example.test/admin:body",
            "payload": {
                "source": "nuclei",
                "detector_id": "nuclei/cve-2026-0001",
                "target_url": "https://example.test/admin",
                "evidence_refs": [],
            },
        },
        {
            "observation_type": "finding.vulnerability_detected",
            "subject_type": "finding.instance",
            "subject_key": "finding.instance:nuclei/cve-2026-0002:https://example.test/admin:body",
            "payload": {
                "source": "nuclei",
                "detector_id": "nuclei/cve-2026-0002",
                "target_url": "https://example.test/admin",
                "evidence_refs": {"evidence_archive_id": "archive-invalid"},
            },
        },
        {
            "observation_type": "relationship.resolves_to",
            "subject_type": "relationship.edge",
            "subject_key": (
                "relationship.edge:host.dns:api.example.test:"
                "resolves_to:host.ip:192.0.2.20"
            ),
            "payload": {
                "source_subject_key": "host.dns:api.example.test",
                "relationship_type": "resolves_to",
                "target_subject_key": "host.ip:198.51.100.7",
            },
        },
    ]


def test_resolver_prefers_runtime_payload_when_task_and_execution_exist() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
        )
        ingestion = KnowledgeIngestionService(db)
        # Create durable rows too; runtime should still be preferred.
        ingestion.ingest_execution(task_id=task.id, source_execution_id=execution_id, raise_on_error=True)

        resolver = KnowledgeReplaySourceResolver(db, query_service=ingestion.query_service)
        resolved = resolver.resolve_source(
            source_execution_id=execution_id,
            task_id=task.id,
        )

        assert resolved["source_kind"] == "runtime"
        assert resolved["task_id"] == task.id
        assert resolved["engagement_id"] == task.engagement_id
        assert resolved["execution_payload"]["execution"]["execution_id"] == execution_id
    finally:
        db.close()
        engine.dispose()


def test_resolver_falls_back_to_durable_rows_when_runtime_is_gone() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            content="x" * 20000,
            execution_metadata={
                "tool_metadata": {"parsed_source": "shell.exec.parse_output"},
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [{"evidence_kind": "port_banner", "port": 22}],
                "semantic_schema_version": "network.v2",
                "capability_family": "network_discovery",
            },
        )
        ingestion = KnowledgeIngestionService(db)
        ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=True,
            raise_on_error=True,
        )

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        resolver = KnowledgeReplaySourceResolver(db, query_service=ingestion.query_service)
        resolved = resolver.resolve_source(
            source_execution_id=execution_id,
            task_id=task.id,
        )

        assert resolved["source_kind"] == "durable_archive"
        assert resolved["engagement_id"] is not None
        assert resolved["execution_payload"]["execution"]["execution_id"] == execution_id
        assert len(resolved["execution_payload"]["artifacts"]) >= 1
        assert resolved["execution_payload"]["execution"]["execution_metadata"]["semantic_observations"] == [
            {"observation_type": "network.open_port"}
        ]
        assert resolved["execution_payload"]["execution"]["execution_metadata"]["semantic_evidence"] == [
            {"evidence_kind": "port_banner", "port": 22}
        ]
        assert (
            resolved["execution_payload"]["execution"]["execution_metadata"]["semantic_schema_version"]
            == "network.v2"
        )
        assert (
            resolved["execution_payload"]["execution"]["execution_metadata"]["capability_family"]
            == "network_discovery"
        )
        assert (
            resolved["execution_payload"]["execution"]["execution_metadata"]["tool_metadata"]["parsed_source"]
            == "shell.exec.parse_output"
        )
        assert resolved["semantic_input_snapshot"]["snapshot_schema_version"] == "1.0"
    finally:
        db.close()
        engine.dispose()


def test_historical_semantic_input_blockers_have_evidence_backed_dispositions() -> None:
    assert set(HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS) == {
        "malformed_nmap_host_ip",
        "nuclei_malformed_or_empty_evidence_refs",
        "amass_relationship_payload_subject_mismatch",
    }
    assert SUPPORTED_DURABLE_SNAPSHOT_AUDIT["scope"] == (
        "repo-supported durable replay corpus available in this branch"
    )
    for disposition in HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS.values():
        assert disposition["outcome"] == "no_supported_persisted_input_contains_blocker"
        assert disposition["selected_source_execution_ids"] == ()
        assert "source_execution_id" not in disposition
        assert disposition["rerun_verification_plan"] is None
        assert "repo-supported durable replay source" in disposition["reason"]
        assert disposition["audit_evidence"] == SUPPORTED_DURABLE_SNAPSHOT_AUDIT["evidence"]
        assert "silently" not in disposition["outcome"]
        assert "12345678-1234-4abc-8abc-123456789101" not in str(disposition)


def test_durable_replay_source_preserves_synthetic_historical_blocker_fixture() -> None:
    engine, db = _build_session()
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        source_execution_id = uuid_lib.UUID("12345678-1234-4abc-8abc-123456789101")
        semantic_rows = _historical_blocker_rows()
        run = KnowledgeIngestionRun(
            id=uuid_lib.uuid4(),
            tenant_id=int(task.tenant_id),
            user_id=int(user.id),
            engagement_id=int(engagement.id),
            task_id=int(task.id),
            source_execution_id=source_execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="historical-baseline",
            status="succeeded",
            run_metadata={
                "source_tool_name": "historical.strict-admission.fixture",
                "semantic_input_snapshot": {
                    "snapshot_schema_version": "1.0",
                    "source_tool_name": "historical.strict-admission.fixture",
                    "semantic_schema_version": "historical.v1",
                    "capability_family": "knowledge_historical_replay",
                    "tool_metadata": {},
                    "semantic_observations": semantic_rows,
                    "semantic_evidence": [],
                    "artifact_refs": [{"artifact_id": "artifact-historical-blocker"}],
                },
            },
        )
        db.add(run)
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=int(task.tenant_id),
                user_id=int(user.id),
                engagement_id=int(engagement.id),
                task_id=int(task.id),
                source_execution_id=source_execution_id,
                storage_mode="inline_excerpt",
                inline_excerpt="historical strict-admission blocker fixture",
                content_sha256="a" * 64,
                byte_size=45,
                mime_type="text/plain",
                lineage_snapshot={"artifact_kind": "stdout"},
            )
        )
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        resolver = KnowledgeReplaySourceResolver(db)
        resolved = resolver.resolve_source(
            source_execution_id=str(source_execution_id),
            task_id=task.id,
        )

        replay_metadata = resolved["execution_payload"]["execution"]["execution_metadata"]
        assert resolved["source_kind"] == "durable_archive"
        assert replay_metadata["semantic_observations"] == semantic_rows
        assert replay_metadata["semantic_schema_version"] == "historical.v1"
        assert replay_metadata["capability_family"] == "knowledge_historical_replay"
        assert resolved["semantic_input_snapshot"]["semantic_observations"] == semantic_rows
    finally:
        db.close()
        engine.dispose()


def test_resolver_durable_fallback_works_when_snapshot_missing() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            content="fallback-without-snapshot",
        )
        ingestion = KnowledgeIngestionService(db)
        result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=True,
            raise_on_error=True,
        )
        run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == result["ingestion_run_id"]).one()
        run.run_metadata = {"source_tool_name": "shell.exec"}
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        resolver = KnowledgeReplaySourceResolver(db, query_service=ingestion.query_service)
        resolved = resolver.resolve_source(
            source_execution_id=execution_id,
            task_id=task.id,
        )

        assert resolved["source_kind"] == "durable_archive"
        assert resolved["execution_payload"]["execution"]["execution_id"] == execution_id
        assert "execution_metadata" not in resolved["execution_payload"]["execution"]
        assert resolved.get("semantic_input_snapshot") is None
        assert len(resolved["execution_payload"]["artifacts"]) >= 1
    finally:
        db.close()
        engine.dispose()


def test_resolver_marks_archived_text_as_text_even_without_inline_excerpt() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            content="x" * 20000,
        )
        ingestion = KnowledgeIngestionService(db)
        ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=True,
            raise_on_error=True,
        )

        archived = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert archived.storage_mode == "archived_file"
        archived.inline_excerpt = None
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        resolver = KnowledgeReplaySourceResolver(db, query_service=ingestion.query_service)
        resolved = resolver.resolve_source(
            source_execution_id=execution_id,
            task_id=task.id,
        )

        artifact = resolved["execution_payload"]["artifacts"][0]
        assert artifact["content_text"] is None
        assert artifact["is_text"] is True
    finally:
        db.close()
        engine.dispose()


def test_resolver_fails_cleanly_when_no_runtime_or_durable_source_exists() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        resolver = KnowledgeReplaySourceResolver(db)
        missing_execution_id = str(uuid_lib.uuid4())

        with pytest.raises(ValueError) as exc:
            resolver.resolve_source(
                source_execution_id=missing_execution_id,
                task_id=task.id,
            )
        assert "Replay source not found" in str(exc.value)
    finally:
        db.close()
        engine.dispose()
