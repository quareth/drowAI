"""Tests for additive engagement web-surface router endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import engagement_knowledge as engagement_routes


class _FakeQueryService:
    def __init__(self) -> None:
        self.origins_calls: list[dict[str, object]] = []
        self.paths_calls: list[dict[str, object]] = []

    def list_service_web_surface_origins(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        service_key: str,
        include_noisy: bool,
    ) -> dict[str, object]:
        self.origins_calls.append(
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "engagement_id": engagement_id,
                "service_key": service_key,
                "include_noisy": include_noisy,
            }
        )
        return {
            "service_key": service_key,
            "items": [{"origin_key": "https://example.com", "total_paths": 2}],
        }

    def list_service_web_surface_paths(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        filters,
    ) -> dict[str, object]:
        self.paths_calls.append(
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "engagement_id": engagement_id,
                "filters": filters.normalized(),
            }
        )
        normalized = filters.normalized()
        return {
            "service_key": normalized.service_key,
            "origin_key": normalized.origin_key,
            "items": [{"canonical_url": "https://example.com/admin"}],
            "total": 1,
            "limit": normalized.limit,
            "offset": normalized.offset,
            "hidden_noisy": 0,
        }


def _build_client():
    fake_service = _FakeQueryService()
    app = FastAPI()
    app.include_router(engagement_routes.router)

    def fake_get_db():
        yield object()

    def fake_get_current_user():
        return SimpleNamespace(id=77, username="owner", is_active=True)

    app.dependency_overrides[engagement_routes.get_db] = fake_get_db
    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[engagement_routes.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=601, user_id=77, role="owner")
    )
    return app, fake_service


def test_web_surface_origins_endpoint_uses_service_key_query(monkeypatch) -> None:
    app, fake_service = _build_client()
    monkeypatch.setattr(engagement_routes, "_query_service", lambda _db: fake_service)
    monkeypatch.setattr(
        engagement_routes,
        "get_engagement_in_tenant_or_404",
        lambda db, engagement_id, tenant_id: SimpleNamespace(id=engagement_id, tenant_id=tenant_id),
    )

    client = TestClient(app)
    try:
        response = client.get(
            "/api/engagements/42/web-surface?service_key=service.socket:10.0.0.10/tcp/443&include_noisy=true",
            headers={"Authorization": "Bearer owner-token"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["service_key"] == "service.socket:10.0.0.10/tcp/443"
    assert len(payload["items"]) == 1
    assert fake_service.origins_calls == [
        {
            "user_id": 77,
            "tenant_id": 601,
            "engagement_id": 42,
            "service_key": "service.socket:10.0.0.10/tcp/443",
            "include_noisy": True,
        }
    ]


def test_web_surface_origins_endpoint_normalizes_include_noisy_string(monkeypatch) -> None:
    """Unrecognized/falsy include_noisy strings must NOT enable noisy rows.

    Regression: ``bool(include_noisy)`` treated any non-empty string as truthy,
    so callers passing ``"false"``/``"0"``/``"no"`` would unexpectedly receive
    noisy results. The router now goes through ``normalize_optional_bool`` and
    defaults unknown values to ``False`` like the path endpoint.
    """
    app, fake_service = _build_client()
    monkeypatch.setattr(engagement_routes, "_query_service", lambda _db: fake_service)
    monkeypatch.setattr(
        engagement_routes,
        "get_engagement_in_tenant_or_404",
        lambda db, engagement_id, tenant_id: SimpleNamespace(id=engagement_id, tenant_id=tenant_id),
    )

    client = TestClient(app)
    try:
        for raw_value in ("false", "0", "no", "off", "garbage"):
            response = client.get(
                "/api/engagements/42/web-surface"
                f"?service_key=service.socket:10.0.0.10/tcp/443&include_noisy={raw_value}",
                headers={"Authorization": "Bearer owner-token"},
            )
            assert response.status_code == 200, response.text
    finally:
        client.close()

    assert fake_service.origins_calls, "expected at least one origins call"
    for call in fake_service.origins_calls:
        assert call["include_noisy"] is False


def test_web_surface_paths_endpoint_uses_service_key_and_origin_query(monkeypatch) -> None:
    app, fake_service = _build_client()
    monkeypatch.setattr(engagement_routes, "_query_service", lambda _db: fake_service)
    monkeypatch.setattr(
        engagement_routes,
        "get_engagement_in_tenant_or_404",
        lambda db, engagement_id, tenant_id: SimpleNamespace(id=engagement_id, tenant_id=tenant_id),
    )

    client = TestClient(app)
    try:
        response = client.get(
            "/api/engagements/42/web-surface/paths"
            "?service_key=service.socket:10.0.0.10/tcp/443"
            "&origin_key=https://example.com&limit=10&offset=5",
            headers={"Authorization": "Bearer owner-token"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["service_key"] == "service.socket:10.0.0.10/tcp/443"
    assert payload["origin_key"] == "https://example.com"
    assert payload["total"] == 1
    recorded_filters = fake_service.paths_calls[0]["filters"]
    assert recorded_filters.service_key == "service.socket:10.0.0.10/tcp/443"
    assert recorded_filters.origin_key == "https://example.com"
    assert recorded_filters.limit == 10
    assert recorded_filters.offset == 5


def test_web_surface_paths_endpoint_normalizes_pagination_before_query_service(monkeypatch) -> None:
    app, fake_service = _build_client()
    monkeypatch.setattr(engagement_routes, "_query_service", lambda _db: fake_service)
    monkeypatch.setattr(
        engagement_routes,
        "get_engagement_in_tenant_or_404",
        lambda db, engagement_id, tenant_id: SimpleNamespace(id=engagement_id, tenant_id=tenant_id),
    )

    client = TestClient(app)
    try:
        paths_response = client.get(
            "/api/engagements/42/web-surface/paths"
            "?service_key=service.socket:10.0.0.10/tcp/443"
            "&origin_key=https://example.com&limit=150&offset=-1",
            headers={"Authorization": "Bearer owner-token"},
        )
    finally:
        client.close()

    assert paths_response.status_code == 200, paths_response.text
    payload = paths_response.json()
    assert payload["service_key"] == "service.socket:10.0.0.10/tcp/443"
    assert payload["origin_key"] == "https://example.com"
    assert payload["total"] == 1
    recorded_filters = fake_service.paths_calls[0]["filters"]
    assert recorded_filters.limit == 100
    assert recorded_filters.offset == 0
