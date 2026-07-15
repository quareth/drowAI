"""Router coverage for the authenticated system metrics settings endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import system_metrics as routes
from backend.schemas.system_metrics import ResourceUsage, SystemMetricsResponse


def _client(*, authenticated: bool, role: str = "owner") -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)

    if authenticated:
        app.dependency_overrides[routes.get_current_user] = lambda: SimpleNamespace(id=7)
        app.dependency_overrides[routes.get_tenant_request_context] = lambda: SimpleNamespace(
            tenant_id=3,
            user_id=7,
            role=role,
        )

    return TestClient(app)


def test_metrics_requires_authentication() -> None:
    response = _client(authenticated=False).get("/api/settings/system/metrics")

    assert response.status_code == 401


def test_metrics_returns_service_snapshot(monkeypatch) -> None:
    expected = SystemMetricsResponse(
        memory=ResourceUsage(
            total_bytes=8_000,
            used_bytes=3_000,
            available_bytes=5_000,
            usage_percent=37.5,
        ),
        storage=ResourceUsage(
            total_bytes=20_000,
            used_bytes=4_000,
            available_bytes=16_000,
            usage_percent=20.0,
        ),
        uptime_seconds=3_600,
        collected_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    class FakeSystemMetricsService:
        def collect(self) -> SystemMetricsResponse:
            return expected

    monkeypatch.setattr(routes, "SystemMetricsService", FakeSystemMetricsService)

    response = _client(authenticated=True).get("/api/settings/system/metrics")

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")


def test_metrics_rejects_tenant_members_without_settings_permission() -> None:
    response = _client(authenticated=True, role="member").get("/api/settings/system/metrics")

    assert response.status_code == 403
