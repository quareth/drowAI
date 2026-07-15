"""Baseline setup API tests for runner readiness and token exposure contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.main import app
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection
from backend.models.tenant import Tenant
from backend.routers import setup as setup_router

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN_PRIMARY_SETUP_FIELDS = {
    "install_token",
    "tenant_id",
    "execution_site_id",
    "enrollment_id",
    "runner_id",
    "credential_id",
    "credential_secret",
    "enrollment_toml",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _build_runner_status_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerConnection.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_runner_status_context(db: Session, *, runner_status: str) -> tuple[int, Runner]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    db.add(tenant)
    db.flush()
    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    db.add(site)
    db.flush()
    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status=runner_status,
    )
    db.add(runner)
    db.commit()
    return int(tenant.id), runner


def test_setup_status_does_not_treat_active_runner_row_alone_as_connected() -> None:
    db = _build_runner_status_session()
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    try:
        _seed_runner_status_context(db, runner_status="active")

        assert setup_router._runner_connected(db, now=observed_at) is False
    finally:
        db.close()


def test_setup_status_does_not_treat_expired_active_connection_as_connected() -> None:
    db = _build_runner_status_session()
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    try:
        tenant_id, runner = _seed_runner_status_context(db, runner_status="registered")
        db.add(
            RunnerConnection(
                tenant_id=tenant_id,
                runner_id=runner.id,
                pod_id="pod-one",
                connection_id="conn-one",
                status="active",
                lease_expires_at=observed_at - timedelta(seconds=1),
                last_seen_at=observed_at - timedelta(minutes=5),
            )
        )
        db.commit()

        assert setup_router._runner_connected(db, now=observed_at) is False
    finally:
        db.close()


def test_setup_status_treats_fresh_active_connection_as_connected() -> None:
    db = _build_runner_status_session()
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    try:
        tenant_id, runner = _seed_runner_status_context(db, runner_status="registered")
        db.add(
            RunnerConnection(
                tenant_id=tenant_id,
                runner_id=runner.id,
                pod_id="pod-one",
                connection_id="conn-one",
                status="active",
                lease_expires_at=observed_at + timedelta(minutes=10),
                last_seen_at=observed_at - timedelta(seconds=30),
            )
        )
        db.commit()

        assert setup_router._runner_connected(db, now=observed_at) is True
    finally:
        db.close()


def test_setup_completion_response_schema_excludes_runner_enrollment_internals() -> None:
    fields = set(setup_router.SetupCompleteResponse.model_fields)

    assert not fields & _FORBIDDEN_PRIMARY_SETUP_FIELDS


def test_setup_frontend_does_not_depend_on_raw_install_token_endpoint() -> None:
    setup_sources = sorted((_REPO_ROOT / "client/src/components/setup").glob("*.ts*"))

    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in setup_sources
        if "/api/runner-control/install-tokens" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_complete_setup_primary_response_omits_raw_runner_enrollment_fields(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")

    class _InstallationService:
        def __init__(self, _db):
            pass

        def is_wizard_enabled(self) -> bool:
            return True

        def is_complete(self) -> bool:
            return False

    class _SetupCompletionService:
        def __init__(self, _db):
            pass

        def complete(self, **_kwargs):
            return SimpleNamespace(
                redirect_path="/auth",
                admin_username="admin",
                runner_site_created=True,
                runner_enrollment_published=True,
                runner_readiness="waiting_for_runner",
            )

    async def _start_background_services() -> bool:
        return True

    monkeypatch.setattr(setup_router, "PlatformInstallationService", _InstallationService)
    monkeypatch.setattr(setup_router, "SetupCompletionService", _SetupCompletionService)
    monkeypatch.setattr(setup_router, "start_background_services", _start_background_services)

    response = client.post(
        "/api/setup/complete",
        json={
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
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "install_token" not in payload
    assert "tenant_id" not in payload
    assert "execution_site_id" not in payload
    assert "enrollment_id" not in payload
    assert "runner_id" not in payload
    assert "credential_secret" not in payload
    assert "enrollment_toml" not in payload
    assert payload["runner_site_created"] is True
    assert payload["runner_enrollment_published"] is True
    assert payload["runner_readiness"] == "waiting_for_runner"
