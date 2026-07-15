"""Tests for extensions in backfill_engagement_knowledge script.

Scope:
- Validate deterministic candidate replay batching/cursor behavior.
- Validate dry-run mode performs no durable writes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeIngestionRun
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.scripts import backfill_engagement_knowledge as script
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService


@pytest.fixture(autouse=True)
def _enable_candidate_feature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory


def _seed_user_engagement_task(db):
    user = User(username=f"candidate-replay-backfill-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Candidate Replay Backfill Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, name="Candidate Replay Backfill Task")
    db.add(task)
    db.flush()
    return engagement, task


def _seed_execution_with_id(db, *, task_id: int, execution_uuid: uuid_lib.UUID, content_text: str) -> str:
    execution = ToolExecution(
        id=execution_uuid,
        task_id=task_id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo backfill"},
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
            task_id=task_id,
            artifact_kind="stdout",
            content_text=content_text,
            content_sha256="f" * 64,
            byte_size=len(content_text.encode("utf-8")),
            mime_type="text/plain",
            is_text=True,
        )
    )
    db.flush()
    return str(execution.id)


def test_remote_runtime_replay_backfill_batch_and_cursor_are_deterministic() -> None:
    engine, session_factory = _build_session()
    db = session_factory()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        ingestion = KnowledgeIngestionService(db)
        execution_id_1 = _seed_execution_with_id(
            db,
            task_id=task.id,
            execution_uuid=uuid_lib.UUID("00000000-0000-0000-0000-000000000001"),
            content_text="first",
        )
        execution_id_2 = _seed_execution_with_id(
            db,
            task_id=task.id,
            execution_uuid=uuid_lib.UUID("00000000-0000-0000-0000-000000000002"),
            content_text="second",
        )
        for execution_id in (execution_id_1, execution_id_2):
            result = ingestion.ingest_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                extractor_family="runtime.ingestion",
                extractor_version="1.0",
                raise_on_error=True,
            )
            assert result["ok"] is True

        first_batch = script.run_remote_runtime_candidate_replay_backfill(
            db=db,
            extractor_family="runtime.ingestion",
            target_extractor_version="2.0",
            batch_size=1,
        )
        assert first_batch["ok"] is True
        assert first_batch["selected_target_count"] == 1
        assert first_batch["succeeded_count"] == 1
        assert first_batch["failed_count"] == 0
        assert first_batch["next_cursor_source_execution_id"] == execution_id_1

        second_batch = script.run_remote_runtime_candidate_replay_backfill(
            db=db,
            extractor_family="runtime.ingestion",
            target_extractor_version="2.0",
            batch_size=1,
            cursor_source_execution_id=str(first_batch["next_cursor_source_execution_id"]),
        )
        assert second_batch["ok"] is True
        assert second_batch["selected_target_count"] == 1
        assert second_batch["succeeded_count"] == 1
        assert second_batch["failed_count"] == 0
        assert second_batch["next_cursor_source_execution_id"] == execution_id_2
    finally:
        db.close()
        engine.dispose()


def test_remote_runtime_replay_backfill_main_dry_run_rolls_back_writes(monkeypatch) -> None:
    engine, session_factory = _build_session()
    db = session_factory()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_id(
            db,
            task_id=task.id,
            execution_uuid=uuid_lib.UUID("00000000-0000-0000-0000-0000000000aa"),
            content_text="dry-run",
        )
        ingestion = KnowledgeIngestionService(db)
        initial = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        assert initial["ok"] is True
        db.commit()

        before_count = (
            db.query(KnowledgeIngestionRun)
            .filter(
                KnowledgeIngestionRun.source_execution_id == execution_id,
                KnowledgeIngestionRun.extractor_family == "runtime.ingestion",
                KnowledgeIngestionRun.extractor_version == "9.9",
            )
            .count()
        )
        assert before_count == 0

        monkeypatch.setattr(script, "SessionLocal", session_factory)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "backfill_engagement_knowledge.py",
                "--mode",
                "candidate_replay",
                "--extractor-family",
                "runtime.ingestion",
                "--target-extractor-version",
                "9.9",
                "--dry-run",
                "--json",
            ],
        )
        exit_code = script.main()
        assert exit_code == 0

        verify_db = session_factory()
        try:
            after_count = (
                verify_db.query(KnowledgeIngestionRun)
                .filter(
                    KnowledgeIngestionRun.source_execution_id == execution_id,
                    KnowledgeIngestionRun.extractor_family == "runtime.ingestion",
                    KnowledgeIngestionRun.extractor_version == "9.9",
                )
                .count()
            )
            assert after_count == 0
        finally:
            verify_db.close()
    finally:
        db.close()
        engine.dispose()


def test_remote_runtime_replay_backfill_main_exits_nonzero_when_feature_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "false")
    engine, session_factory = _build_session()
    db = session_factory()
    try:
        _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_id(
            db,
            task_id=task.id,
            execution_uuid=uuid_lib.UUID("00000000-0000-0000-0000-0000000000bb"),
            content_text="feature-off",
        )
        ingestion = KnowledgeIngestionService(db)
        initial = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        assert initial["ok"] is True
        db.commit()

        monkeypatch.setattr(script, "SessionLocal", session_factory)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "backfill_engagement_knowledge.py",
                "--mode",
                "candidate_replay",
                "--json",
            ],
        )
        exit_code = script.main()
        assert exit_code == 2
    finally:
        db.close()
        engine.dispose()


def test_execution_plane_historical_main_reports_web_path_counters_via_backfill_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _FakeDb:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    fake_db = _FakeDb()
    captured_calls: list[dict[str, object]] = []

    class _StubHistoricalBackfillService:
        def __init__(self, db) -> None:
            assert db is fake_db

        def run_backfill(self, *, target_engagement_ids=None, verify_idempotent_rerun=True):
            captured_calls.append(
                {
                    "target_engagement_ids": target_engagement_ids,
                    "verify_idempotent_rerun": verify_idempotent_rerun,
                }
            )
            return {
                "ok": True,
                "completion_gate_passed": True,
                "attempted_engagement_count": 1,
                "succeeded_engagement_count": 1,
                "failed_engagement_count": 0,
                "web_path_upsert_count": 4,
                "web_path_insert_count": 2,
                "engagement_statuses": [],
                "failed_engagements": [],
            }

    monkeypatch.setattr(script, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(script, "KnowledgeHistoricalBackfillService", _StubHistoricalBackfillService)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_engagement_knowledge.py",
            "--mode",
            "historical_projection",
            "--engagement-id",
            "77",
            "--json",
        ],
    )

    exit_code = script.main()
    assert exit_code == 0
    assert captured_calls == [
        {
            "target_engagement_ids": [77],
            "verify_idempotent_rerun": True,
        }
    ]

    payload = json.loads(capsys.readouterr().out)
    assert payload["web_path_upsert_count"] == 4
    assert payload["web_path_insert_count"] == 2
