"""Tests for the offline deterministic-E2E seed command boundary."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Engagement, LLMUsageRecord, Task, Tenant, TenantMembership, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeService,
)
from scripts.e2e_seed import (
    _seed_tenant_membership,
    _seed_reporting_input,
    _seed_usage_settings,
    _usage_settings_credential_secret,
    _seed_workspace_knowledge,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = REPO_ROOT / "scripts" / "e2e_seed.py"


def test_seed_cli_rejects_non_e2e_invocation_before_database_import() -> None:
    env = dict(os.environ)
    env.pop("E2E_DETERMINISTIC_MODE", None)
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "membership",
            "--actor-user-id",
            "1",
            "--target-user-id",
            "2",
            "--tenant-id",
            "1",
            "--role",
            "viewer",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "E2E_DETERMINISTIC_MODE" in result.stderr
    assert "DATABASE_URL" not in result.stderr


def test_seed_script_is_not_imported_by_http_routers() -> None:
    wired_http_files = [REPO_ROOT / "backend" / "main.py"]
    wired_http_files.extend((REPO_ROOT / "backend" / "routers").rglob("*.py"))

    assert all("e2e_seed" not in path.read_text(encoding="utf-8") for path in wired_http_files)


def test_tenant_membership_seed_creates_isolated_non_default_tenant() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        user = User(username="tenant-seed-owner", password="unused")
        db.add(user)
        db.flush()

        result = _seed_tenant_membership(
            db,
            user_id=user.id,
            tenant_slug="e2e-isolated-tenant",
            tenant_name="E2E Isolated Tenant",
            role="owner",
        )
        db.commit()

        tenant = db.query(Tenant).filter_by(id=result["tenant_id"]).one()
        membership = db.query(TenantMembership).filter_by(
            id=result["membership_id"]
        ).one()
        assert tenant.slug == "e2e-isolated-tenant"
        assert tenant.name == "E2E Isolated Tenant"
        assert tenant.status == "active"
        assert membership.user_id == user.id
        assert membership.tenant_id == tenant.id
        assert membership.role == "owner"
        assert membership.status == "active"
        assert result["is_default_tenant"] is False

        repeated = _seed_tenant_membership(
            db,
            user_id=user.id,
            tenant_slug="e2e-isolated-tenant",
            tenant_name="E2E Isolated Tenant",
            role="owner",
        )
        assert repeated == result
    finally:
        db.close()
        engine.dispose()


def test_workspace_knowledge_seed_is_task_scoped_and_uses_projector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_DETERMINISTIC_MODE", "true")
    monkeypatch.setenv("E2E_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        user = User(username="workspace-seed-owner", password="unused")
        tenant = Tenant(name="Workspace seed", slug="workspace-seed")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        engagement = Engagement(
            tenant_id=tenant.id,
            user_id=user.id,
            name="Workspace seed engagement",
            status="active",
        )
        db.add(engagement)
        db.flush()
        task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Workspace seed task",
        )
        db.add(task)
        db.commit()

        result = _seed_workspace_knowledge(
            db,
            user_id=user.id,
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            task_id=task.id,
            relative_path="artifacts/owner-observation.txt",
            content="task-local owner content",
            finding_title="Task-local exposed service",
        )
        db.commit()

        seeded_file = tmp_path / "workspaces" / f"task-{task.id}" / result["relative_path"]
        assert seeded_file.read_text(encoding="utf-8") == "task-local owner content"
        assert result["finding_upsert_count"] == 1
        assert db.query(KnowledgeFinding).filter_by(engagement_id=engagement.id).one().title == (
            "Task-local exposed service"
        )
        service = db.query(KnowledgeService).filter_by(engagement_id=engagement.id).one()
        evidence = db.query(KnowledgeEvidenceArchive).filter_by(engagement_id=engagement.id).one()
        provenance = db.query(KnowledgeEntityProvenance).filter_by(
            engagement_id=engagement.id,
            entity_type="finding",
        ).one()
        assert service.port == 8443
        assert evidence.inline_excerpt == "task-local owner content"
        assert db.query(KnowledgeRelationship).filter_by(engagement_id=engagement.id).count() == 1
        assert str(provenance.evidence_archive_id) == str(evidence.id)
        assert result["evidence_id"] == str(evidence.id)
        assert result["service_id"] == str(service.id)
        assert result["asset_id"]
        assert result["finding_id"]

        with pytest.raises(ValueError, match="traversal"):
            _seed_workspace_knowledge(
                db,
                user_id=user.id,
                tenant_id=tenant.id,
                engagement_id=engagement.id,
                task_id=task.id,
                relative_path="../escape.txt",
                content="escape",
                finding_title="Should not project",
            )
    finally:
        db.close()
        engine.dispose()


def test_reporting_input_seed_prepares_current_memo_from_task_local_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_DETERMINISTIC_MODE", "true")
    monkeypatch.setenv("E2E_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        user = User(username="reporting-seed-owner", password="unused")
        tenant = Tenant(name="Reporting seed", slug="reporting-seed")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        engagement = Engagement(
            tenant_id=tenant.id,
            user_id=user.id,
            name="Reporting seed engagement",
            status="active",
        )
        db.add(engagement)
        db.flush()
        task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Reporting seed task",
        )
        db.add(task)
        db.commit()
        _seed_workspace_knowledge(
            db,
            user_id=user.id,
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            task_id=task.id,
            relative_path="artifacts/report-source.txt",
            content="deterministic report evidence",
            finding_title="Deterministic report finding",
        )

        result = _seed_reporting_input(
            db,
            user_id=user.id,
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        db.commit()

        from backend.models.reporting import TaskClosureMemo

        db.refresh(task)
        memo = db.query(TaskClosureMemo).filter_by(id=result["memo_id"]).one()
        assert task.status == "stopped"
        assert memo.status == "ready"
        assert memo.is_current is True
        assert memo.source_watermark["empty"] is False
        assert len(result["source_watermark_hash"]) == 64
        assert memo.memo["reportable_observations"]
        assert memo.memo["possible_findings"]
    finally:
        db.close()
        engine.dispose()


def test_usage_settings_seed_uses_scoped_services_and_never_returns_secret() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        user = User(username="usage-seed-owner", password="unused")
        tenant = Tenant(name="Usage seed", slug="usage-seed")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
        engagement = Engagement(
            tenant_id=tenant.id,
            user_id=user.id,
            name="Usage seed engagement",
            status="active",
        )
        db.add(engagement)
        db.flush()
        task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Usage seed task",
        )
        other_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Usage filter control",
        )
        db.add_all([task, other_task])
        db.commit()

        secret = _usage_settings_credential_secret(user_id=user.id, task_id=task.id)
        result = _seed_usage_settings(
            db,
            user_id=user.id,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id="e2e-usage-primary",
        )

        rows = db.query(LLMUsageRecord).filter_by(task_id=task.id).all()
        assert len(rows) == 2
        assert sum(row.prompt_tokens for row in rows) == 160
        assert sum(row.completion_tokens for row in rows) == 60
        assert {row.conversation_id for row in rows} == {
            "e2e-usage-primary",
            "e2e-usage-filter-control",
        }
        assert db.query(LLMUsageRecord).filter_by(task_id=other_task.id).count() == 0
        assert result["call_count"] == 2
        assert result["prompt_tokens"] == 160
        assert result["completion_tokens"] == 60
        assert result["credential_masked"] is True
        assert secret not in str(result)
    finally:
        db.close()
        engine.dispose()
