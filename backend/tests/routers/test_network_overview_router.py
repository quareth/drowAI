"""Router coverage for the read-only tenant network overview endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import network_overview as routes
from backend.schemas.network_overview import ManagementNetworkOverview, NetworkOverviewResponse


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
        app.dependency_overrides[routes.get_db] = lambda: SimpleNamespace()
    return TestClient(app)


def test_network_overview_requires_authentication() -> None:
    assert _client(authenticated=False).get("/api/settings/network/overview").status_code == 401


def test_network_overview_rejects_members_without_settings_permission() -> None:
    assert _client(authenticated=True, role="member").get("/api/settings/network/overview").status_code == 403


def test_network_overview_returns_service_projection(monkeypatch) -> None:
    expected = NetworkOverviewResponse(
        deployment_profile="single_host",
        management=ManagementNetworkOverview(
            advertised_url="http://drowai.local",
            advertised_host="drowai.local",
            advertised_url_source="generated_config",
            primary_ip="192.168.1.20",
            interfaces=[],
            gateway_ip="192.168.1.1",
            gateway_interface="eth0",
            dns_servers=["192.168.1.1"],
        ),
        runners=[],
        collected_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    class FakeNetworkOverviewService:
        def __init__(self, db) -> None:
            self.db = db

        def collect(self, *, tenant_id: int, request) -> NetworkOverviewResponse:
            assert tenant_id == 3
            return expected

    monkeypatch.setattr(routes, "NetworkOverviewService", FakeNetworkOverviewService)

    response = _client(authenticated=True).get("/api/settings/network/overview")

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
