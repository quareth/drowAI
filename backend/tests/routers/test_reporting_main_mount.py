"""Route continuity tests for reporting namespace wiring."""

from __future__ import annotations

from fastapi.routing import APIRoute

from backend.main import app


def _http_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def test_reporting_namespace_is_mounted_on_main_app() -> None:
    paths = {route.path for route in _http_routes()}

    assert "/api/reporting/engagements/{engagement_id}/inputs" in paths
    assert "/api/reporting/engagements/{engagement_id}/reports" in paths
    assert "/api/reporting/engagements/{engagement_id}/reports/current" in paths
    assert "/api/reporting/engagements/{engagement_id}/reports/history" in paths
    assert "/api/reporting/reports/{report_id}" in paths
    assert "/api/reporting/jobs/{job_id}" in paths
    assert "/api/reporting/engagements/{engagement_id}/jobs/{job_id}" in paths
    assert "/api/reporting/tasks/{task_id}/memo/prepare" in paths
    assert "/api/reporting/tasks/{task_id}/memo/current" in paths
    assert "/api/reporting/tasks/{task_id}/memo/history" in paths


def test_legacy_reports_namespace_remains_mounted_with_existing_prefix_and_tags() -> None:
    legacy_routes = [
        route for route in _http_routes() if route.path.startswith("/api/reports")
    ]

    assert legacy_routes
    assert any(route.path == "/api/reports/" for route in legacy_routes)
    assert any(route.path == "/api/reports/task/{task_id}" for route in legacy_routes)
    assert all("reports" in route.tags for route in legacy_routes)
