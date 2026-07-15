"""Regression tests for standalone setup completion orchestration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.config.generated_config import GeneratedConfigPaths
from backend.models import (
    ExecutionSite,
    PlatformInstallation,
    RunnerInstallToken,
    Tenant,
    TenantMembership,
    User,
)
from backend.services.platform.generated_artifacts import (
    FilesystemGeneratedArtifactPublisher,
    RunnerConfigArtifact,
)
from backend.services.platform.setup_completion_service import (
    SetupCompletionError,
    SetupCompletionService,
)


def _build_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _build_session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'setup.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _setup_payload() -> dict[str, dict[str, object]]:
    return {
        "database": {
            "db_name": "drowai",
            "db_user": "drowai_user",
            "db_password": "secure-password",
        },
        "security": {
            "session_timeout": 30,
            "admin_username": "admin",
            "admin_email": "admin@drowai.local",
            "admin_password": "secure-password",
        },
        "display": {"timezone": "UTC"},
        "network": {"kali_docker_network": "drowai-platform"},
        "runner": {"create_site": True, "site_name": "Default Site", "site_slug": "default-site"},
    }


class _CommitCheckingPublisher:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self.published: list[RunnerConfigArtifact] = []

    def publish_runner_config(self, artifact: RunnerConfigArtifact) -> Path:
        with self._session_factory() as verifier:
            token_count = verifier.query(RunnerInstallToken).filter_by(status="issued").count()
            assert token_count == 1
            installation = verifier.get(PlatformInstallation, 1)
            assert installation is not None
            assert installation.status == "provisioning"
            assert installation.completed_at is None
        self.published.append(artifact)
        return Path("/tmp/enrollment.toml")


class _FailingPublisher:
    def publish_runner_config(self, artifact: RunnerConfigArtifact) -> Path:
        del artifact
        raise OSError("publish failed")


def test_runner_provisioning_uses_admin_membership_tenant() -> None:
    db = _build_db()
    try:
        tenant = Tenant(id=42, slug="seeded-tenant", name="Seeded Tenant")
        admin = User(username="seeded-admin", password="hashed-password")
        db.add_all([tenant, admin])
        db.flush()
        db.add(TenantMembership(tenant_id=42, user_id=int(admin.id), role="owner"))
        admin_user_id = int(admin.id)
        db.commit()

        result = SetupCompletionService(db).create_runner_provisioning(
            admin_user_id=admin_user_id,
            runner={"site_name": "Seeded Site", "site_slug": "seeded-site"},
            network={"kali_docker_network": "drowai-net"},
        )

        site = db.get(ExecutionSite, result["execution_site_id"])
        assert site is not None
        token = db.execute(
            select(RunnerInstallToken).where(RunnerInstallToken.execution_site_id == site.id)
        ).scalar_one()

        assert result["install_token"].startswith("rit_")
        assert site.tenant_id == 42
        assert token.tenant_id == 42
        assert token.created_by_user_id == admin_user_id
        assert token.token_hash != result["install_token"]
    finally:
        db.close()


def test_complete_commits_install_token_before_runner_config_publication(tmp_path: Path) -> None:
    session_factory = _build_session_factory(tmp_path)
    db = session_factory()
    publisher = _CommitCheckingPublisher(session_factory)
    payload = _setup_payload()

    try:
        result = SetupCompletionService(db, artifact_publisher=publisher).complete(**payload)

        assert result.admin_username == "admin"
        assert result.runner_site_created is True
        assert result.runner_enrollment_published is True
        assert result.runner_readiness == "waiting_for_runner"
        assert not hasattr(result, "install_token")
        assert not hasattr(result, "execution_site_id")
        assert len(publisher.published) == 1
        assert publisher.published[0].install_token.startswith("rit_")
        site = db.execute(select(ExecutionSite)).scalar_one()
        token = db.execute(select(RunnerInstallToken)).scalar_one()
        assert site.name == "Default Site"
        assert site.slug == "default-site"
        assert site.network_label == "drowai-platform"
        assert token.execution_site_id == site.id
        assert token.status == "issued"
        assert token.used_at is None
        assert token.expires_at is not None
        assert token.token_hash != publisher.published[0].install_token
        installation = db.get(PlatformInstallation, 1)
        assert installation is not None
        assert installation.status == "complete"
        assert installation.completed_at is not None
    finally:
        db.close()


def test_publish_failure_leaves_setup_failed_and_retryable(tmp_path: Path) -> None:
    session_factory = _build_session_factory(tmp_path)
    payload = _setup_payload()
    db = session_factory()

    try:
        with pytest.raises(SetupCompletionError):
            SetupCompletionService(db, artifact_publisher=_FailingPublisher()).complete(**payload)

        installation = db.get(PlatformInstallation, 1)
        assert installation is not None
        assert installation.status == "failed"
        assert installation.completed_at is None

        publisher = _CommitCheckingPublisher(session_factory)
        result = SetupCompletionService(db, artifact_publisher=publisher).complete(**payload)

        assert result.admin_username == "admin"
        assert db.query(User).filter_by(username="admin").count() == 1
        assert db.query(RunnerInstallToken).filter_by(status="revoked").count() == 1
        assert db.query(RunnerInstallToken).filter_by(status="issued").count() == 1
        assert db.get(PlatformInstallation, 1).status == "complete"
    finally:
        db.close()


def test_filesystem_runner_config_publisher_writes_atomically_with_restrictive_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = GeneratedConfigPaths(config_dir=tmp_path / "config", secrets_dir=tmp_path / "secrets")
    publisher = FilesystemGeneratedArtifactPublisher(paths=paths)
    monkeypatch.setenv("DROWAI_RUNNER_ROOT", str(tmp_path / "runner-root"))
    monkeypatch.setenv("DROWAI_RUNNER_HOST_BIND_ROOT", str(tmp_path / "host-runner-root"))

    config_path = publisher.publish_runner_config(
        RunnerConfigArtifact(
            install_token="rit_test_token",
            network={"kali_docker_network": "drowai-net"},
        )
    )

    content = config_path.read_text(encoding="utf-8")
    assert "registration_token = \"rit_test_token\"" in content
    assert f"runner_root = \"{tmp_path / 'runner-root'}\"" in content
    assert f"host_bind_root = \"{tmp_path / 'host-runner-root'}\"" in content
    assert "tenant_id" not in content
    assert "network = \"drowai-net\"" in content
    assert oct(os.stat(config_path).st_mode & 0o777) == "0o600"
