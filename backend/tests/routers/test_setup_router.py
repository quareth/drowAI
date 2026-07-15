"""Router tests for standalone setup wizard endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import setup as setup_router


def _setup_payload() -> dict[str, object]:
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
    }


class _IncompleteInstallationService:
    def __init__(self, _db):
        pass

    def is_wizard_enabled(self) -> bool:
        return True

    def is_complete(self) -> bool:
        return False


class _AlreadyCompleteInstallationService:
    def __init__(self, _db):
        pass

    def is_wizard_enabled(self) -> bool:
        return True

    def is_complete(self) -> bool:
        return True


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_setup_status_reports_required_for_standalone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    response = client.get("/api/setup/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["wizard_enabled"] is True
    assert payload["deployment_profile"] == "single_host"
    assert "setup_required" in payload
    assert payload["installation_status"] in {"pending", "provisioning", "complete", "failed"}
    assert "setup_error" in payload


def test_setup_status_enables_wizard_for_distributed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "distributed")
    response = client.get("/api/setup/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["wizard_enabled"] is True
    assert "setup_required" in payload
    assert payload["installation_status"] in {"pending", "provisioning", "complete", "failed"}
    assert "setup_error" in payload


def test_validate_database_uses_configured_database_probe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    with (
        patch(
            "backend.routers.setup.resolve_configured_database_identity",
            return_value=("drowai", "drowai_user"),
        ),
        patch(
            "backend.routers.setup.ping_configured_database",
            return_value=True,
        ) as mocked,
    ):
        response = client.post(
            "/api/setup/validate-database",
            json={
                "db_name": "drowai",
                "db_user": "drowai_user",
                "db_password": "secure-password",
            },
        )
    assert response.status_code == 200
    mocked.assert_called_once()


@pytest.mark.parametrize(
    ("field", "value"),
    (("db_name", "other_database"), ("db_user", "other_user")),
)
def test_validate_database_rejects_unconfigured_identity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    payload = dict(_setup_payload()["database"])
    payload[field] = value

    with (
        patch(
            "backend.routers.setup.resolve_configured_database_identity",
            return_value=("drowai", "drowai_user"),
        ),
        patch("backend.routers.setup.ping_configured_database") as probe,
    ):
        response = client.post("/api/setup/validate-database", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Database name and username must match the configured database"
    probe.assert_not_called()


def test_complete_setup_rejects_unconfigured_database_identity_before_provisioning(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    monkeypatch.setattr(
        setup_router,
        "PlatformInstallationService",
        _IncompleteInstallationService,
    )
    payload = _setup_payload()
    payload["database"]["db_user"] = "other_user"

    with (
        patch(
            "backend.routers.setup.resolve_configured_database_identity",
            return_value=("drowai", "drowai_user"),
        ),
        patch("backend.routers.setup.SetupCompletionService") as completion_service,
    ):
        response = client.post("/api/setup/complete", json=payload)

    assert response.status_code == 400
    completion_service.assert_not_called()


def test_complete_setup_is_idempotent_when_already_complete(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    start_background_services = AsyncMock(return_value=True)
    monkeypatch.setattr(
        setup_router,
        "PlatformInstallationService",
        _AlreadyCompleteInstallationService,
    )
    monkeypatch.setattr(
        setup_router,
        "start_background_services",
        start_background_services,
    )

    response = client.post(
        "/api/setup/complete",
        json=_setup_payload(),
    )
    start_background_services.assert_awaited_once_with()
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["redirect"] == "/auth"
    assert "install_token" not in payload
    assert "execution_site_id" not in payload
    assert "tenant_id" not in payload
    assert payload["runner_enrollment_published"] is False
    assert payload["runtime_services_started"] is True
    assert payload["restart_required"] is False


def test_complete_setup_starts_deferred_background_services(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    start_background_services = AsyncMock(return_value=True)

    monkeypatch.setattr(
        setup_router,
        "PlatformInstallationService",
        _IncompleteInstallationService,
    )
    monkeypatch.setattr(setup_router, "SetupCompletionService", _SetupCompletionService)
    monkeypatch.setattr(
        setup_router,
        "start_background_services",
        start_background_services,
    )

    response = client.post(
        "/api/setup/complete",
        json=_setup_payload(),
    )

    assert response.status_code == 200, response.text
    start_background_services.assert_awaited_once_with()
    assert response.json()["runtime_services_started"] is True
    assert response.json()["restart_required"] is False
    assert "install_token" not in response.json()
    assert "execution_site_id" not in response.json()


def test_complete_setup_reports_background_service_startup_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    start_background_services = AsyncMock(side_effect=RuntimeError("scheduler unavailable"))
    monkeypatch.setattr(
        setup_router,
        "PlatformInstallationService",
        _IncompleteInstallationService,
    )
    monkeypatch.setattr(setup_router, "SetupCompletionService", _SetupCompletionService)
    monkeypatch.setattr(
        setup_router,
        "start_background_services",
        start_background_services,
    )

    response = client.post("/api/setup/complete", json=_setup_payload())

    assert response.status_code == 200, response.text
    start_background_services.assert_awaited_once_with()
    assert response.json()["runtime_services_started"] is False
    assert response.json()["restart_required"] is True
