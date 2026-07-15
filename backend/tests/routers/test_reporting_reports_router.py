"""Router contract tests for engagement report generation and read endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReportJob, TaskClosureMemo
from backend.models.tenant import Tenant
from backend.routers.reporting import router as reporting_router
from backend.routers.reporting import reports as reports_routes
from backend.schemas.reporting import (
    CurrentEngagementReportResponse,
    EngagementReportGenerationRequest,
    EngagementReportGenerationResponse,
    EngagementReportHistoryResponse,
    EngagementReportDeleteResponse,
    EngagementReportReadResponse,
    EngagementReportSection,
    EngagementReportSectionSourceRefs,
    EngagementReportUndoDeleteResponse,
    ReportLibraryResponse,
)
from backend.services.reporting.source_watermark_service import SourceWatermarkService
from backend.services.llm_provider import (
    LLMCredentialService,
    ReportingLLMSelectionService,
)


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def reporting_reports_app() -> FastAPI:
    app = FastAPI()
    app.include_router(reporting_router)

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="owner")

    def fake_db():
        yield _FakeDb()

    app.dependency_overrides[reports_routes.get_current_user] = fake_current_user
    app.dependency_overrides[reports_routes.get_tenant_request_context] = (
        fake_tenant_context
    )
    app.dependency_overrides[reports_routes.get_db] = fake_db
    return app


@pytest.fixture(autouse=True)
def _report_scheduler_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scheduler_ready() -> bool:
        return True

    monkeypatch.setattr(reports_routes, "start_background_services", scheduler_ready)


def _build_session_factory() -> sessionmaker[Session]:
    assert backend_models.__all__
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _seed_report_generation_scope(
    db: Session,
) -> tuple[int, int, int, str]:
    tenant = Tenant(slug=f"tenant-{uuid4().hex[:8]}", name="Tenant")
    user = User(username=f"user-{uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    LLMCredentialService(db).upsert_api_key(
        user_id=user.id,
        provider=OPENAI_PROVIDER_ID,
        api_key="sk-report-router",
    )
    ReportingLLMSelectionService(db).set_selection(
        user_id=user.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
    )

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Engagement",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name="Task",
        status=TaskStatus.STOPPED.value,
    )
    db.add(task)
    db.flush()

    source_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = TaskClosureMemo(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        version=1,
        is_current=True,
        status="ready",
        memo_mode="supported",
        source_watermark=source_watermark,
        memo={"summary": "ready memo"},
    )
    db.add(memo)
    db.commit()
    return int(tenant.id), int(user.id), int(engagement.id), str(memo.id)


def test_report_endpoints_declare_response_models() -> None:
    routes_by_path_and_method = {
        (getattr(route, "path", ""), next(iter(getattr(route, "methods", {})))): route
        for route in reporting_router.routes
    }

    assert (
        routes_by_path_and_method[
            ("/api/reporting/engagements/{engagement_id}/reports", "POST")
        ].response_model
        is EngagementReportGenerationResponse
    )
    assert (
        routes_by_path_and_method[("/api/reporting/reports", "GET")].response_model
        is ReportLibraryResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/reports/{report_id}", "GET")
        ].response_model
        is EngagementReportReadResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/engagements/{engagement_id}/reports/current", "GET")
        ].response_model
        is CurrentEngagementReportResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/engagements/{engagement_id}/reports", "GET")
        ].response_model
        is EngagementReportHistoryResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/engagements/{engagement_id}/reports/history", "GET")
        ].response_model
        is EngagementReportHistoryResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/reports/{report_id}", "DELETE")
        ].response_model
        is EngagementReportDeleteResponse
    )
    assert (
        routes_by_path_and_method[
            ("/api/reporting/reports/{report_id}/undo-delete", "POST")
        ].response_model
        is EngagementReportUndoDeleteResponse
    )


def test_generation_endpoint_accepts_request_and_scopes_write(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    calls = []
    job_id = uuid4()
    selected_memo_id = uuid4()

    def fake_get_owned_engagement_or_404(
        db, engagement_id: int, user_id: int, tenant_id: int
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
        return SimpleNamespace(id=engagement_id, status="active")

    class FakeReportGenerationService:
        def __init__(self, db):
            self.db = db

        def request_generation(self, **kwargs):
            calls.append({"kind": "generation", "db": self.db, **kwargs})
            return SimpleNamespace(job_id=job_id, report_id=None, status="queued")

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(
        reports_routes,
        "ReportGenerationService",
        FakeReportGenerationService,
    )

    response = TestClient(reporting_reports_app).post(
        "/api/reporting/engagements/45/reports",
        json={
            "report_type": "pentest",
            "selected_task_memo_ids": [str(selected_memo_id)],
            "include_candidate_findings": True,
            "force_regenerate": True,
        },
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "job_id": str(job_id),
        "report_id": None,
        "status": "queued",
    }
    assert [call["kind"] for call in calls] == ["ownership", "generation"]
    assert calls[0]["tenant_id"] == 701
    assert calls[0]["user_id"] == 11
    assert calls[1]["tenant_id"] == 701
    assert calls[1]["user_id"] == 11
    assert calls[1]["requested_by_user_id"] == 11
    assert calls[1]["engagement_id"] == 45
    assert calls[1]["selected_task_memo_ids"] == [selected_memo_id]
    assert calls[1]["include_candidate_findings"] is True
    assert calls[1]["force_regenerate"] is True
    assert calls[1]["engagement_is_owned"] is True


@pytest.mark.asyncio
async def test_generation_route_commits_queued_job_without_scheduling_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    job_id = uuid4()
    selected_memo_id = uuid4()

    class FakeDb(_FakeDb):
        def commit(self) -> None:
            calls.append("commit")
            super().commit()

    def fake_get_owned_engagement_or_404(*_args, **_kwargs):
        calls.append("ownership")
        return SimpleNamespace(id=45, status="active")

    class FakeReportGenerationService:
        def __init__(self, db):
            self.db = db

        def request_generation(self, **_kwargs):
            calls.append("generation")
            return SimpleNamespace(job_id=job_id, report_id=None, status="queued")

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(
        reports_routes,
        "ReportGenerationService",
        FakeReportGenerationService,
    )

    response = await reports_routes.generate_engagement_report(
        engagement_id=45,
        request=EngagementReportGenerationRequest(
            report_type="pentest",
            selected_task_memo_ids=[selected_memo_id],
        ),
        current_user=SimpleNamespace(id=11),
        tenant_context=SimpleNamespace(tenant_id=701, user_id=11, role="owner"),
        db=FakeDb(),  # type: ignore[arg-type]
    )

    assert response == EngagementReportGenerationResponse(
        job_id=job_id,
        report_id=None,
        status="queued",
    )
    assert calls == ["ownership", "generation", "commit"]


@pytest.mark.asyncio
async def test_generation_route_does_not_commit_current_ready_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    report_id = uuid4()
    selected_memo_id = uuid4()

    class FakeDb(_FakeDb):
        def commit(self) -> None:
            calls.append("commit")
            super().commit()

    class FakeReportGenerationService:
        def __init__(self, db):
            self.db = db

        def request_generation(self, **_kwargs):
            return SimpleNamespace(job_id=None, report_id=report_id, status="ready")

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        lambda *_args, **_kwargs: SimpleNamespace(id=45, status="active"),
    )
    monkeypatch.setattr(
        reports_routes,
        "ReportGenerationService",
        FakeReportGenerationService,
    )

    response = await reports_routes.generate_engagement_report(
        engagement_id=45,
        request=EngagementReportGenerationRequest(
            report_type="pentest",
            selected_task_memo_ids=[selected_memo_id],
        ),
        current_user=SimpleNamespace(id=11),
        tenant_context=SimpleNamespace(tenant_id=701, user_id=11, role="owner"),
        db=FakeDb(),  # type: ignore[arg-type]
    )

    assert response == EngagementReportGenerationResponse(
        job_id=None,
        report_id=report_id,
        status="ready",
    )
    assert calls == []


@pytest.mark.asyncio
async def test_generation_route_rejects_archived_engagement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        lambda *_args, **_kwargs: SimpleNamespace(id=45, status="archived"),
    )

    with pytest.raises(HTTPException) as exc:
        await reports_routes.generate_engagement_report(
            engagement_id=45,
            request=EngagementReportGenerationRequest(
                report_type="pentest",
                selected_task_memo_ids=[uuid4()],
            ),
            current_user=SimpleNamespace(id=11),
            tenant_context=SimpleNamespace(tenant_id=701, user_id=11, role="owner"),
            db=_FakeDb(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 409
    assert "archived engagements" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_generation_route_returns_503_without_creating_job_when_scheduler_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_created = False

    async def scheduler_unavailable() -> bool:
        return False

    class FakeReportGenerationService:
        def __init__(self, _db):
            nonlocal service_created
            service_created = True

    db = _FakeDb()
    monkeypatch.setattr(
        reports_routes, "start_background_services", scheduler_unavailable
    )
    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        lambda *_args, **_kwargs: SimpleNamespace(id=45, status="active"),
    )
    monkeypatch.setattr(
        reports_routes,
        "ReportGenerationService",
        FakeReportGenerationService,
    )

    with pytest.raises(HTTPException) as exc:
        await reports_routes.generate_engagement_report(
            engagement_id=45,
            request=EngagementReportGenerationRequest(
                report_type="pentest",
                selected_task_memo_ids=[uuid4()],
            ),
            current_user=SimpleNamespace(id=11),
            tenant_context=SimpleNamespace(tenant_id=701, user_id=11, role="owner"),
            db=db,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert service_created is False
    assert db.commits == 0


def test_generation_endpoint_commits_durable_job_visible_from_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _build_session_factory()
    with session_factory() as seed_db:
        tenant_id, user_id, engagement_id, memo_id = _seed_report_generation_scope(
            seed_db
        )

    def fake_current_user():
        return SimpleNamespace(id=user_id, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=tenant_id, user_id=user_id, role="owner")

    def real_db():
        db = session_factory()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app = FastAPI()
    app.include_router(reporting_router)
    app.dependency_overrides[reports_routes.get_current_user] = fake_current_user
    app.dependency_overrides[reports_routes.get_tenant_request_context] = (
        fake_tenant_context
    )
    app.dependency_overrides[reports_routes.get_db] = real_db

    response = TestClient(app).post(
        f"/api/reporting/engagements/{engagement_id}/reports",
        json={
            "report_type": "pentest",
            "selected_task_memo_ids": [memo_id],
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["job_id"]
    assert payload["report_id"] is None
    assert payload["status"] == "queued"

    with session_factory() as check_db:
        job = check_db.scalars(select(EngagementReportJob)).one()
        assert str(job.id) == payload["job_id"]
        assert job.status == "queued"
        assert job.selected_task_memo_ids == [memo_id]


def test_current_endpoint_returns_stable_empty_shape_and_scopes_read(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    calls = []

    def fake_get_owned_engagement_or_404(
        db, engagement_id: int, user_id: int, tenant_id: int
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

        def get_current_report(
            self, *, tenant_id: int, user_id: int, engagement_id: int, report_type: str
        ):
            calls.append(
                {
                    "kind": "current",
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "engagement_id": engagement_id,
                    "report_type": report_type,
                }
            )
            return CurrentEngagementReportResponse(
                engagement_id=engagement_id,
                report_type=report_type,
                report=None,
            )

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        "/api/reporting/engagements/45/reports/current?report_type=pentest"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "engagement_id": 45,
        "report_type": "pentest",
        "report": None,
    }
    assert [call["kind"] for call in calls] == ["ownership", "current"]
    assert calls[0]["tenant_id"] == 701
    assert calls[0]["user_id"] == 11
    assert calls[1]["report_type"] == "pentest"


def test_history_endpoint_returns_empty_items_shape_and_scopes_read(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    calls = []

    def fake_get_owned_engagement_or_404(
        db, engagement_id: int, user_id: int, tenant_id: int
    ):
        calls.append((db, engagement_id, user_id, tenant_id))
        return SimpleNamespace(id=engagement_id)

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def list_report_history(
            self, *, tenant_id: int, user_id: int, engagement_id: int, report_type: str
        ):
            return EngagementReportHistoryResponse(
                engagement_id=engagement_id,
                report_type=report_type,
                reports=[],
            )

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        "/api/reporting/engagements/45/reports?report_type=pentest"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "engagement_id": 45,
        "report_type": "pentest",
        "reports": [],
    }
    assert len(calls) == 1
    assert calls[0][1:] == (45, 11, 701)


def test_report_library_endpoint_scopes_read_without_engagement(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    calls = []

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def list_report_library(self, **kwargs):
            calls.append({"db": self.db, **kwargs})
            return ReportLibraryResponse(reports=[], total=0, limit=25, offset=5)

    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        "/api/reporting/reports?report_type=pentest&query=alpha&limit=25&offset=5"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reports": [], "total": 0, "limit": 25, "offset": 5}
    assert calls == [
        {
            "db": ANY,
            "tenant_id": 701,
            "user_id": 11,
            "report_type": "pentest",
            "engagement_id": None,
            "query": "alpha",
            "limit": 25,
            "offset": 5,
        }
    ]


def test_direct_report_endpoint_returns_full_report_after_read_scope(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    report_id = uuid4()
    timestamp = datetime(2026, 6, 9, 5, 0, tzinfo=UTC)
    calls = []

    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_report(self, *, tenant_id: int, user_id: int, report_id: str):
            calls.append(
                {
                    "db": self.db,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "report_id": report_id,
                }
            )
            return EngagementReportReadResponse(
                id=report_id,
                schema_version="1",
                engagement_id=45,
                report_type="pentest",
                version=3,
                status="ready",
                is_current=True,
                title="Report",
                sections=[
                    EngagementReportSection(
                        schema_version="1",
                        section_id="executive_summary",
                        section_type="summary",
                        title="Executive Summary",
                        status="ready",
                        content_markdown="Full section",
                        blocks=[],
                        source_refs=EngagementReportSectionSourceRefs(
                            task_memo_ids=[],
                            knowledge_refs=[],
                            evidence_refs=[],
                        ),
                    )
                ],
                markdown_snapshot="# Report",
                source_task_memo_ids=[],
                source_knowledge_refs=[],
                source_evidence_refs=[],
                generation_metadata={},
                created_at=timestamp,
                updated_at=timestamp,
                generated_at=timestamp,
            )

    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        f"/api/reporting/reports/{report_id}"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == str(report_id)
    assert payload["sections"][0]["content_markdown"] == "Full section"
    assert calls == [
        {
            "db": ANY,
            "tenant_id": 701,
            "user_id": 11,
            "report_id": str(report_id),
        }
    ]


def test_report_endpoints_reject_unsupported_report_type_before_read_service(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_current_report(self, **_kwargs):
            raise AssertionError("service must not run for unsupported report types")

        def list_report_history(self, **_kwargs):
            raise AssertionError("service must not run for unsupported report types")

    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    client = TestClient(reporting_reports_app)
    current_response = client.get(
        "/api/reporting/engagements/45/reports/current?report_type=compliance"
    )
    history_response = client.get(
        "/api/reporting/engagements/45/reports/history?report_type=compliance"
    )

    assert current_response.status_code == 422, current_response.text
    assert history_response.status_code == 422, history_response.text


def test_report_endpoints_enforce_report_read_permission_before_service(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_current_report(self, **_kwargs):
            raise AssertionError("service must not run when authorization fails")

    def unauthorized_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="no-access")

    reporting_reports_app.dependency_overrides[
        reports_routes.get_tenant_request_context
    ] = unauthorized_tenant_context
    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        "/api/reporting/engagements/45/reports/current?report_type=pentest"
    )

    assert response.status_code == 403, response.text


def test_generation_endpoint_enforces_report_write_permission_before_service(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    class FakeReportGenerationService:
        def __init__(self, db):
            self.db = db

        def request_generation(self, **_kwargs):
            raise AssertionError("service must not run when authorization fails")

    def unauthorized_tenant_context():
        return SimpleNamespace(tenant_id=701, user_id=11, role="viewer")

    reporting_reports_app.dependency_overrides[
        reports_routes.get_tenant_request_context
    ] = unauthorized_tenant_context
    monkeypatch.setattr(
        reports_routes,
        "ReportGenerationService",
        FakeReportGenerationService,
    )

    response = TestClient(reporting_reports_app).post(
        "/api/reporting/engagements/45/reports",
        json={
            "report_type": "pentest",
            "selected_task_memo_ids": [str(uuid4())],
        },
    )

    assert response.status_code == 403, response.text


def test_report_endpoints_map_foreign_or_missing_engagement_to_404(
    monkeypatch: pytest.MonkeyPatch,
    reporting_reports_app: FastAPI,
) -> None:
    class FakeReportReadService:
        def __init__(self, db):
            self.db = db

        def get_current_report(self, **_kwargs):
            raise AssertionError("service must not run for inaccessible engagements")

    def fake_get_owned_engagement_or_404(*_args, **_kwargs):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Engagement not found",
        )

    monkeypatch.setattr(
        reports_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )
    monkeypatch.setattr(reports_routes, "ReportReadService", FakeReportReadService)

    response = TestClient(reporting_reports_app).get(
        "/api/reporting/engagements/999/reports/current?report_type=pentest"
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"
