"""Tests for bounded engagement-scoped durable evidence reads."""

from __future__ import annotations

from pathlib import Path
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.services.knowledge.candidate_extraction.contracts import CandidateExtractionRequest
from backend.services.knowledge.candidate_extraction.evidence_reader import CandidateEvidenceCollector
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadService,
)


@pytest.fixture(autouse=True)
def _isolate_durable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_engagement(db):
    user = User(username="knowledge-evidence-read-user", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Read Engagement", status="active")
    db.add(engagement)
    db.flush()
    return user, engagement


def test_inline_excerpt_read_returns_bounded_content() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="ABCDEFGHIJ",
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "a1"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=4),
        )

        assert result.status == "ready"
        assert result.source == "inline_excerpt"
        assert result.mode_used == "head"
        assert result.content == "ABCD"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_read_evidence_rejects_same_tenant_non_owner_user() -> None:
    engine, db = _build_session()
    try:
        owner, engagement = _seed_user_engagement(db)
        teammate = User(username="knowledge-evidence-read-teammate", password="secret")
        db.add(teammate)
        db.flush()
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="owner-only",
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "owner-only"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        denied = service.read_evidence(
            tenant_id=engagement.tenant_id,
            user_id=teammate.id,
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=32),
        )
        allowed = service.read_evidence(
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=32),
        )

        assert denied.status == "not_found"
        assert denied.content is None
        assert allowed.status == "ready"
        assert allowed.content == "owner-only"
    finally:
        db.close()
        engine.dispose()


def test_archived_file_read_is_bounded_and_scoped_to_durable_root() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        durable_paths = WorkspaceConfig.ensure_engagement_durable_structure(engagement.id)
        archived_path = durable_paths["evidence"] / "evidence.txt"
        archived_path.write_text("1234567890", encoding="utf-8")

        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt=None,
            archived_file_ref=str(archived_path.resolve()),
            lineage_snapshot={"artifact_id": "a2"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=5),
        )

        assert result.status == "ready"
        assert result.source == "archived_file"
        assert result.mode_used == "head"
        assert result.content == "12345"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_metadata_only_returns_not_available() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="metadata_only",
            inline_excerpt=None,
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "a3"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="auto", max_chars=32),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
    finally:
        db.close()
        engine.dispose()


def test_archived_file_path_escape_or_out_of_root_is_rejected() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        root = WorkspaceConfig.get_engagement_durable_root_path(engagement.id)
        root.mkdir(parents=True, exist_ok=True)

        outside_path = root.parent / "outside.txt"
        outside_path.write_text("secret", encoding="utf-8")

        escaped_ref_row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt=None,
            archived_file_ref="../outside.txt",
            lineage_snapshot={"artifact_id": "a4"},
        )
        absolute_outside_row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="archived_file",
            inline_excerpt=None,
            archived_file_ref=str(outside_path.resolve()),
            lineage_snapshot={"artifact_id": "a5"},
        )
        db.add(escaped_ref_row)
        db.add(absolute_outside_row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        escaped_result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(escaped_ref_row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=32),
        )
        absolute_result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(absolute_outside_row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=32),
        )

        assert escaped_result.status == "not_available"
        assert escaped_result.content is None
        assert absolute_result.status == "not_available"
        assert absolute_result.content is None
    finally:
        db.close()
        engine.dispose()


def test_match_mode_returns_context_and_truncation() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        text = "0123456789MATCHABCDEFGHIJ"
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt=text,
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "a6"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="match", query="match", max_chars=10),
        )

        assert result.status == "ready"
        assert result.mode_used == "match"
        assert result.content == "56789MATCH"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_object_ref_read_returns_bounded_content() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        object_store = LocalObjectStore(root_path=WorkspaceConfig.get_project_root() / "object-store")
        object_key = "tenants/1/engagements/1/evidence/readable.txt"
        object_store.put_bytes(object_key, b"OBJECT-READ-CONTENT", content_type="text/plain")
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="object_ref",
            inline_excerpt=None,
            object_key=object_key,
            archived_file_ref=None,
            mime_type="text/plain",
            lineage_snapshot={"artifact_id": "obj-1"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db, object_store=object_store)
        result = service.read_evidence(
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="head", max_chars=6),
        )

        assert result.status == "ready"
        assert result.source == "object_ref"
        assert result.mode_used == "head"
        assert result.content == "OBJECT"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_object_ref_binary_returns_not_available() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        object_store = LocalObjectStore(root_path=WorkspaceConfig.get_project_root() / "object-store")
        object_key = "tenants/1/engagements/1/evidence/binary.bin"
        object_store.put_bytes(object_key, b"\x00\xff\x10\x80", content_type="application/octet-stream")
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="object_ref",
            inline_excerpt=None,
            object_key=object_key,
            archived_file_ref=None,
            mime_type="application/octet-stream",
            lineage_snapshot={"artifact_id": "obj-2"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db, object_store=object_store)
        result = service.read_evidence(
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="auto", max_chars=64),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
    finally:
        db.close()
        engine.dispose()


def test_tenant_scope_mismatch_returns_not_found() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="tenant-scoped",
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "scope-1"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            tenant_id=int(engagement.tenant_id) + 1,
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="auto", max_chars=32),
        )

        assert result.status == "not_found"
        assert result.source == "none"
    finally:
        db.close()
        engine.dispose()


def test_omitted_tenant_scope_uses_engagement_tenant() -> None:
    engine, db = _build_session()
    try:
        _user, engagement = _seed_user_engagement(db)
        row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=int(engagement.tenant_id) + 1,
            user_id=_user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=uuid_lib.uuid4(),
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="foreign-tenant-row",
            archived_file_ref=None,
            lineage_snapshot={"artifact_id": "scope-2"},
        )
        db.add(row)
        db.commit()

        service = KnowledgeEvidenceReadService(db)
        result = service.read_evidence(
            engagement_id=engagement.id,
            evidence_id=str(row.id),
            request=KnowledgeEvidenceReadRequest(mode="auto", max_chars=32),
        )

        assert result.status == "not_found"
        assert result.source == "none"
    finally:
        db.close()
        engine.dispose()


def test_candidate_evidence_collector_reads_durable_evidence_with_engagement_tenant_scope() -> None:
    engine, db = _build_session()
    try:
        user, engagement = _seed_user_engagement(db)
        execution_id = uuid_lib.uuid4()
        allowed_row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="allowed",
            archived_file_ref=None,
            lineage_snapshot={"artifact_kind": "stdout"},
        )
        denied_row = KnowledgeEvidenceArchive(
            id=uuid_lib.uuid4(),
            tenant_id=int(engagement.tenant_id) + 1,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_id,
            source_artifact_id=uuid_lib.uuid4(),
            storage_mode="inline_excerpt",
            inline_excerpt="denied",
            archived_file_ref=None,
            lineage_snapshot={"artifact_kind": "stderr"},
        )
        db.add(allowed_row)
        db.add(denied_row)
        db.commit()

        class _StubEvidenceReadService:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def read_evidence(self, **kwargs):
                self.calls.append(dict(kwargs))
                return type(
                    "_Result",
                    (),
                    {
                        "status": "ready",
                        "content": "bounded",
                        "mode_used": "head",
                    },
                )()

        stub_service = _StubEvidenceReadService()
        collector = CandidateEvidenceCollector(db, evidence_read_service=stub_service)
        results = collector.read_durable_evidence(
            request=CandidateExtractionRequest(
                engagement_id=engagement.id,
                source_execution_id=str(execution_id),
                ingestion_run_id="run-tenant-threading",
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0",
                extraction_mode="candidate_fallback",
                tool_name="shell.exec",
                capability_family=None,
            )
        )

        assert len(stub_service.calls) == 1
        assert stub_service.calls[0]["tenant_id"] == int(engagement.tenant_id)
        assert len(results) == 1
        assert results[0]["evidence_archive_id"] == str(allowed_row.id)
    finally:
        db.close()
        engine.dispose()
