"""Integration tests for canonical semantic ingestion flow."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant, TenantMembership
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.replay_service import KnowledgeReplayService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db):
    user = User(username=f"execution-plane-semantic-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    tenant = Tenant(
        slug=f"execution-plane-semantic-{uuid_lib.uuid4()}",
        name="Execution Plane Semantic Tenant",
    )
    db.add(tenant)
    db.flush()
    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Execution Plane Semantic Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Execution Plane Semantic Task",
    )
    db.add(task)
    db.flush()
    return engagement, task


def _seed_execution(
    db,
    *,
    task_id: int,
    tool_name: str,
    tool_arguments: dict,
    execution_metadata: dict | None = None,
    stdout: str = "",
) -> str:
    tenant_id = db.query(Task.tenant_id).filter(Task.id == int(task_id)).scalar()
    if tenant_id is None:
        raise ValueError(f"Task {task_id} has no tenant_id")
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=int(tenant_id),
        task_id=task_id,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata=execution_metadata or {},
    )
    db.add(execution)
    db.flush()
    if stdout:
        db.add(
            ExecutionArtifact(
                id=uuid_lib.uuid4(),
                execution_id=execution.id,
                tenant_id=int(tenant_id),
                task_id=task_id,
                artifact_kind="stdout",
                content_text=stdout,
                content_sha256="b" * 64,
                byte_size=len(stdout.encode("utf-8")),
                mime_type="text/plain",
                is_text=True,
            )
        )
        db.flush()
    return str(execution.id)


def _semantic_metadata(
    rows: list[dict],
    *,
    capability_family: str = "network_discovery",
    schema_version: str = "integration-test.v1",
) -> dict:
    return {
        "semantic_observations": rows,
        "semantic_evidence": [],
        "semantic_schema_version": schema_version,
        "capability_family": capability_family,
    }


def _host_discovered_row(ip: str) -> dict:
    return {
        "observation_type": "network.host_discovered",
        "subject_type": "host.ip",
        "subject_key": f"host.ip:{ip}",
        "payload": {"ip": ip, "source": "integration-test"},
    }


def _open_port_row(ip: str, port: int, *, service_name: str) -> dict:
    return {
        "observation_type": "network.open_port",
        "subject_type": "service.socket",
        "subject_key": f"service.socket:{ip}/tcp/{port}",
        "payload": {
            "ip": ip,
            "protocol": "tcp",
            "port": port,
            "service_name": service_name,
            "source": "integration-test",
        },
    }


def test_canonical_ingestion_writes_observations_and_fact_stats_for_supported_tool() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution(
            db,
            task_id=task.id,
            tool_name="information_gathering.network_discovery.nmap",
            tool_arguments={"target": "10.10.10.5"},
            execution_metadata=_semantic_metadata(
                [
                    _host_discovered_row("10.10.10.5"),
                    _open_port_row("10.10.10.5", 443, service_name="https"),
                ],
                schema_version="nmap.v1",
            ),
            stdout="443/tcp open https",
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["observation_inserted_count"] >= 2

        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(run.run_metadata or {})
        stats = dict(run_metadata.get("fact_stats") or {})
        assert "adapter_stats" not in run_metadata
        assert stats["source_tool_name"] == "information_gathering.network_discovery.nmap"
        assert stats["fact_input_count"] == 2
        assert stats["fact_accepted_count"] == 2
        assert stats["observation_count_total"] >= 2
    finally:
        db.close()
        engine.dispose()


def test_canonical_ingestion_keeps_no_semantic_facts_as_clean_zero_observation_run() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo hi"},
            execution_metadata={"tool_metadata": {"parser": "shell"}},
            stdout="hi",
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["observation_inserted_count"] == 0
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(run.run_metadata or {})
        stats = dict(run_metadata.get("fact_stats") or {})
        assert "adapter_stats" not in run_metadata
        assert stats["fact_input_count"] == 0
        assert stats["observation_count_total"] == 0
    finally:
        db.close()
        engine.dispose()


def test_replay_uses_canonical_semantic_snapshot_after_task_delete() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution(
            db,
            task_id=task.id,
            tool_name="information_gathering.network_discovery.nmap",
            tool_arguments={"target": "10.10.10.9"},
            execution_metadata=_semantic_metadata(
                [
                    _host_discovered_row("10.10.10.9"),
                    _open_port_row("10.10.10.9", 22, service_name="ssh"),
                ],
                schema_version="nmap.v1",
            ),
            stdout="22/tcp open ssh",
        )
        ingestion = KnowledgeIngestionService(db)
        initial = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True
        assert initial["observation_inserted_count"] >= 2

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion)
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )
        assert replay["ok"] is True

        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        replay_metadata = dict(replay_run.run_metadata or {})
        stats = dict(replay_metadata.get("fact_stats") or {})
        assert "adapter_stats" not in replay_metadata
        assert stats["source_tool_name"] == "information_gathering.network_discovery.nmap"
        assert stats["fact_accepted_count"] >= 2
        inserted = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == replay_run.id)
            .count()
        )
        assert inserted >= 2
    finally:
        db.close()
        engine.dispose()
