"""Router tests for runner control management endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from typing import Iterator
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, WebSocket, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

import backend.auth as auth_module
import backend.database as database_module
from backend.database import Base
from backend.models.core import Task, User
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import runner_control as runner_routes
from backend.services.tenant import dependencies as tenant_dependencies
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.credentials import RunnerCredentialAuthError, RunnerCredentialService
from runtime_shared.runner_protocol import RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE


def _build_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            TenantMembership.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
            RunnerCredential.__table__,
            RunnerInstallToken.__table__,
            RuntimeJob.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_context(db: Session, *, role: str = "owner") -> tuple[SimpleNamespace, Tenant, ExecutionSite, Runner]:
    user = User(username="owner", password="hashed")
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    db.add_all([user, tenant])
    db.flush()

    db.add(
        TenantMembership(
            tenant_id=tenant.id,
            user_id=user.id,
            role=role,
        )
    )
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
        status="registered",
        version="1.0.0",
    )
    db.add(runner)
    db.commit()
    return SimpleNamespace(id=user.id, username=user.username), tenant, site, runner


def _seed_foreign_runner(db: Session) -> tuple[Tenant, ExecutionSite, Runner]:
    tenant = Tenant(slug="tenant-foreign", name="Tenant Foreign")
    db.add(tenant)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Foreign Site",
        slug="foreign-site",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-foreign",
        status="registered",
        version="1.0.0",
    )
    db.add(runner)
    db.commit()
    return tenant, site, runner


def _seed_ready_replacement(
    db: Session,
    *,
    tenant: Tenant,
) -> tuple[ExecutionSite, Runner]:
    now = datetime.now(tz=timezone.utc)
    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Replacement Site",
        slug="replacement-site",
        status="active",
    )
    db.add(site)
    db.flush()
    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-replacement",
        status="active",
        last_seen_at=now,
        capacity_json={"active_tasks": 10, "available_tasks": 0},
    )
    db.add(runner)
    db.flush()
    RunnerCredentialService(db).issue_runner_credential(
        tenant_id=tenant.id,
        runner_id=runner.id,
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-replacement",
            connection_id="conn-replacement",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    db.commit()
    return site, runner


def _runner_envelope(
    *,
    tenant_id: int,
    runner_id: UUID,
    message_type: str,
    payload: dict[str, object],
    message_id: str = "msg-1",
    correlation_id: str | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": "runner_control.v1",
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": correlation_id,
            "runtime_job_id": None,
            "task_id": None,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload,
        }
    )


@contextmanager
def _make_client(db: Session, current_user: object, *, base_url: str = "http://testserver") -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(runner_routes.router)

    def _fake_get_db():
        yield db

    def _fake_get_current_user():
        return current_user

    app.dependency_overrides[runner_routes.get_db] = _fake_get_db
    app.dependency_overrides[database_module.get_db] = _fake_get_db
    app.dependency_overrides[tenant_dependencies.get_db] = _fake_get_db
    app.dependency_overrides[auth_module.get_current_user] = _fake_get_current_user
    try:
        yield TestClient(app, base_url=base_url)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _make_registration_client(db: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(runner_routes.router)

    def _fake_get_db():
        yield db

    app.dependency_overrides[runner_routes.get_db] = _fake_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _make_channel_auth_client(db: Session) -> Iterator[TestClient]:
    app = FastAPI()

    def _fake_get_db():
        yield db

    app.dependency_overrides[runner_routes.get_db] = _fake_get_db

    @app.get("/probe/runner-channel-auth")
    def probe_runner_channel_auth(
        identity: runner_routes.RunnerChannelAuthContext = Depends(runner_routes.authenticate_runner_channel),
    ) -> dict[str, object]:
        return {
            "tenant_id": identity.tenant_id,
            "runner_id": str(identity.runner_id),
            "credential_id": str(identity.credential_id),
            "allowed_protocol_versions": list(identity.allowed_protocol_versions),
        }

    @app.websocket("/probe/ws-runner-channel-auth")
    async def ws_runner_channel_auth(
        websocket: WebSocket,
        identity: runner_routes.RunnerChannelAuthContext = Depends(runner_routes.authenticate_runner_channel),
    ) -> None:
        await websocket.accept()
        await websocket.send_json(
            {
                "tenant_id": identity.tenant_id,
                "runner_id": str(identity.runner_id),
                "credential_id": str(identity.credential_id),
            }
        )
        await websocket.close()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_create_install_token_returns_plaintext_only_once_and_hashes_in_db() -> None:
    db = _build_session()
    user, _tenant, site, _runner = _seed_context(db)
    observed_before = datetime.now(timezone.utc)

    with _make_client(db, user) as client:
        response = client.post(
            "/api/runner-control/install-tokens",
            json={
                "execution_site_id": str(site.id),
                "ttl_seconds": 900,
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["install_token"].startswith("rit_")
    assert payload["execution_site_id"] == str(site.id)
    expires_at = datetime.fromisoformat(payload["expires_at"])
    assert observed_before + timedelta(seconds=895) <= expires_at
    assert expires_at <= datetime.now(timezone.utc) + timedelta(seconds=905)

    token_row = db.execute(
        select(RunnerInstallToken).where(RunnerInstallToken.id == UUID(payload["install_token_id"]))
    ).scalar_one()
    assert token_row.token_hash != payload["install_token"]


def test_create_runner_enrollment_returns_runner_site_material_without_tenant_prompt() -> None:
    db = _build_session()
    user, tenant, _site, _runner = _seed_context(db)

    with _make_client(db, user) as client:
        response = client.post(
            "/api/runner-control/enrollments",
            json={
                "site_name": "Field Office",
                "management_url": "http://management.example.test:8000",
                "tls_verify": False,
                "allow_insecure_management_url": True,
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["runner_site"]["name"] == "Field Office"
    assert payload["runner_site"]["slug"] == "field-office"
    assert "tenant_id" not in payload["runner_site"]
    assert "tenant_id" not in json.dumps(payload, sort_keys=True)
    assert payload["package_name"] == "drowai-runner-site-field-office.tar.gz"
    assert "registration_token = \"rit_" in payload["enrollment_toml"]
    assert "tenant_id" not in payload["enrollment_toml"]
    assert payload["install_commands"] == [
        "tar xzf drowai-runner-site-field-office.tar.gz",
        "cd drowai-runner-site",
        "docker compose up -d --build",
    ]

    token = db.execute(
        select(RunnerInstallToken).where(RunnerInstallToken.tenant_id == tenant.id)
    ).scalar_one()
    assert token.execution_site_id == UUID(payload["runner_site"]["id"])


def test_management_url_endpoints_resolve_and_persist_canonical_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)

    with _make_client(db, user, base_url="http://192.168.50.130") as client:
        resolved_response = client.get("/api/runner-control/management-url")
        update_response = client.put(
            "/api/runner-control/management-url",
            json={"management_url": "http://192.168.50.130/"},
        )
        stored_response = client.get("/api/runner-control/management-url")

    assert resolved_response.status_code == 200
    assert resolved_response.json() == {
        "management_url": "http://192.168.50.130",
        "source": "request_origin",
    }
    assert update_response.status_code == 200
    assert update_response.json() == {
        "management_url": "http://192.168.50.130",
        "source": "generated_config",
    }
    assert stored_response.json() == {
        "management_url": "http://192.168.50.130",
        "source": "generated_config",
    }


def test_management_url_rejects_path_bearing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)

    with _make_client(db, user) as client:
        response = client.put(
            "/api/runner-control/management-url",
            json={"management_url": "http://192.168.50.130/api"},
        )

    assert response.status_code == 400
    assert "origin only" in response.json()["detail"]


def test_runner_sites_report_management_observed_connectivity() -> None:
    db = _build_session()
    user, tenant, site, runner = _seed_context(db)
    now = datetime.now(timezone.utc)
    db.add(
        RunnerConnection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="expired-conn",
            status="active",
            lease_expires_at=now - timedelta(seconds=1),
            last_seen_at=now - timedelta(seconds=10),
        )
    )
    waiting_site = ExecutionSite(
        tenant_id=tenant.id,
        name="Waiting Site",
        slug="waiting-site",
        status="active",
    )
    db.add(waiting_site)
    db.commit()

    with _make_client(db, user) as client:
        response = client.get("/api/runner-control/runner-sites")

    assert response.status_code == 200
    by_slug = {record["slug"]: record for record in response.json()}
    assert by_slug["waiting-site"]["connectivity_status"] == "waiting"
    assert by_slug["waiting-site"]["runner_count"] == 0
    assert by_slug["primary-site"]["connectivity_status"] == "offline"
    assert by_slug["primary-site"]["runner_count"] == 1
    assert by_slug["primary-site"]["connected_runner_count"] == 0
    assert by_slug["primary-site"]["last_seen_at"] is not None

    connection = db.execute(
        select(RunnerConnection).where(RunnerConnection.connection_id == "expired-conn")
    ).scalar_one()
    connection.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()

    with _make_client(db, user) as client:
        connected_response = client.get("/api/runner-control/runner-sites")

    assert connected_response.status_code == 200
    connected_by_slug = {record["slug"]: record for record in connected_response.json()}
    assert connected_by_slug["primary-site"]["connectivity_status"] == "connected"
    assert connected_by_slug["primary-site"]["connected_runner_count"] == 1


def test_create_runner_enrollment_package_streams_preconfigured_tarball(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    observed: dict[str, object] = {}

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        output_path = Path(str(args[-1]))
        enrollment_path = Path(str(args[-3]))
        assert "tenant_id" not in enrollment_path.read_text(encoding="utf-8")
        output_path.write_bytes(b"runner-site-package")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner_routes.subprocess, "run", _fake_run)

    with _make_client(db, user) as client:
        response = client.post(
            "/api/runner-control/enrollments/package",
            json={
                "site_name": "Package Site",
                "management_url": "http://management.example.test:8000",
                "tls_verify": False,
                "allow_insecure_management_url": True,
            },
        )

    assert response.status_code == 201
    assert response.content == b"runner-site-package"
    assert "drowai-runner-site-package-site.tar.gz" in response.headers["content-disposition"]
    assert "--enrollment-toml" in observed["args"]


def test_create_runner_enrollment_package_uses_request_origin_when_url_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    observed: dict[str, str] = {}

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output_path = Path(str(args[-1]))
        enrollment_path = Path(str(args[args.index("--enrollment-toml") + 1]))
        observed["enrollment"] = enrollment_path.read_text(encoding="utf-8")
        output_path.write_bytes(b"runner-site-package")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner_routes.subprocess, "run", _fake_run)

    with _make_client(db, user, base_url="http://192.168.50.130") as client:
        response = client.post(
            "/api/runner-control/enrollments/package",
            json={
                "site_name": "Package Site",
                "tls_verify": False,
            },
        )

    assert response.status_code == 201
    assert 'control_plane_url = "http://192.168.50.130"' in observed["enrollment"]
    assert "192.168.50.130:5000" not in observed["enrollment"]


def test_create_runner_enrollment_package_masks_token_bearing_subprocess_logs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    observed: dict[str, str] = {}

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        enrollment_path = Path(str(args[args.index("--enrollment-toml") + 1]))
        enrollment_toml = enrollment_path.read_text(encoding="utf-8")
        raw_token = enrollment_toml.split('registration_token = "', 1)[1].split('"', 1)[0]
        observed["raw_token"] = raw_token
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout=f"failed to package registration_token={raw_token}",
            stderr=f"enrollment token {raw_token} rejected",
        )

    monkeypatch.setattr(runner_routes.subprocess, "run", _fake_run)
    caplog.set_level("ERROR", logger="backend.routers.runner_control")

    with _make_client(db, user) as client:
        response = client.post(
            "/api/runner-control/enrollments/package",
            json={
                "site_name": "Package Log Site",
                "management_url": "http://management.example.test:8000",
                "tls_verify": False,
                "allow_insecure_management_url": True,
            },
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    raw_token = observed["raw_token"]
    messages = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.routers.runner_control"
    )
    assert raw_token not in messages
    assert "<MASKED_INSTALL_TOKEN>" in messages


def test_remove_runner_site_hard_deletes_registry_material_and_returns_no_content() -> None:
    db = _build_session()
    user, tenant, site, runner = _seed_context(db)
    _seed_ready_replacement(db, tenant=tenant)
    credential_service = RunnerCredentialService(db)
    issued_credential = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        created_by_user_id=user.id,
    )
    db.commit()

    with _make_client(db, user) as client:
        response = client.delete(f"/api/runner-control/runner-sites/{site.id}")

    assert response.status_code == 204
    assert response.content == b""

    refreshed_site = db.get(ExecutionSite, site.id)
    refreshed_runner = db.get(Runner, runner.id)
    refreshed_token = db.get(RunnerInstallToken, issued_token.install_token_id)
    refreshed_credential = db.get(RunnerCredential, issued_credential.credential_id)

    assert refreshed_site is None
    assert refreshed_runner is None
    assert refreshed_token is None
    assert refreshed_credential is None


def test_remove_runner_site_returns_structured_active_execution_conflict() -> None:
    db = _build_session()
    user, tenant, site, runner = _seed_context(db)
    _seed_ready_replacement(db, tenant=tenant)
    now = datetime.now(tz=timezone.utc)
    runner.status = "active"
    runner.last_seen_at = now
    runner.capacity_json = {"active_tasks": 2, "active_runtime_jobs": []}
    db.add(
        RunnerConnection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    db.commit()

    with _make_client(db, user) as client:
        response = client.delete(f"/api/runner-control/runner-sites/{site.id}")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "RUNNER_SITE_ACTIVE_EXECUTIONS",
        "message": "Runner Site has active executions.",
        "execution_count": 2,
    }
    assert db.get(ExecutionSite, site.id) is not None


def test_remove_runner_site_returns_structured_last_connected_conflict() -> None:
    db = _build_session()
    user, tenant, site, _runner = _seed_context(db)

    with _make_client(db, user) as client:
        response = client.delete(f"/api/runner-control/runner-sites/{site.id}")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error_code": "RUNNER_SITE_LAST_CONNECTED",
        "message": "Connect another Runner Site before removing this one.",
    }
    assert db.get(ExecutionSite, site.id) is not None


def test_remove_runner_site_returns_structured_not_found_for_cross_tenant_site() -> None:
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    _foreign_tenant, foreign_site, _foreign_runner = _seed_foreign_runner(db)

    with _make_client(db, user) as client:
        response = client.delete(f"/api/runner-control/runner-sites/{foreign_site.id}")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error_code": "RUNNER_SITE_NOT_FOUND",
        "message": "Runner Site not found.",
    }


def test_runner_management_mutation_uses_centralized_runner_manage_policy() -> None:
    db = _build_session()
    viewer, _tenant, site, _runner = _seed_context(db, role="viewer")

    with _make_client(db, viewer) as client:
        response = client.post(
            "/api/runner-control/install-tokens",
            json={
                "execution_site_id": str(site.id),
                "ttl_seconds": 900,
            },
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in response.json()["detail"]


def test_runner_enrollment_token_bearing_endpoints_require_runner_manage_policy() -> None:
    db = _build_session()
    viewer, _tenant, _site, _runner = _seed_context(db, role="viewer")
    payload = {
        "site_name": "Viewer Site",
        "management_url": "http://management.example.test:8000",
        "tls_verify": False,
        "allow_insecure_management_url": True,
    }

    with _make_client(db, viewer) as client:
        enrollment_response = client.post("/api/runner-control/enrollments", json=payload)
        package_response = client.post("/api/runner-control/enrollments/package", json=payload)

    assert enrollment_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in enrollment_response.json()["detail"]
    assert package_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in package_response.json()["detail"]


def test_runner_management_read_endpoints_require_runner_manage_action() -> None:
    db = _build_session()
    viewer, _tenant, _site, runner = _seed_context(db, role="viewer")

    with _make_client(db, viewer) as client:
        execution_sites_response = client.get("/api/runner-control/execution-sites")
        runners_response = client.get("/api/runner-control/runners")
        runner_response = client.get(f"/api/runner-control/runners/{runner.id}")

    assert execution_sites_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in execution_sites_response.json()["detail"]
    assert runners_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in runners_response.json()["detail"]
    assert runner_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in runner_response.json()["detail"]


def test_install_token_and_runner_ids_fail_closed_across_tenants() -> None:
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    _foreign_tenant, foreign_site, foreign_runner = _seed_foreign_runner(db)

    with _make_client(db, user) as client:
        install_token_response = client.post(
            "/api/runner-control/install-tokens",
            json={
                "execution_site_id": str(foreign_site.id),
                "ttl_seconds": 900,
            },
        )
        runner_detail_response = client.get(f"/api/runner-control/runners/{foreign_runner.id}")
        runner_revoke_response = client.post(f"/api/runner-control/runners/{foreign_runner.id}/revoke")

    assert install_token_response.status_code == status.HTTP_404_NOT_FOUND
    assert install_token_response.json() == {"detail": "Execution site not found."}
    assert runner_detail_response.status_code == status.HTTP_404_NOT_FOUND
    assert runner_detail_response.json() == {"detail": "Runner not found."}
    assert runner_revoke_response.status_code == status.HTTP_404_NOT_FOUND
    assert runner_revoke_response.json() == {"detail": "Runner not found."}


def test_runtime_job_id_lookup_fails_closed_across_tenants() -> None:
    db = _build_session()
    user, _tenant, _site, _runner = _seed_context(db)
    foreign_tenant, _foreign_site, _foreign_runner = _seed_foreign_runner(db)

    foreign_runtime_job = RuntimeJob(
        tenant_id=foreign_tenant.id,
        job_type="runner_control.runtime.assignment_probe",
        status="queued",
        idempotency_key="foreign-runtime-job",
    )
    db.add(foreign_runtime_job)
    db.commit()

    with _make_client(db, user) as client:
        response = client.get(f"/api/runner-control/runtime-jobs/{foreign_runtime_job.id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "Runtime job not found."}


def test_runner_management_requires_explicit_active_tenant_for_multi_membership_user() -> None:
    db = _build_session()
    user, tenant_one, site_one, _runner = _seed_context(db)

    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    db.add(tenant_two)
    db.flush()
    db.add(
        TenantMembership(
            tenant_id=tenant_two.id,
            user_id=user.id,
            role="viewer",
        )
    )
    db.add(
        ExecutionSite(
            tenant_id=tenant_two.id,
            name="Tenant Two Site",
            slug="tenant-two-site",
            status="active",
        )
    )
    db.commit()

    with _make_client(db, user) as client:
        no_header = client.get("/api/runner-control/execution-sites")
        tenant_one_response = client.get(
            "/api/runner-control/execution-sites",
            headers={tenant_dependencies.ACTIVE_TENANT_HEADER: str(tenant_one.id)},
        )
        tenant_two_response = client.get(
            "/api/runner-control/execution-sites",
            headers={tenant_dependencies.ACTIVE_TENANT_HEADER: str(tenant_two.id)},
        )

    assert no_header.status_code == status.HTTP_409_CONFLICT
    assert "Explicit tenant selection is required" in no_header.json()["detail"]
    assert tenant_one_response.status_code == status.HTTP_200_OK
    payload = tenant_one_response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == str(site_one.id)
    assert tenant_two_response.status_code == status.HTTP_403_FORBIDDEN
    assert "runner.manage" in tenant_two_response.json()["detail"]


def test_runtime_job_lookup_allows_same_tenant_non_creator_task() -> None:
    db = _build_session()
    user, tenant, _site, _runner = _seed_context(db)

    task_owner = User(username="tenant-task-owner", password="hashed")
    db.add(task_owner)
    db.flush()
    task = Task(
        user_id=task_owner.id,
        tenant_id=tenant.id,
        name="shared-task",
    )
    db.add(task)
    db.flush()
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        task_id=task.id,
        job_type="runner_control.runtime.assignment_probe",
        status="queued",
        idempotency_key="runtime-job-shared-task",
    )
    db.add(runtime_job)
    db.commit()

    with _make_client(db, user) as client:
        response = client.get(
            f"/api/runner-control/runtime-jobs/{runtime_job.id}",
            headers={tenant_dependencies.ACTIVE_TENANT_HEADER: str(tenant.id)},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(runtime_job.id)
    assert response.json()["task_id"] == task.id


def test_runtime_job_list_allows_same_tenant_non_creator_task() -> None:
    db = _build_session()
    user, tenant, _site, _runner = _seed_context(db)

    task_owner = User(username="tenant-task-owner-list", password="hashed")
    db.add(task_owner)
    db.flush()
    task = Task(
        user_id=task_owner.id,
        tenant_id=tenant.id,
        name="shared-task-list",
    )
    db.add(task)
    db.flush()
    first_job = RuntimeJob(
        tenant_id=tenant.id,
        task_id=task.id,
        job_type="runner_control.runtime.assignment_probe",
        status="queued",
        idempotency_key="runtime-job-shared-task-list-1",
    )
    second_job = RuntimeJob(
        tenant_id=tenant.id,
        task_id=task.id,
        job_type="runner_control.runtime.assignment_probe",
        status="queued",
        idempotency_key="runtime-job-shared-task-list-2",
    )
    db.add_all([first_job, second_job])
    db.commit()

    with _make_client(db, user) as client:
        response = client.get(
            "/api/runner-control/runtime-jobs",
            params={"task_id": task.id},
            headers={tenant_dependencies.ACTIVE_TENANT_HEADER: str(tenant.id)},
        )

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    returned_ids = {row["id"] for row in payload}
    assert returned_ids == {str(first_job.id), str(second_job.id)}


def test_runner_detail_response_excludes_secret_hash_fields() -> None:
    db = _build_session()
    user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db, now_provider=lambda: datetime(2026, 5, 22, tzinfo=timezone.utc))
    credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    with _make_client(db, user) as client:
        response = client.get(f"/api/runner-control/runners/{runner.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(runner.id)
    assert len(payload["credentials"]) == 1
    credential_payload = payload["credentials"][0]
    assert "secret_hash" not in credential_payload


def test_revoke_endpoint_blocks_future_credential_authentication() -> None:
    db = _build_session()
    user, tenant, _site, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    credential_service = RunnerCredentialService(db, now_provider=lambda: now)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    with _make_client(db, user) as client:
        response = client.post(f"/api/runner-control/runners/{runner.id}/revoke")

    assert response.status_code == 200
    assert response.json()["revoked_credential_count"] == 1

    with pytest.raises(RunnerCredentialAuthError) as revoked_error:
        credential_service.authenticate_runner_credential(
            tenant_id=tenant.id,
            runner_id=runner.id,
            plaintext_secret=issued.plaintext_secret,
        )
    assert revoked_error.value.error_code == "RUNNER_AUTH_REVOKED"


def test_runner_list_is_tenant_filtered() -> None:
    db = _build_session()
    user, tenant, site, runner = _seed_context(db)

    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    db.add(tenant_two)
    db.flush()
    db.add(
        ExecutionSite(
            tenant_id=tenant_two.id,
            name="Other Site",
            slug="other-site",
            status="active",
        )
    )
    db.flush()
    other_site = db.execute(
        select(ExecutionSite).where(ExecutionSite.tenant_id == tenant_two.id)
    ).scalar_one()
    db.add(
        Runner(
            tenant_id=tenant_two.id,
            execution_site_id=other_site.id,
            name="runner-other",
            status="registered",
        )
    )
    db.commit()

    with _make_client(db, user) as client:
        response = client.get("/api/runner-control/runners")

    assert response.status_code == 200
    payload = response.json()
    assert [row["id"] for row in payload] == [str(runner.id)]
    assert payload[0]["execution_site_id"] == str(site.id)
    assert payload[0]["name"] == "runner-alpha"


def test_register_endpoint_uses_install_token_not_user_jwt_and_returns_registration_payload() -> None:
    db = _build_session()
    _user, tenant, site, _runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_install_token(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        created_by_user_id=1,
    )
    db.commit()

    with _make_registration_client(db) as client:
        response = client.post(
            "/api/runner-control/register",
            headers={"Authorization": "Bearer definitely-not-a-user-jwt"},
            json={
                "install_token": issued.plaintext_token,
                "runner_name": "runner-register-api",
                "runner_version": "1.2.3",
                "labels": {"region": "us-west"},
                "capabilities": ["docker"],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["runner_id"]
    assert payload["tenant_id"] == tenant.id
    assert payload["credential_id"]
    assert payload["credential_fingerprint"]
    assert payload["credential_secret"].startswith("rsec_")
    assert payload["channel_endpoint"] == "http://testserver/api/runner-control/channel"
    assert payload["protocol_version"] == "data_plane.v1"
    assert payload["heartbeat_interval_seconds"] == 30


def test_register_endpoint_rejects_invalid_or_replayed_token_with_generic_failure() -> None:
    db = _build_session()
    _user, tenant, site, _runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_install_token(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        created_by_user_id=1,
    )
    db.commit()

    with _make_registration_client(db) as client:
        invalid_response = client.post(
            "/api/runner-control/register",
            json={
                "tenant_id": tenant.id,
                "install_token": "rit_invalid_token",
                "runner_name": "runner-invalid-token",
            },
        )
        first_use = client.post(
            "/api/runner-control/register",
            json={
                "tenant_id": tenant.id,
                "install_token": issued.plaintext_token,
                "runner_name": "runner-replay",
            },
        )
        replay = client.post(
            "/api/runner-control/register",
            json={
                "tenant_id": tenant.id,
                "install_token": issued.plaintext_token,
                "runner_name": "runner-replay",
            },
        )

    assert invalid_response.status_code == 401
    assert invalid_response.json() == {"detail": "Runner registration failed."}
    assert first_use.status_code == 201
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Runner registration failed."}


def test_runner_channel_auth_dependency_returns_identity_and_updates_last_used_at() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db, now_provider=lambda: datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc))
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    with _make_channel_auth_client(db) as client:
        response = client.get(
            "/probe/runner-channel-auth",
            headers={
                "x-runner-tenant-id": str(tenant.id),
                "x-runner-id": str(runner.id),
                "x-runner-credential-secret": issued.plaintext_secret,
                "Authorization": "Bearer not-a-user-jwt",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == tenant.id
    assert payload["runner_id"] == str(runner.id)
    assert payload["credential_id"] == str(issued.credential_id)
    assert payload["allowed_protocol_versions"] == list(RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE)

    fresh_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        stored_credential = fresh_session.execute(
            select(RunnerCredential).where(RunnerCredential.id == issued.credential_id)
        ).scalar_one()
        assert stored_credential.last_used_at is not None
        assert stored_credential.secret_hash != issued.plaintext_secret
    finally:
        fresh_session.close()


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        (
            {
                "x-runner-tenant-id": "invalid",
                "x-runner-id": "not-a-uuid",
                "x-runner-credential-secret": "rsec_wrong",
            },
            401,
        ),
    ],
)
def test_runner_channel_auth_dependency_rejects_missing_or_malformed_headers(
    headers: dict[str, str],
    expected_status: int,
) -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    with _make_channel_auth_client(db) as client:
        response = client.get("/probe/runner-channel-auth", headers=headers)

    assert response.status_code == expected_status
    assert response.json() == {"detail": "Runner channel authentication failed."}


def test_runner_channel_auth_dependency_rejects_revoked_credentials_for_http_and_ws() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    now = datetime(2026, 5, 23, 10, 30, tzinfo=timezone.utc)
    credential_service = RunnerCredentialService(db, now_provider=lambda: now)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    credential_row = db.execute(
        select(RunnerCredential).where(RunnerCredential.id == issued.credential_id)
    ).scalar_one()
    credential_service.revoke_runner_credential(credential_row)
    db.commit()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_channel_auth_client(db) as client:
        http_response = client.get("/probe/runner-channel-auth", headers=headers)
        assert http_response.status_code == 401
        assert http_response.json() == {"detail": "Runner channel authentication failed."}

        with pytest.raises(WebSocketDisconnect) as ws_error:
            with client.websocket_connect(
                "/probe/ws-runner-channel-auth",
                headers=headers,
            ):
                pass

    assert ws_error.value.code == status.WS_1008_POLICY_VIOLATION


def test_runner_channel_auth_failure_logs_are_redacted_and_rate_limited(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    monkeypatch.setattr(
        runner_routes,
        "_RUNNER_AUTH_FAILURE_LOG_RATE_LIMITER",
        runner_routes._AuthFailureLogRateLimiter(max_events=1, window_seconds=3600.0, max_keys=16),
    )
    caplog.set_level("WARNING", logger="backend.routers.runner_control")

    raw_secret = "rsec_super_secret_value_1234567890"
    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": raw_secret,
    }

    with _make_channel_auth_client(db) as client:
        first = client.get("/probe/runner-channel-auth", headers=headers)
        second = client.get("/probe/runner-channel-auth", headers=headers)
        third = client.get("/probe/runner-channel-auth", headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 401

    warning_records = [
        record
        for record in caplog.records
        if record.name == "backend.routers.runner_control"
        and record.getMessage().startswith("runner_control.channel.auth.failed")
    ]
    assert len(warning_records) == 1
    message = warning_records[0].getMessage()
    assert raw_secret not in message
    assert "fields={'install_token': '<NO_INSTALL_TOKEN>', 'runner_secret': 'rsec...7890'}" in message


def test_runner_channel_requires_hello_first_and_logs_policy_close_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }
    caplog.set_level("INFO", logger="backend.routers.runner_control")

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.heartbeat",
                    payload={
                        "capacity": {
                            "active_tasks": 0,
                            "max_active_tasks": 2,
                            "available_tasks": 2,
                            "max_parallel_commands_per_task": 4,
                            "docker_available": True,
                            "runtime_image": "drowai-runtime-local:latest",
                            "runtime_image_available": True,
                            "version": "2.0.0",
                            "capabilities": ["docker"],
                            "labels": {"region": "us-east"},
                        }
                    },
                    message_id="hb-before-hello",
                )
            )
            error_payload = websocket.receive_json()
            assert error_payload["type"] == "error"
            assert error_payload["payload"]["error_code"] == "RUNNER_HELLO_REQUIRED"
            with pytest.raises(WebSocketDisconnect) as ws_close:
                websocket.receive_text()

    assert ws_close.value.code == status.WS_1008_POLICY_VIOLATION
    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.status == "offline"
    assert refreshed_runner.last_seen_at is not None

    close_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.routers.runner_control"
        and record.getMessage().startswith("runner_control.channel.closed")
    ]
    assert any("event=RUNNER_CHANNEL_CLOSED_POLICY_VIOLATION" in message for message in close_logs)


def test_runner_channel_connect_then_disconnect_keeps_runner_non_active_until_hello() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers):
            pass

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    connection = db.execute(
        select(RunnerConnection).where(
            RunnerConnection.tenant_id == tenant.id,
            RunnerConnection.runner_id == runner.id,
        )
    ).scalar_one()

    assert refreshed_runner.status == "offline"
    assert refreshed_runner.last_seen_at is not None
    assert connection.status == "disconnected"
    assert connection.last_seen_at is not None


def test_runner_channel_heartbeat_updates_runner_state_and_connection_lease() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.hello",
                    payload={
                        "version": "1.9.0",
                        "capabilities": ["docker", "kali"],
                        "labels": {"region": "us-east", "tier": "gold"},
                    },
                    message_id="hello-1",
                )
            )
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.heartbeat",
                    payload={
                        "capacity": {
                            "active_tasks": 1,
                            "max_active_tasks": 4,
                            "available_tasks": 3,
                            "max_parallel_commands_per_task": 6,
                            "docker_available": True,
                            "runtime_image": "drowai-runtime-local:latest",
                            "runtime_image_available": True,
                            "version": "1.9.0",
                            "capabilities": ["docker", "kali"],
                            "labels": {"region": "us-east", "tier": "gold"},
                        }
                    },
                    message_id="hb-1",
                )
            )
            websocket.close()

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    connection = db.execute(
        select(RunnerConnection).where(
            RunnerConnection.tenant_id == tenant.id,
            RunnerConnection.runner_id == runner.id,
        )
    ).scalar_one()

    assert refreshed_runner.status == "offline"
    assert refreshed_runner.version == "1.9.0"
    assert refreshed_runner.labels_json == {"region": "us-east", "tier": "gold"}
    assert refreshed_runner.capabilities_json == ["docker", "kali"]
    assert refreshed_runner.capacity_json == {
        "active_tasks": 1,
        "max_active_tasks": 4,
        "available_tasks": 3,
        "max_parallel_commands_per_task": 6,
        "docker_available": True,
        "runtime_image": "drowai-runtime-local:latest",
        "runtime_image_available": True,
        "version": "1.9.0",
        "capabilities": ["docker", "kali"],
        "labels": {"region": "us-east", "tier": "gold"},
        "active_runtime_jobs": [],
    }
    assert refreshed_runner.last_seen_at is not None
    assert connection.status == "disconnected"
    assert connection.last_seen_at is not None
    assert connection.lease_expires_at >= connection.last_seen_at


def test_runner_channel_unknown_and_runtime_messages_return_deterministic_errors() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.hello",
                    payload={"version": "2.0.0", "capabilities": ["docker"], "labels": {"region": "eu-west"}},
                    message_id="hello-unknown-test",
                )
            )

            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.not-real",
                    payload={},
                    message_id="unknown-type",
                )
            )
            unknown_error = websocket.receive_json()
            assert unknown_error["type"] == "error"
            assert unknown_error["payload"]["error_code"] == "RUNNER_MESSAGE_TYPE_UNKNOWN"

            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="task.start",
                    payload={},
                    message_id="task-start-disabled",
                )
            )
            disabled_error = websocket.receive_json()
            assert disabled_error["type"] == "error"
            assert disabled_error["payload"]["error_code"] == "RUNNER_CONTROL_NOT_IMPLEMENTED"


def test_runner_channel_dispatches_cross_session_outbound_message_and_persists_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    enqueue_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        enqueue_store = DBRunnerCoordinationStore(enqueue_session, pod_id="enqueue-pod")
        enqueue_store.enqueue_outbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="outbound-live-1",
            message_type="task.start",
            payload_json={"command": "start"},
            idempotency_key="outbound-live-1",
            runtime_job_id=None,
            task_id=42,
            correlation_id="corr-live-1",
        )
        enqueue_session.commit()
    finally:
        enqueue_session.close()

    monkeypatch.setenv("HOSTNAME", "channel-pod")
    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            outbound = websocket.receive_json()
            assert outbound["message_id"] == "outbound-live-1"
            assert outbound["type"] == "task.start"
            assert outbound["tenant_id"] == str(tenant.id)
            assert outbound["runner_id"] == str(runner.id)

            pre_ack_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
            try:
                pre_ack_row = pre_ack_session.execute(
                    select(RunnerControlMessage).where(
                        RunnerControlMessage.tenant_id == tenant.id,
                        RunnerControlMessage.runner_id == runner.id,
                        RunnerControlMessage.direction == "outbound",
                        RunnerControlMessage.message_id == "outbound-live-1",
                    )
                ).scalar_one()
                assert pre_ack_row.status != "acked"
            finally:
                pre_ack_session.close()

            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.hello",
                    payload={
                        "version": "2.0.0",
                        "capabilities": ["docker"],
                        "labels": {"region": "us-east"},
                    },
                    message_id="hello-live-1",
                )
            )
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.ack",
                    payload={
                        "acked_message_id": "outbound-live-1",
                        "status": "accepted",
                        "error_code": None,
                    },
                    message_id="runner-ack-live-1",
                    correlation_id="corr-live-1",
                )
            )
            websocket.close()

    verification_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        outbound_row = verification_session.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant.id,
                RunnerControlMessage.runner_id == runner.id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.message_id == "outbound-live-1",
            )
        ).scalar_one()
        assert outbound_row.status == "acked"
        assert outbound_row.delivery_attempt_count >= 1
    finally:
        verification_session.close()


def test_runner_channel_dispatch_timeout_marks_message_failed_when_ack_missing() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    enqueue_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        enqueue_store = DBRunnerCoordinationStore(enqueue_session, pod_id="enqueue-pod")
        enqueue_store.enqueue_outbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="outbound-timeout-live-1",
            message_type="task.start",
            payload_json={"delivery_policy": {"timeout_seconds": 0.2, "max_attempts": 1}},
            idempotency_key="outbound-timeout-live-1",
            runtime_job_id=None,
            task_id=7,
            correlation_id="corr-timeout-live-1",
        )
        enqueue_session.commit()
    finally:
        enqueue_session.close()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            outbound = websocket.receive_json()
            assert outbound["message_id"] == "outbound-timeout-live-1"
            websocket.send_text(
                _runner_envelope(
                    tenant_id=tenant.id,
                    runner_id=runner.id,
                    message_type="runner.hello",
                    payload={
                        "version": "2.0.0",
                        "capabilities": ["docker"],
                        "labels": {"region": "us-east"},
                    },
                    message_id="hello-timeout-live-1",
                )
            )
            time.sleep(0.35)
            websocket.close()

    verification_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        outbound_row = verification_session.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant.id,
                RunnerControlMessage.runner_id == runner.id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.message_id == "outbound-timeout-live-1",
            )
        ).scalar_one()
        assert outbound_row.status == "failed"
        assert outbound_row.error_code == "RUNNER_ACK_TIMEOUT"
        assert outbound_row.delivery_attempt_count >= 1
    finally:
        verification_session.close()


def test_runner_channel_close_before_ack_applies_offline_policy() -> None:
    db = _build_session()
    _user, tenant, _site, runner = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    enqueue_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        enqueue_store = DBRunnerCoordinationStore(enqueue_session, pod_id="enqueue-pod")
        enqueue_store.enqueue_outbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="outbound-offline-live-1",
            message_type="task.start",
            payload_json={"delivery_policy": {"offline": "fail", "max_attempts": 3}},
            idempotency_key="outbound-offline-live-1",
            runtime_job_id=None,
            task_id=8,
            correlation_id="corr-offline-live-1",
        )
        enqueue_session.commit()
    finally:
        enqueue_session.close()

    headers = {
        "x-runner-tenant-id": str(tenant.id),
        "x-runner-id": str(runner.id),
        "x-runner-credential-secret": issued.plaintext_secret,
    }

    with _make_registration_client(db) as client:
        with client.websocket_connect("/api/runner-control/channel", headers=headers) as websocket:
            outbound = websocket.receive_json()
            assert outbound["message_id"] == "outbound-offline-live-1"
            websocket.close()

    verification_session = Session(bind=db.get_bind(), autoflush=False, autocommit=False)
    try:
        outbound_row = verification_session.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant.id,
                RunnerControlMessage.runner_id == runner.id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.message_id == "outbound-offline-live-1",
            )
        ).scalar_one()
        assert outbound_row.status == "failed"
        assert outbound_row.error_code == "RUNNER_OFFLINE"
        assert outbound_row.delivery_attempt_count >= 1
    finally:
        verification_session.close()
