"""Router contract tests for reporting storage input inventory endpoint."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.routers.reporting import router as reporting_router
from backend.routers.reporting import inputs as inputs_routes
from backend.schemas.reporting import EngagementReportingInputsResponse
from backend.services.reporting.input_inventory_service import (
    ReportingInputInventoryNotFoundError,
)


@pytest.fixture
def reporting_inputs_app() -> FastAPI:
    app = FastAPI()
    app.include_router(reporting_router)

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="owner")

    def fake_db():
        yield object()

    app.dependency_overrides[inputs_routes.get_current_user] = fake_current_user
    app.dependency_overrides[inputs_routes.get_tenant_request_context] = fake_tenant_context
    app.dependency_overrides[inputs_routes.get_db] = fake_db
    return app


def test_inputs_endpoint_declares_response_model() -> None:
    matching_routes = [
        route
        for route in reporting_router.routes
        if getattr(route, "path", "") == "/api/reporting/engagements/{engagement_id}/inputs"
    ]

    assert len(matching_routes) == 1
    assert matching_routes[0].response_model is EngagementReportingInputsResponse


def test_inputs_endpoint_calls_inventory_service_with_tenant_and_user_context(
    monkeypatch: pytest.MonkeyPatch,
    reporting_inputs_app: FastAPI,
) -> None:
    calls = []

    class FakeInputInventoryService:
        def __init__(self, db):
            self.db = db

        def list_engagement_inputs(self, *, tenant_id: int, user_id: int, engagement_id: int):
            calls.append(
                {
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "engagement_id": engagement_id,
                }
            )
            return EngagementReportingInputsResponse(engagement_id=engagement_id, tasks=[])

    monkeypatch.setattr(inputs_routes, "InputInventoryService", FakeInputInventoryService)

    client = TestClient(reporting_inputs_app)
    response = client.get("/api/reporting/engagements/45/inputs")

    assert response.status_code == 200, response.text
    assert response.json() == {"engagement_id": 45, "tasks": []}
    assert len(calls) == 1
    assert calls[0]["tenant_id"] == 701
    assert calls[0]["user_id"] == 11
    assert calls[0]["engagement_id"] == 45


def test_inputs_endpoint_enforces_report_read_permission(
    monkeypatch: pytest.MonkeyPatch,
    reporting_inputs_app: FastAPI,
) -> None:
    class FakeInputInventoryService:
        def __init__(self, db):
            self.db = db

        def list_engagement_inputs(self, **_kwargs):
            raise AssertionError("service must not run when authorization fails")

    def unauthorized_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="no-access")

    reporting_inputs_app.dependency_overrides[
        inputs_routes.get_tenant_request_context
    ] = unauthorized_tenant_context
    monkeypatch.setattr(inputs_routes, "InputInventoryService", FakeInputInventoryService)

    client = TestClient(reporting_inputs_app)
    response = client.get("/api/reporting/engagements/45/inputs")

    assert response.status_code == 403, response.text


def test_inputs_endpoint_maps_non_owned_engagement_to_404(
    monkeypatch: pytest.MonkeyPatch,
    reporting_inputs_app: FastAPI,
) -> None:
    class FakeInputInventoryService:
        def __init__(self, db):
            self.db = db

        def list_engagement_inputs(self, **_kwargs):
            raise ReportingInputInventoryNotFoundError("Engagement not found")

    monkeypatch.setattr(inputs_routes, "InputInventoryService", FakeInputInventoryService)

    client = TestClient(reporting_inputs_app)
    response = client.get("/api/reporting/engagements/999/inputs")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"
