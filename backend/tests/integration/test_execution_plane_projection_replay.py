"""Integration tests for projection integration and replay repair behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeAsset, KnowledgeFinding, KnowledgeIngestionRun, KnowledgeRelationship, KnowledgeService
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.projection_service import KnowledgeProjectionService
from backend.services.knowledge.replay_service import KnowledgeReplayService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db):
    user = User(username=f"execution-plane-projection-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Execution Plane Projection Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, name="Execution Plane Projection Task")
    db.add(task)
    db.flush()
    return engagement, task


def _seed_nmap_execution(
    db,
    *,
    task_id: int,
    target_ip: str = "10.10.10.5",
    port: int = 443,
    service_name: str = "https",
) -> str:
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        task_id=task_id,
        tool_name="information_gathering.network_discovery.nmap",
        tool_arguments={"target": target_ip},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata={
            "tool_metadata": {
                "hosts": [
                    {
                        "ip": target_ip,
                        "ports": [
                            {"port": port, "protocol": "tcp", "service": service_name},
                        ],
                    }
                ]
            },
            "capability_family": "network_discovery",
        },
    )
    db.add(execution)
    db.flush()
    stdout = f"{port}/tcp open {service_name}"
    db.add(
        ExecutionArtifact(
            id=uuid_lib.uuid4(),
            execution_id=execution.id,
            task_id=task_id,
            artifact_kind="stdout",
            content_text=stdout,
            content_sha256="d" * 64,
            byte_size=len(stdout.encode("utf-8")),
            mime_type="text/plain",
            is_text=True,
        )
    )
    db.flush()
    return str(execution.id)


def _semantic_snapshot(db):
    return {
        "assets": sorted(
            (row.asset_key, row.asset_type, row.ip_address, row.hostname, row.status)
            for row in db.query(KnowledgeAsset).all()
        ),
        "services": sorted(
            (row.service_key, row.protocol, row.port, row.service_name, row.status)
            for row in db.query(KnowledgeService).all()
        ),
        "findings": sorted(
            (row.finding_key, row.finding_type, row.subject_key, row.status, row.assertion_level)
            for row in db.query(KnowledgeFinding).all()
        ),
        "relationships": sorted(
            (row.relationship_key, row.source_subject_key, row.relationship_type, row.target_subject_key)
            for row in db.query(KnowledgeRelationship).all()
        ),
    }


class _TransientFailingProjectionService:
    """Fail first projection call, then delegate to the real projector."""

    def __init__(self, db):
        self._inner = KnowledgeProjectionService(db)
        self.calls = 0

    def project_observations(self, *, engagement_id: int, observations):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient projection failure")
        return self._inner.project_observations(engagement_id=engagement_id, observations=observations)


class _PersistentFailingProjectionService:
    """Always fail projection to exercise repair-required metadata path."""

    def project_observations(self, *, engagement_id: int, observations):
        raise RuntimeError("persistent projection failure")


class _PartialWriteFailProjectionService:
    """Write one row then fail; savepoint rollback must remove partial write."""

    def __init__(self, db):
        self.db = db

    def project_observations(self, *, engagement_id: int, observations):
        self.db.add(
            KnowledgeAsset(
                engagement_id=int(engagement_id),
                asset_key="host.ip:203.0.113.99",
                asset_type="host.ip",
                first_seen_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        self.db.flush()
        raise RuntimeError("projection failed after partial write")


def test_ingestion_integration_projects_read_models_and_records_projection_metadata() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id)
        service = KnowledgeIngestionService(db)

        result = service.ingest_execution(task_id=task.id, source_execution_id=execution_id, raise_on_error=True)
        assert result["ok"] is True
        assert result["projection_status"] == "succeeded"
        assert result["asset_upsert_count"] >= 1
        assert result["service_upsert_count"] >= 1

        run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == result["ingestion_run_id"]).one()
        assert run.run_metadata["projection_status"] == "succeeded"
        assert int(run.run_metadata["asset_upsert_count"]) >= 1
        assert int(run.run_metadata["service_upsert_count"]) >= 1
        assert db.query(KnowledgeAsset).count() >= 1
        assert db.query(KnowledgeService).count() >= 1
    finally:
        db.close()
        engine.dispose()


def test_replay_rebuilds_read_models_with_newer_extractor_version() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id, target_ip="10.10.10.9", port=22, service_name="ssh")
        ingestion = KnowledgeIngestionService(db)
        initial = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        assert initial["ok"] is True
        assert db.query(KnowledgeAsset).count() >= 1

        db.query(KnowledgeRelationship).delete()
        db.query(KnowledgeFinding).delete()
        db.query(KnowledgeService).delete()
        db.query(KnowledgeAsset).delete()
        db.flush()
        assert db.query(KnowledgeAsset).count() == 0

        replay = KnowledgeReplayService(db, ingestion_service=ingestion).replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            target_extractor_version="1.1",
        )
        assert replay["ok"] is True
        assert replay["projection_status"] == "succeeded"
        assert db.query(KnowledgeAsset).count() >= 1
        assert db.query(KnowledgeService).count() >= 1
    finally:
        db.close()
        engine.dispose()


def test_projection_failure_sets_repair_required_and_routes_to_rebuild_owner() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id)
        service = KnowledgeIngestionService(db, projection_service=_PersistentFailingProjectionService())

        result = service.ingest_execution(task_id=task.id, source_execution_id=execution_id, raise_on_error=False)
        assert result["ok"] is False
        run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == result["ingestion_run_id"]).one()
        metadata = dict(run.run_metadata or {})
        assert metadata["projection_status"] == "failed"
        assert metadata["semantic_status"] == "failed"
        assert metadata["repair_required"] is True
        assert metadata["repair_owner"] == "knowledge_read_model_rebuild_service"
        assert isinstance(metadata.get("semantic_metrics"), dict)
        assert isinstance(metadata.get("projection_error"), str) and metadata["projection_error"]
    finally:
        db.close()
        engine.dispose()


def test_projection_failure_rolls_back_partial_projection_writes_for_same_run() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id)
        service = KnowledgeIngestionService(db, projection_service=_PartialWriteFailProjectionService(db))

        result = service.ingest_execution(task_id=task.id, source_execution_id=execution_id, raise_on_error=False)
        assert result["ok"] is False
        assert db.query(KnowledgeAsset).count() == 0
        assert db.query(KnowledgeService).count() == 0
        assert db.query(KnowledgeFinding).count() == 0
        assert db.query(KnowledgeRelationship).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_transient_projection_failure_retries_once_and_clears_repair_required() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id)
        transient = _TransientFailingProjectionService(db)
        service = KnowledgeIngestionService(db, projection_service=transient)

        result = service.ingest_execution(task_id=task.id, source_execution_id=execution_id, raise_on_error=True)
        assert result["ok"] is True
        run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == result["ingestion_run_id"]).one()
        metadata = dict(run.run_metadata or {})
        assert metadata["projection_status"] == "succeeded"
        assert int(metadata["projection_attempt_count"]) == 2
        assert metadata["repair_required"] is False
    finally:
        db.close()
        engine.dispose()


def test_replay_repair_path_clears_repair_required_after_successful_projection() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(db, task_id=task.id, target_ip="10.10.10.77", port=8080, service_name="http")
        failing = KnowledgeIngestionService(db, projection_service=_PersistentFailingProjectionService())
        failed = failing.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=False,
        )
        assert failed["ok"] is False
        failed_run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == failed["ingestion_run_id"]).one()
        assert dict(failed_run.run_metadata or {}).get("repair_required") is True

        healthy = KnowledgeIngestionService(db)
        replay = KnowledgeReplayService(db, ingestion_service=healthy).replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            target_extractor_version="1.1",
        )
        assert replay["ok"] is True
        repaired_run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"]).one()
        metadata = dict(repaired_run.run_metadata or {})
        assert metadata["projection_status"] == "succeeded"
        assert metadata["repair_required"] is False
    finally:
        db.close()
        engine.dispose()


def test_replay_runtime_and_post_delete_durable_sources_produce_same_semantic_outcome() -> None:
    engine, db = _build_session()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_nmap_execution(
            db,
            task_id=task.id,
            target_ip="10.10.90.9",
            port=8443,
            service_name="https-alt",
        )
        ingestion = KnowledgeIngestionService(db)
        initial = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True

        db.query(KnowledgeRelationship).delete()
        db.query(KnowledgeFinding).delete()
        db.query(KnowledgeService).delete()
        db.query(KnowledgeAsset).delete()
        db.flush()

        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion)
        runtime_replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            target_extractor_version="1.1",
        )
        assert runtime_replay["ok"] is True
        assert runtime_replay["replay_source_type"] == "runtime"
        runtime_snapshot = _semantic_snapshot(db)

        db.query(KnowledgeRelationship).delete()
        db.query(KnowledgeFinding).delete()
        db.query(KnowledgeService).delete()
        db.query(KnowledgeAsset).delete()
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        durable_replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            target_extractor_version="1.2",
        )
        assert durable_replay["ok"] is True
        assert durable_replay["replay_source_type"] == "durable_archive"
        durable_snapshot = _semantic_snapshot(db)

        runtime_run = db.query(KnowledgeIngestionRun).filter(
            KnowledgeIngestionRun.id == runtime_replay["ingestion_run_id"]
        ).one()
        durable_run = db.query(KnowledgeIngestionRun).filter(
            KnowledgeIngestionRun.id == durable_replay["ingestion_run_id"]
        ).one()
        assert dict(runtime_run.run_metadata or {}).get("replay_source_type") == "runtime"
        assert dict(durable_run.run_metadata or {}).get("replay_source_type") == "durable_archive"
        assert runtime_snapshot == durable_snapshot
    finally:
        db.close()
        engine.dispose()
