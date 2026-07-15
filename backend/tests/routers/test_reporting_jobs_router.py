"""Router contract tests for engagement report job status endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.reporting import EngagementReportJob
from backend.models.tenant import Tenant
from backend.routers.reporting import router as reporting_router
from backend.routers.reporting import jobs as jobs_routes
from backend.schemas.reporting import (
    EngagementReportActiveJobResponse,
    EngagementReportJobStatusResponse,
)


REPORTING_JOBS_ROUTER_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    EngagementReportJob.__table__,
]


@pytest.fixture
def reporting_jobs_app() -> FastAPI:
    app = FastAPI()
    app.include_router(reporting_router)

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="owner")

    def fake_db():
        yield object()

    app.dependency_overrides[jobs_routes.get_current_user] = fake_current_user
    app.dependency_overrides[jobs_routes.get_tenant_request_context] = fake_tenant_context
    app.dependency_overrides[jobs_routes.get_db] = fake_db
    return app


def test_job_status_endpoint_declares_response_model() -> None:
    routes_by_path = {
        getattr(route, "path", ""): route for route in reporting_router.routes
    }

    assert (
        routes_by_path["/api/reporting/jobs/{job_id}"].response_model
        is EngagementReportJobStatusResponse
    )
    assert (
        routes_by_path[
            "/api/reporting/engagements/{engagement_id}/jobs/{job_id}"
        ].response_model
        is EngagementReportJobStatusResponse
    )
    assert (
        routes_by_path[
            "/api/reporting/engagements/{engagement_id}/jobs/active"
        ].response_model
        is EngagementReportActiveJobResponse
    )


def test_active_job_endpoint_returns_latest_active_job_and_scopes_read(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    calls = []
    job_id = uuid4()
    created_at = datetime(2026, 6, 9, 5, 0, tzinfo=UTC)

    def fake_get_owned_engagement_or_404(
        db,
        engagement_id: int,
        user_id: int,
        tenant_id: int,
    ):
        calls.append(
            {
                "kind": "ownership",
                "db": db,
                "engagement_id": engagement_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
            }
        )
        return SimpleNamespace(id=engagement_id)

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_active_job(
            self,
            *,
            tenant_id: int,
            user_id: int,
            requested_by_user_id: int,
            engagement_id: int,
            report_type: str,
        ):
            calls.append(
                {
                    "kind": "active_job",
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "requested_by_user_id": requested_by_user_id,
                    "engagement_id": engagement_id,
                    "report_type": report_type,
                }
            )
            return EngagementReportActiveJobResponse(
                job=EngagementReportJobStatusResponse(
                    id=job_id,
                    engagement_id=engagement_id,
                    report_id=None,
                    report_type="pentest",
                    status="queued",
                    generation_phase="sections",
                    selected_task_memo_ids=[],
                    include_candidate_findings=False,
                    source_watermark={"schema_version": 1},
                    current_section_id=None,
                    completed_sections=[],
                    total_sections=0,
                    attempt_count=0,
                    max_attempts=3,
                    last_error_code=None,
                    error_message=None,
                    created_at=created_at,
                    updated_at=created_at,
                    started_at=None,
                    finished_at=None,
                )
            )

    monkeypatch.setattr(
        jobs_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        "/api/reporting/engagements/45/jobs/active?report_type=pentest"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["job"]["id"] == str(job_id)
    assert payload["job"]["status"] == "queued"
    assert [call["kind"] for call in calls] == ["ownership", "active_job"]
    assert calls[1]["tenant_id"] == 701
    assert calls[1]["user_id"] == 11
    assert calls[1]["requested_by_user_id"] == 11
    assert calls[1]["engagement_id"] == 45
    assert calls[1]["report_type"] == "pentest"


def test_active_job_endpoint_returns_null_when_no_active_job(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    monkeypatch.setattr(
        jobs_routes,
        "get_owned_engagement_or_404",
        lambda *_args, **_kwargs: SimpleNamespace(id=45),
    )

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_active_job(self, **_kwargs):
            return EngagementReportActiveJobResponse(job=None)

    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        "/api/reporting/engagements/45/jobs/active?report_type=pentest"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"job": None}


def test_job_status_endpoint_returns_existing_job_and_scopes_read(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    calls = []
    job_id = uuid4()
    report_id = uuid4()
    created_at = datetime(2026, 6, 9, 5, 0, tzinfo=UTC)
    updated_at = datetime(2026, 6, 9, 5, 1, tzinfo=UTC)
    started_at = datetime(2026, 6, 9, 5, 2, tzinfo=UTC)

    def fake_get_owned_engagement_or_404(
        db,
        engagement_id: int,
        user_id: int,
        tenant_id: int,
    ):
        calls.append(
            {
                "kind": "ownership",
                "db": db,
                "engagement_id": engagement_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
            }
        )
        return SimpleNamespace(id=engagement_id)

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_job_status(
            self,
            *,
            tenant_id: int,
            user_id: int,
            requested_by_user_id: int,
            engagement_id: int,
            job_id: str,
        ):
            calls.append(
                {
                    "kind": "job",
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "requested_by_user_id": requested_by_user_id,
                    "engagement_id": engagement_id,
                    "job_id": job_id,
                }
            )
            return EngagementReportJobStatusResponse(
                id=job_id,
                engagement_id=engagement_id,
                report_id=report_id,
                report_type="pentest",
                status="generating",
                generation_phase="sections",
                selected_task_memo_ids=[str(uuid4())],
                include_candidate_findings=True,
                source_watermark={"schema_version": 1},
                current_section_id="executive_summary",
                completed_sections=["scope", "methodology"],
                total_sections=5,
                attempt_count=2,
                max_attempts=3,
                last_error_code="SECTION_RETRY",
                error_message="Retrying section",
                created_at=created_at,
                updated_at=updated_at,
                started_at=started_at,
                finished_at=None,
            )

    monkeypatch.setattr(
        jobs_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        f"/api/reporting/engagements/45/jobs/{job_id}"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == str(job_id)
    assert payload["engagement_id"] == 45
    assert payload["report_id"] == str(report_id)
    assert payload["report_type"] == "pentest"
    assert payload["status"] == "generating"
    assert payload["current_section_id"] == "executive_summary"
    assert payload["completed_sections"] == ["scope", "methodology"]
    assert payload["total_sections"] == 5
    assert payload["attempt_count"] == 2
    assert payload["max_attempts"] == 3
    assert payload["last_error_code"] == "SECTION_RETRY"
    assert payload["error_message"] == "Retrying section"
    assert payload["created_at"].startswith("2026-06-09T05:00:00")
    assert payload["updated_at"].startswith("2026-06-09T05:01:00")
    assert payload["started_at"].startswith("2026-06-09T05:02:00")
    assert payload["finished_at"] is None
    assert [call["kind"] for call in calls] == ["ownership", "job"]
    assert calls[0]["tenant_id"] == 701
    assert calls[0]["user_id"] == 11
    assert calls[1]["tenant_id"] == 701
    assert calls[1]["user_id"] == 11
    assert calls[1]["requested_by_user_id"] == 11
    assert calls[1]["engagement_id"] == 45
    assert calls[1]["job_id"] == str(job_id)


def test_engagement_job_status_endpoint_returns_404_for_foreign_requester_job(
    reporting_jobs_app: FastAPI,
) -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=REPORTING_JOBS_ROUTER_TABLES)
    factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    try:
        with factory() as db:
            tenant = Tenant(slug=f"tenant-{uuid4().hex}", name="Tenant")
            owner = User(username=f"owner-{uuid4().hex}", password="hashed-password")
            requester = User(
                username=f"requester-{uuid4().hex}", password="hashed-password"
            )
            db.add_all([tenant, owner, requester])
            db.flush()

            engagement = Engagement(
                tenant_id=tenant.id,
                user_id=owner.id,
                name="Engagement",
            )
            db.add(engagement)
            db.flush()

            job = EngagementReportJob(
                tenant_id=tenant.id,
                user_id=owner.id,
                requested_by_user_id=requester.id,
                engagement_id=engagement.id,
                report_type="pentest",
                status="queued",
                idempotency_key=f"job-{uuid4()}",
                selected_task_memo_ids=[],
                include_candidate_findings=False,
                source_watermark={"schema_version": 1},
                completed_sections=[],
                total_sections=5,
                attempt_count=0,
                max_attempts=3,
            )
            db.add(job)
            db.commit()

            def scoped_current_user():
                return SimpleNamespace(id=owner.id, username="owner", is_active=True)

            def scoped_tenant_context():
                return SimpleNamespace(
                    tenant_id=tenant.id,
                    user_id=owner.id,
                    role="owner",
                )

            def scoped_db():
                yield db

            reporting_jobs_app.dependency_overrides[
                jobs_routes.get_current_user
            ] = scoped_current_user
            reporting_jobs_app.dependency_overrides[
                jobs_routes.get_tenant_request_context
            ] = scoped_tenant_context
            reporting_jobs_app.dependency_overrides[jobs_routes.get_db] = scoped_db

            response = TestClient(reporting_jobs_app).get(
                f"/api/reporting/engagements/{engagement.id}/jobs/{job.id}"
            )

            assert response.status_code == 404, response.text
            assert response.json()["detail"] == "Report job not found"
    finally:
        engine.dispose()


def test_job_status_target_endpoint_scopes_read_by_requester(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    calls = []
    job_id = uuid4()
    created_at = datetime(2026, 6, 9, 5, 0, tzinfo=UTC)
    updated_at = datetime(2026, 6, 9, 5, 1, tzinfo=UTC)

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_job_status_by_id(
            self,
            *,
            tenant_id: int,
            user_id: int,
            requested_by_user_id: int,
            job_id: str,
        ):
            calls.append(
                {
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "requested_by_user_id": requested_by_user_id,
                    "job_id": job_id,
                }
            )
            return EngagementReportJobStatusResponse(
                id=job_id,
                engagement_id=45,
                report_id=None,
                report_type="pentest",
                status="queued",
                generation_phase="sections",
                selected_task_memo_ids=[str(uuid4())],
                include_candidate_findings=False,
                source_watermark={"schema_version": 1},
                current_section_id=None,
                completed_sections=[],
                total_sections=5,
                attempt_count=0,
                max_attempts=3,
                created_at=created_at,
                updated_at=updated_at,
            )

    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(f"/api/reporting/jobs/{job_id}")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(job_id)
    assert calls == [
        {
            "db": calls[0]["db"],
            "tenant_id": 701,
            "user_id": 11,
            "requested_by_user_id": 11,
            "job_id": str(job_id),
        }
    ]


def test_job_status_endpoint_returns_404_when_service_has_no_scoped_job(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    def fake_get_owned_engagement_or_404(*_args, **_kwargs):
        return SimpleNamespace(id=45)

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_job_status(self, **_kwargs):
            return None

    monkeypatch.setattr(
        jobs_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        f"/api/reporting/engagements/45/jobs/{uuid4()}"
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Report job not found"


def test_job_status_endpoint_enforces_report_read_permission_before_service(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_job_status(self, **_kwargs):
            raise AssertionError("service must not run when authorization fails")

    def unauthorized_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="no-access")

    reporting_jobs_app.dependency_overrides[
        jobs_routes.get_tenant_request_context
    ] = unauthorized_tenant_context
    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        f"/api/reporting/engagements/45/jobs/{uuid4()}"
    )

    assert response.status_code == 403, response.text


def test_job_status_endpoint_maps_foreign_or_missing_engagement_to_404(
    monkeypatch: pytest.MonkeyPatch,
    reporting_jobs_app: FastAPI,
) -> None:
    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_job_status(self, **_kwargs):
            raise AssertionError("service must not run for inaccessible engagements")

    def fake_get_owned_engagement_or_404(*_args, **_kwargs):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Engagement not found",
        )

    monkeypatch.setattr(
        jobs_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(jobs_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_jobs_app).get(
        f"/api/reporting/engagements/999/jobs/{uuid4()}"
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"
