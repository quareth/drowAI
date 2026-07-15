"""Tests for semantic snapshot durability in ingestion run metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeIngestionRun
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db):
    tenant = Tenant(id=1, slug="semantic-transport", name="Semantic Transport")
    db.add(tenant)
    db.flush()
    user = User(username=f"semantic-transport-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Semantic Transport Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Semantic Transport Task",
    )
    db.add(task)
    db.flush()
    return engagement, task


def test_ingestion_run_metadata_contains_semantic_input_snapshot() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        execution = ToolExecution(
            id=uuid_lib.uuid4(),
            tenant_id=task.tenant_id,
            task_id=task.id,
            tool_name="information_gathering.network_discovery.nmap",
            tool_arguments={"target": "10.0.0.5"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            execution_metadata={
                "tool_metadata": {
                    "open_ports": [22, 443],
                    "parser": "nmap.parse_output",
                    "semantic_schema_version": "nmap.v1",
                },
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [{"evidence_kind": "service_banner", "port": 22}],
                "semantic_schema_version": "nmap.v1",
                "capability_family": "network_discovery",
            },
        )
        db.add(execution)
        db.flush()
        db.add(
            ExecutionArtifact(
                id=uuid_lib.uuid4(),
                execution_id=execution.id,
                tenant_id=task.tenant_id,
                task_id=task.id,
                artifact_kind="stdout",
                content_text="22/tcp open ssh",
                content_sha256="a" * 64,
                byte_size=15,
                mime_type="text/plain",
                is_text=True,
            )
        )
        db.flush()

        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=str(execution.id),
            raise_on_error=True,
        )

        assert result["ok"] is True
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        snapshot = run.run_metadata.get("semantic_input_snapshot")
        assert isinstance(snapshot, dict)
        assert snapshot["snapshot_schema_version"] == "1.0"
        assert snapshot["source_tool_name"] == "information_gathering.network_discovery.nmap"
        assert snapshot["capability_family"] == "network_discovery"
        assert snapshot["tool_metadata"]["open_ports"] == [22, 443]
        assert snapshot["tool_metadata"]["semantic_schema_version"] == "nmap.v1"
        assert snapshot["semantic_observations"] == [{"observation_type": "network.open_port"}]
        assert snapshot["semantic_evidence"] == [{"evidence_kind": "service_banner", "port": 22}]
        assert snapshot["semantic_schema_version"] == "nmap.v1"
        assert len(snapshot["artifact_refs"]) == 1
        assert snapshot["artifact_refs"][0]["artifact_kind"] == "stdout"
        assert snapshot["artifact_refs"][0]["mime_type"] == "text/plain"
        assert run.engagement_id == engagement.id
    finally:
        db.close()
        engine.dispose()
