"""Router contract tests for task closure memo endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.routers.reporting import router as reporting_router
from backend.routers.reporting import memos as memos_routes
from backend.schemas.reporting import (
    TaskClosureMemoHistoryResponse,
    TaskClosureMemoPrepareResponse,
    TaskClosureMemoReadResponse,
)
from backend.services.reporting.contracts import (
    TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
)
from backend.services.reporting.task_memo_service import TaskMemoServiceError


@pytest.fixture
def reporting_memos_app() -> FastAPI:
    app = FastAPI()
    app.include_router(memos_routes.router, prefix="/api/reporting")

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="owner")

    def fake_db():
        yield object()

    app.dependency_overrides[memos_routes.get_current_user] = fake_current_user
    app.dependency_overrides[memos_routes.get_tenant_request_context] = fake_tenant_context
    app.dependency_overrides[memos_routes.get_db] = fake_db
    return app


def test_memo_endpoints_declare_response_models() -> None:
    routes_by_path = {
        getattr(route, "path", ""): route for route in memos_routes.router.routes
    }

    assert (
        routes_by_path["/tasks/{task_id}/memo/prepare"].response_model
        is TaskClosureMemoPrepareResponse
    )
    assert (
        routes_by_path["/tasks/{task_id}/memo/current"].response_model
        is TaskClosureMemoReadResponse
    )
    assert (
        routes_by_path["/tasks/{task_id}/memo/history"].response_model
        is TaskClosureMemoHistoryResponse
    )


def test_aggregate_reporting_router_mounts_memos_without_dropping_existing_routes() -> None:
    routes_by_path = {
        getattr(route, "path", ""): route for route in reporting_router.routes
    }

    assert "/api/reporting/tasks/{task_id}/memo/prepare" in routes_by_path
    assert "/api/reporting/tasks/{task_id}/memo/current" in routes_by_path
    assert "/api/reporting/tasks/{task_id}/memo/history" in routes_by_path
    assert "/api/reporting/engagements/{engagement_id}/inputs" in routes_by_path
    assert "/api/reporting/engagements/{engagement_id}/reports/current" in routes_by_path
    assert "/api/reporting/engagements/{engagement_id}/reports/history" in routes_by_path
    assert "/api/reporting/engagements/{engagement_id}/jobs/{job_id}" in routes_by_path


def test_prepare_endpoint_delegates_to_memo_service_with_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
    reporting_memos_app: FastAPI,
) -> None:
    calls = []
    memo = _memo_row(task_id=123, version=2)

    class FakeTaskMemoService:
        def __init__(self, db):
            self.db = db

        async def prepare_task_memo(
            self,
            *,
            tenant_id: int,
            user_id: int,
            task_id: int,
            regenerate: bool,
        ):
            calls.append(
                {
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "task_id": task_id,
                    "regenerate": regenerate,
                }
            )
            return memo

    monkeypatch.setattr(memos_routes, "TaskMemoService", FakeTaskMemoService)

    response = TestClient(reporting_memos_app).post(
        "/api/reporting/tasks/123/memo/prepare",
        json={"regenerate": True},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_id"] == 123
    assert payload["memo"]["id"] == str(memo.id)
    assert payload["memo"]["body"]["summary"] == "The task identified reportable evidence."
    assert calls == [
        {
            "db": calls[0]["db"],
            "tenant_id": 701,
            "user_id": 11,
            "task_id": 123,
            "regenerate": True,
        }
    ]


def test_read_endpoints_delegate_to_memo_service_with_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
    reporting_memos_app: FastAPI,
) -> None:
    calls = []
    current = _memo_row(task_id=123, version=2)
    previous = _memo_row(task_id=123, version=1, is_current=False)

    class FakeTaskMemoService:
        def __init__(self, db):
            self.db = db

        def get_current_task_memo(
            self,
            *,
            tenant_id: int,
            user_id: int,
            task_id: int,
        ):
            calls.append(("current", tenant_id, user_id, task_id))
            return current

        def list_task_memo_history(
            self,
            *,
            tenant_id: int,
            user_id: int,
            task_id: int,
            limit: int,
            offset: int,
        ):
            calls.append(("history", tenant_id, user_id, task_id, limit, offset))
            return [current, previous]

    monkeypatch.setattr(memos_routes, "TaskMemoService", FakeTaskMemoService)
    client = TestClient(reporting_memos_app)

    current_response = client.get("/api/reporting/tasks/123/memo/current")
    history_response = client.get(
        "/api/reporting/tasks/123/memo/history?limit=2&offset=1"
    )

    assert current_response.status_code == 200, current_response.text
    assert current_response.json()["version"] == 2
    assert history_response.status_code == 200, history_response.text
    assert history_response.json()["task_id"] == 123
    assert [item["version"] for item in history_response.json()["items"]] == [2, 1]
    assert calls == [
        ("current", 701, 11, 123),
        ("history", 701, 11, 123, 2, 1),
    ]


def test_viewer_cannot_prepare_but_can_read_current_and_history(
    monkeypatch: pytest.MonkeyPatch,
    reporting_memos_app: FastAPI,
) -> None:
    def viewer_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="viewer")

    class FakeTaskMemoService:
        def __init__(self, db):
            self.db = db

        async def prepare_task_memo(self, **_kwargs):
            raise AssertionError("service must not run when write authorization fails")

        def get_current_task_memo(self, **_kwargs):
            return _memo_row(task_id=123, version=1)

        def list_task_memo_history(self, **_kwargs):
            return []

    reporting_memos_app.dependency_overrides[
        memos_routes.get_tenant_request_context
    ] = viewer_tenant_context
    monkeypatch.setattr(memos_routes, "TaskMemoService", FakeTaskMemoService)
    client = TestClient(reporting_memos_app)

    prepare_response = client.post("/api/reporting/tasks/123/memo/prepare", json={})
    current_response = client.get("/api/reporting/tasks/123/memo/current")
    history_response = client.get("/api/reporting/tasks/123/memo/history")

    assert prepare_response.status_code == 403, prepare_response.text
    assert current_response.status_code == 200, current_response.text
    assert history_response.status_code == 200, history_response.text


@pytest.mark.parametrize(
    ("reason", "expected_status", "expected_detail"),
    [
        (TASK_MEMO_ERROR_TASK_NOT_FOUND, 404, "Task closure memo source not found"),
        (
            TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
            409,
            "Task is not eligible for memo preparation",
        ),
        (
            TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
            409,
            "Task memo preparation is already in progress.",
        ),
        (
            TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
            422,
            "Task closure memo request could not be processed",
        ),
        (TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE, 500, "Task closure memo preparation failed"),
    ],
)
def test_prepare_endpoint_maps_service_errors_without_leaking_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    reporting_memos_app: FastAPI,
    reason: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    class FakeTaskMemoService:
        def __init__(self, db):
            self.db = db

        async def prepare_task_memo(self, **_kwargs):
            raise TaskMemoServiceError(
                reason=reason,
                safe_message="internal source details must not leak",
            )

    monkeypatch.setattr(memos_routes, "TaskMemoService", FakeTaskMemoService)

    response = TestClient(reporting_memos_app).post(
        "/api/reporting/tasks/123/memo/prepare",
        json={},
    )

    assert response.status_code == expected_status, response.text
    assert response.json()["detail"] == expected_detail
    assert "internal source details" not in response.text


def test_current_endpoint_returns_404_when_no_current_memo(
    monkeypatch: pytest.MonkeyPatch,
    reporting_memos_app: FastAPI,
) -> None:
    class FakeTaskMemoService:
        def __init__(self, db):
            self.db = db

        def get_current_task_memo(self, **_kwargs):
            return None

    monkeypatch.setattr(memos_routes, "TaskMemoService", FakeTaskMemoService)

    response = TestClient(reporting_memos_app).get(
        "/api/reporting/tasks/123/memo/current"
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task closure memo not found"


def _memo_row(
    *,
    task_id: int,
    version: int,
    is_current: bool = True,
) -> SimpleNamespace:
    now = datetime(2026, 6, 9, 5, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid4(),
        schema_version="task_closure_memo.v1",
        engagement_id=45,
        task_id=task_id,
        version=version,
        status="ready",
        memo_mode="supported",
        is_current=is_current,
        source_watermark={"schema_version": "1"},
        memo={
            "task_name": "Evidence review",
            "summary": "The task identified reportable evidence.",
            "include_in_report_recommendation": {
                "include": True,
                "reason": "Evidence supports a reportable observation.",
            },
            "actions_performed": [
                {"text": "Reviewed service evidence.", "source": "transcript"}
            ],
            "reportable_observations": [
                {
                    "text": "The service exposed an admin panel.",
                    "confidence": "high",
                    "evidence_refs": ["evidence-1"],
                    "knowledge_refs": [],
                }
            ],
            "possible_findings": [],
            "limitations": [],
            "unsupported_notes": [],
            "evidence_refs": ["evidence-1"],
            "knowledge_refs": [],
        },
        error_message=None,
        created_at=now,
        updated_at=now,
        generated_at=now,
    )
