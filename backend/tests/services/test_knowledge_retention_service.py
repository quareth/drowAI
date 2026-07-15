"""Service tests for knowledge retention policy orchestration.

This module verifies behavior:
- explicit retention-class policy outputs
- dry-run safety (no writes)
- operational cleanup beyond agent_logs only
- evidence protection for active findings and replay policy"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.chat import AgentLog
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeFinding, KnowledgeIngestionRun
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.streaming import StreamEvent, SystemLog
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.data_plane.retention_service import DataPlaneRetentionService
from backend.services.knowledge.evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadService,
)
from backend.services.knowledge.retention_executor import KnowledgeRetentionExecutor
from backend.services.knowledge.retention_service import KnowledgeRetentionService
from backend.services.retention.contracts import (
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    operational_log_retention_days: int
    retention_batch_size_per_tenant: int


@pytest.fixture(autouse=True)
def _isolate_durable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_engagement_task(db):
    user = User(username=f"retention-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Retention engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, name="Retention task")
    db.add(task)
    db.flush()
    return user, engagement, task


def _seed_operational_logs(
    db,
    *,
    task_id: int,
    tenant_id: int,
    old_ts: datetime,
    new_ts: datetime,
    sequence_start: int = 1,
) -> None:
    db.add(
        AgentLog(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start,
            type="reasoning",
            content="old",
            turn_id="turn-1",
            turn_number=1,
            timestamp=old_ts,
        )
    )
    db.add(
        AgentLog(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start + 1,
            type="reasoning",
            content="new",
            turn_id="turn-2",
            turn_number=2,
            timestamp=new_ts,
        )
    )
    db.add(
        SystemLog(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start,
            type="system",
            content="old",
            timestamp=old_ts,
        )
    )
    db.add(
        SystemLog(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start + 1,
            type="system",
            content="new",
            timestamp=new_ts,
        )
    )
    db.add(
        StreamEvent(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start,
            event_type="delta",
            payload={"kind": "old"},
            created_at=old_ts,
        )
    )
    db.add(
        StreamEvent(
            task_id=task_id,
            tenant_id=tenant_id,
            sequence=sequence_start + 1,
            event_type="delta",
            payload={"kind": "new"},
            created_at=new_ts,
        )
    )


def test_retention_dry_run_is_explicit_by_class_and_no_writes() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=90)
        new_ts = now - timedelta(days=1)
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id,
            old_ts=old_ts,
            new_ts=new_ts,
        )

        active_execution_id = uuid_lib.uuid4()
        replay_execution_id = uuid_lib.uuid4()
        cold_execution_id = uuid_lib.uuid4()
        non_archived_execution_id = uuid_lib.uuid4()

        active_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=active_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="active finding evidence",
            archived_file_ref="/tmp/not-used-active",
            lineage_snapshot={"artifact_id": "active-a1"},
        )
        replay_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=replay_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="replay evidence",
            archived_file_ref="/tmp/not-used-replay",
            lineage_snapshot={"artifact_id": "replay-a1"},
        )
        cold_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=cold_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="cold evidence",
            archived_file_ref="/tmp/not-used-cold",
            lineage_snapshot={"artifact_id": "cold-a1"},
        )
        non_archived_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=non_archived_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="metadata_only",
            inline_excerpt=None,
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "meta-a1"},
        )
        db.add_all([active_evidence, replay_evidence, cold_evidence, non_archived_evidence])

        db.add(
            KnowledgeIngestionRun(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=replay_execution_id,
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0",
                status="succeeded",
                run_metadata={"candidate_extraction_mode": "candidate_replay"},
            )
        )

        db.add(
            KnowledgeFinding(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                finding_key="finding://active/1",
                finding_type="vulnerability",
                subject_type="finding.instance",
                subject_key="finding.instance:active-1",
                title="Open finding",
                severity="high",
                status="open",
                assertion_level="observed",
                confidence="high",
                first_seen_at=now - timedelta(days=5),
                last_seen_at=now - timedelta(days=1),
                evidence_summary={
                    "evidence_refs": [
                        {
                            "evidence_archive_id": str(active_evidence.id),
                            "excerpt": "active",
                        }
                    ]
                },
            )
        )
        db.commit()

        service = KnowledgeRetentionService(db, operational_retention_days=30)
        summary = service.run(dry_run=True).to_dict()

        assert summary["dry_run"] is True
        assert RETENTION_CLASS_OPERATIONAL_EPHEMERAL in summary["retention_classes"]
        assert RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE in summary["retention_classes"]

        op = summary["operational_logs"]
        assert op["candidate_total"] == 3
        assert op["deleted_total"] == 0
        assert {rule["name"] for rule in op["rules"]} == {"agent_logs", "system_logs", "stream_events"}
        assert all(
            rule["retention_class"] == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
            for rule in op["rules"]
        )

        evidence = summary["evidence_compaction"]
        assert evidence["decision_count"] == 4
        assert evidence["eligible_count"] == 1
        assert evidence["preserved_count"] == 3
        assert evidence["compacted_count"] == 0
        assert evidence["compacted_bytes"] == 0
        eligible_ids = {item["evidence_id"] for item in evidence["eligible"]}
        assert eligible_ids == {str(cold_evidence.id)}

        preserved_by_id = {item["evidence_id"]: item for item in evidence["preserved"]}
        assert preserved_by_id[str(active_evidence.id)]["action"] == "preserve_active_finding"
        assert preserved_by_id[str(replay_evidence.id)]["action"] == "preserve_replay_policy"
        assert preserved_by_id[str(non_archived_evidence.id)]["action"] == "preserve_non_archived_mode"
        assert all(
            item["retention_class"] == RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE
            for item in evidence["preserved"]
        )

        assert db.query(AgentLog).count() == 2
        assert db.query(SystemLog).count() == 2
        assert db.query(StreamEvent).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_retention_operational_cleanup_is_tenant_scoped_and_bounded() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=90)
        new_ts = now - timedelta(days=1)
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id,
            old_ts=old_ts,
            new_ts=new_ts,
        )
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id + 100,
            old_ts=old_ts,
            new_ts=new_ts,
            sequence_start=101,
        )
        db.commit()

        dry_run_summary = KnowledgeRetentionService(
            db,
            tenant_id=engagement.tenant_id,
            operational_retention_days=30,
            operational_batch_limit=2,
        ).run(dry_run=True).to_dict()
        apply_summary = KnowledgeRetentionService(
            db,
            tenant_id=engagement.tenant_id,
            operational_retention_days=30,
            operational_batch_limit=2,
        ).run(dry_run=False).to_dict()

        assert dry_run_summary["operational_logs"]["candidate_total"] == 2
        assert apply_summary["operational_logs"]["candidate_total"] == 2
        assert apply_summary["operational_logs"]["deleted_total"] == 2
        assert db.query(AgentLog).filter(AgentLog.tenant_id == engagement.tenant_id + 100).count() == 2
        assert db.query(SystemLog).filter(SystemLog.tenant_id == engagement.tenant_id + 100).count() == 2
        assert db.query(StreamEvent).filter(StreamEvent.tenant_id == engagement.tenant_id + 100).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_knowledge_retention_executor_returns_canonical_operational_result() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=90)
        new_ts = now - timedelta(days=1)
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id,
            old_ts=old_ts,
            new_ts=new_ts,
        )
        db.commit()

        executor = KnowledgeRetentionExecutor(db)
        policy = _Policy(
            operational_log_retention_days=30,
            retention_batch_size_per_tenant=2,
        )
        dry_run = executor.run(
            policy=policy,
            tenant_id=engagement.tenant_id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=50,
        )
        applied = executor.run(
            policy=policy,
            tenant_id=engagement.tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert dry_run.retention_class == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
        assert dry_run.counts.batch_limit == 2
        assert dry_run.counts.candidate_count == 2
        assert {decision.retention_class for decision in dry_run.decisions} == {
            RETENTION_CLASS_OPERATIONAL_EPHEMERAL
        }
        assert {decision.outcome for decision in dry_run.decisions} == {
            RETENTION_DECISION_CANDIDATE
        }
        assert applied.retention_class == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
        assert applied.counts.candidate_count == dry_run.counts.candidate_count
        assert applied.counts.applied_count == 2
        assert {decision.outcome for decision in applied.decisions} == {
            RETENTION_DECISION_APPLIED
        }
    finally:
        db.close()
        engine.dispose()


def test_retention_exec_compacts_only_policy_eligible_evidence() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)

        evidence_dir = WorkspaceConfig.ensure_engagement_durable_structure(engagement.id)["evidence"]
        cold_path = evidence_dir / "cold-evidence.bin"
        protected_path = evidence_dir / "protected-evidence.bin"
        cold_payload = b"cold-bytes"
        protected_payload = b"protected-bytes"
        cold_path.write_bytes(cold_payload)
        protected_path.write_bytes(protected_payload)

        cold_execution_id = uuid_lib.uuid4()
        protected_execution_id = uuid_lib.uuid4()

        cold_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=cold_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="cold",
            archived_file_ref=str(cold_path.resolve()),
            lineage_snapshot={"artifact_id": "cold-a1"},
            archive_metadata={"policy_family": "default_archive_policy"},
        )
        protected_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=protected_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="protected",
            archived_file_ref=str(protected_path.resolve()),
            lineage_snapshot={"artifact_id": "protected-a1"},
            archive_metadata={"delete_survival_required": True},
        )
        db.add_all([cold_evidence, protected_evidence])
        db.commit()

        service = KnowledgeRetentionService(db, operational_retention_days=30)
        summary = service.run(dry_run=False).to_dict()
        evidence_summary = summary["evidence_compaction"]

        assert evidence_summary["eligible_count"] == 1
        assert evidence_summary["compacted_count"] == 1
        assert evidence_summary["compacted_bytes"] >= len(cold_payload)

        cold_row = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.id == cold_evidence.id)
            .one()
        )
        assert str(cold_row.storage_mode) == "metadata_only"
        assert cold_row.inline_excerpt is None
        assert cold_row.archived_file_ref is None
        metadata = dict(cold_row.archive_metadata or {})
        compaction = dict(metadata.get("compaction") or {})
        assert compaction.get("replay_policy_status") == "not_required"
        assert compaction.get("archived_file_deleted") is True
        assert cold_path.exists() is False

        protected_row = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.id == protected_evidence.id)
            .one()
        )
        assert str(protected_row.storage_mode) == "archived_file"
        assert str(protected_row.archived_file_ref) == str(protected_path.resolve())
        assert protected_path.exists() is True
    finally:
        db.close()
        engine.dispose()


def test_retention_exec_deletes_expired_operational_logs_across_tables() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=90)
        new_ts = now - timedelta(days=1)
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id,
            old_ts=old_ts,
            new_ts=new_ts,
        )
        db.commit()

        service = KnowledgeRetentionService(db, operational_retention_days=30)
        summary = service.run(dry_run=False).to_dict()

        assert summary["dry_run"] is False
        assert summary["operational_logs"]["candidate_total"] == 3
        assert summary["operational_logs"]["deleted_total"] == 3
        per_rule_deleted = {item["name"]: item["deleted_count"] for item in summary["operational_logs"]["rules"]}
        assert per_rule_deleted["agent_logs"] == 1
        assert per_rule_deleted["system_logs"] == 1
        assert per_rule_deleted["stream_events"] == 1

        assert db.query(AgentLog).count() == 1
        assert db.query(SystemLog).count() == 1
        assert db.query(StreamEvent).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_retention_preserves_candidate_finding_evidence() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        candidate_execution_id = uuid_lib.uuid4()

        candidate_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=candidate_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="candidate finding evidence",
            archived_file_ref="/tmp/not-used-candidate",
            lineage_snapshot={"artifact_id": "candidate-a1"},
        )
        db.add(candidate_evidence)
        db.add(
            KnowledgeFinding(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                finding_key="finding://candidate/1",
                finding_type="vulnerability",
                subject_type="finding.instance",
                subject_key="finding.instance:candidate-1",
                title="Candidate finding",
                severity="medium",
                status="candidate",
                assertion_level="candidate",
                confidence="medium",
                first_seen_at=now - timedelta(days=3),
                last_seen_at=now - timedelta(days=1),
                evidence_summary={
                    "evidence_refs": [
                        {
                            "evidence_archive_id": str(candidate_evidence.id),
                            "excerpt": "candidate evidence",
                        }
                    ]
                },
            )
        )
        db.commit()

        summary = KnowledgeRetentionService(db, operational_retention_days=30).run(dry_run=True).to_dict()
        evidence = summary["evidence_compaction"]
        assert evidence["eligible_count"] == 0
        preserved_by_id = {item["evidence_id"]: item for item in evidence["preserved"]}
        assert preserved_by_id[str(candidate_evidence.id)]["action"] == "preserve_active_finding"
    finally:
        db.close()
        engine.dispose()


def test_retention_compaction_keeps_read_contract_and_replay_protected_rows() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        evidence_dir = WorkspaceConfig.ensure_engagement_durable_structure(engagement.id)["evidence"]
        cold_path = evidence_dir / "cold-readable.bin"
        replay_path = evidence_dir / "replay-protected.bin"
        cold_path.write_text("cold evidence payload", encoding="utf-8")
        replay_path.write_text("replay protected payload", encoding="utf-8")

        cold_execution_id = uuid_lib.uuid4()
        replay_execution_id = uuid_lib.uuid4()

        cold_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=cold_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="cold",
            archived_file_ref=str(cold_path.resolve()),
            lineage_snapshot={"artifact_id": "cold-read-a1"},
            archive_metadata={"policy_family": "default_archive_policy"},
        )
        replay_protected_evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=replay_execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt="replay",
            archived_file_ref=str(replay_path.resolve()),
            lineage_snapshot={"artifact_id": "replay-read-a1"},
            archive_metadata={"policy_family": "default_archive_policy"},
        )
        db.add_all([cold_evidence, replay_protected_evidence])
        db.add(
            KnowledgeIngestionRun(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=replay_execution_id,
                extractor_family="runtime.ingestion",
                extractor_version="1.0",
                status="succeeded",
                run_metadata={"replay_source_type": "durable_archive"},
            )
        )
        db.commit()

        summary = KnowledgeRetentionService(db, operational_retention_days=30).run(dry_run=False).to_dict()
        evidence_summary = summary["evidence_compaction"]
        assert evidence_summary["eligible_count"] == 1
        assert evidence_summary["compacted_count"] == 1
        preserved_by_id = {item["evidence_id"]: item for item in evidence_summary["preserved"]}
        assert preserved_by_id[str(replay_protected_evidence.id)]["action"] == "preserve_replay_policy"

        read_service = KnowledgeEvidenceReadService(db)
        cold_read = read_service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(cold_evidence.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=256),
        )
        assert cold_read.status == "not_available"
        assert cold_read.storage_mode == "metadata_only"
        assert cold_read.source == "none"

        replay_read = read_service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(replay_protected_evidence.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=256),
        )
        assert replay_read.status == "ready"
        assert replay_read.storage_mode == "archived_file"
        assert replay_read.source == "inline_excerpt"
        assert replay_read.content is not None and "replay" in replay_read.content
    finally:
        db.close()
        engine.dispose()


def test_retention_never_deletes_active_finding_evidence_lineage(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)

        execution = ToolExecution(
            tenant_id=task.tenant_id,
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat report"},
            agent_path="runner.tool_command",
            status="succeeded",
            started_at=now,
        )
        db.add(execution)
        db.flush()

        artifact_object_key = "tenants/test/tasks/test/executions/e1/artifacts/a1/report.txt"
        evidence_object_key = "tenants/test/engagements/evidence/ev-1/report.txt"
        artifact = ExecutionArtifact(
            execution_id=execution.id,
            tenant_id=task.tenant_id,
            task_id=task.id,
            artifact_kind="tool_result",
            object_key=artifact_object_key,
            upload_status="ready",
            content_sha256="a" * 64,
            byte_size=13,
            mime_type="text/plain",
            is_text=True,
        )
        db.add(artifact)
        db.flush()

        evidence = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution.id,
            source_artifact_id=artifact.id,
            storage_mode="object_ref",
            object_key=evidence_object_key,
            content_sha256="b" * 64,
            byte_size=21,
            mime_type="text/plain",
            inline_excerpt=None,
            lineage_snapshot={"artifact_id": str(artifact.id)},
            archive_metadata={"delete_survival_required": True},
        )
        db.add(evidence)
        db.add(
            KnowledgeFinding(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=user.id,
                engagement_id=engagement.id,
                finding_key="finding://active/object-ref/1",
                finding_type="vulnerability",
                subject_type="finding.instance",
                subject_key="finding.instance:active-object-ref",
                title="Active object-ref finding",
                severity="high",
                status="open",
                assertion_level="observed",
                confidence="high",
                first_seen_at=now - timedelta(days=2),
                last_seen_at=now,
                evidence_summary={
                    "evidence_refs": [
                        {
                            "evidence_archive_id": str(evidence.id),
                            "excerpt": "required",
                        }
                    ]
                },
            )
        )
        db.commit()

        store = LocalObjectStore(root_path=tmp_path / "object-store")
        store.put_bytes(artifact_object_key, b"artifact-bytes", content_type="text/plain")
        store.put_bytes(evidence_object_key, b"evidence-bytes", content_type="text/plain")

        service = KnowledgeRetentionService(
            db,
            operational_retention_days=30,
            data_plane_retention_service=DataPlaneRetentionService(db, object_store=store),
        )
        summary = service.run(dry_run=False).to_dict()

        artifact_retention = summary["artifact_object_retention"]
        assert artifact_retention["deleted_count"] == 0
        assert artifact_retention["preserved_count"] == 1
        assert artifact_retention["preserved"][0]["reason"] == "durable_evidence_policy_protected"
        assert store.head_object(artifact_object_key) is not None
        assert store.head_object(evidence_object_key) is not None
    finally:
        db.close()
        engine.dispose()


def test_retention_emits_metrics_for_deletions_and_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inc_calls: list[tuple[str, int]] = []
    gauge_calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        "backend.services.knowledge.retention_service.safe_inc",
        lambda name, value=1: inc_calls.append((str(name), int(value))),
    )
    monkeypatch.setattr(
        "backend.services.knowledge.retention_service.safe_gauge",
        lambda name, value: gauge_calls.append((str(name), float(value))),
    )

    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        now = datetime.now(timezone.utc)
        old_ts = now - timedelta(days=90)
        new_ts = now - timedelta(days=1)
        _seed_operational_logs(
            db,
            task_id=task.id,
            tenant_id=engagement.tenant_id,
            old_ts=old_ts,
            new_ts=new_ts,
        )

        evidence_dir = WorkspaceConfig.ensure_engagement_durable_structure(engagement.id)["evidence"]
        cold_path = evidence_dir / "cold-metrics.bin"
        cold_path.write_bytes(b"metrics-bytes")
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=uuid_lib.uuid4(),
                source_artifact_id=uuid_lib.uuid4(),
                storage_mode="archived_file",
                inline_excerpt="cold",
                archived_file_ref=str(cold_path.resolve()),
                lineage_snapshot={"artifact_id": "cold-metrics-a1"},
                archive_metadata={"policy_family": "default_archive_policy"},
            )
        )
        db.commit()

        summary = KnowledgeRetentionService(db, operational_retention_days=30).run(dry_run=False).to_dict()
        assert summary["operational_logs"]["deleted_total"] >= 3
        assert summary["evidence_compaction"]["compacted_count"] >= 1
    finally:
        db.close()
        engine.dispose()

    counter_totals: dict[str, int] = {}
    for name, value in inc_calls:
        counter_totals[name] = counter_totals.get(name, 0) + value
    assert counter_totals.get("knowledge_retention_deleted_total", 0) >= 1
    assert any(name == "knowledge_retention_deleted_bytes" for name, _ in gauge_calls)
    assert any(name == "knowledge_retention_duration_seconds" for name, _ in gauge_calls)
