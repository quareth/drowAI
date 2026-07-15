"""Integration tests for ingestion orchestration outcomes."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_execution(db) -> tuple[int, str]:
    user = User(username=f"tenant-baseline-ingestion-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Runtime Ingestion Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, name="Runtime Ingestion Task")
    db.add(task)
    db.flush()
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        task_id=task.id,
        tool_name="custom.unsupported_tool",
        tool_arguments={"target": "10.0.0.5"},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    db.flush()
    db.add(
        ExecutionArtifact(
            id=uuid_lib.uuid4(),
            execution_id=execution.id,
            task_id=task.id,
            artifact_kind="stdout",
            content_text="unsupported tool output",
            content_sha256="d" * 64,
            byte_size=256,
            mime_type="text/plain",
            is_text=True,
        )
    )
    db.commit()
    return int(task.id), str(execution.id)


def test_unsupported_execution_still_records_clean_zero_observation_run() -> None:
    engine, db = _build_session()
    try:
        task_id, execution_id = _seed_execution(db)
        service = KnowledgeIngestionService(db)

        result = service.ingest_execution(
            task_id=task_id,
            source_execution_id=execution_id,
            tool_name_hint="custom.unsupported_tool",
            compact_output_hint={"summary": "unsupported tool compact summary"},
            raise_on_error=True,
        )
        db.commit()

        assert result["ok"] is True
        assert result["status"] == "succeeded"
        assert int(result["archive_count"]) >= 1
        assert int(result["observation_inserted_count"]) == 0

        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        archive_count = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .count()
        )
        observation_count = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == run.id)
            .count()
        )

        assert run.status == "succeeded"
        assert archive_count >= 1
        assert observation_count == 0
    finally:
        db.close()
        engine.dispose()

