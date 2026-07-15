"""Router contract tests for reporting LLM selection endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.routers import llm as llm_routes
from backend.services.llm_provider import ProviderConfigurationError
from backend.services.llm_provider.types import LLMSelectionStatus


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, _row) -> None:
        pass


@pytest.fixture
def llm_reporting_selection_app(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(llm_routes.router)
    db = _FakeDb()

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_db():
        yield db

    app.dependency_overrides[llm_routes.get_current_user] = fake_current_user
    app.dependency_overrides[llm_routes.get_db] = fake_db
    yield app, db
    app.dependency_overrides.clear()


def test_reporting_selection_get_returns_unset_status(
    monkeypatch: pytest.MonkeyPatch,
    llm_reporting_selection_app,
) -> None:
    app, db = llm_reporting_selection_app

    class FakeReportingSelectionService:
        def __init__(self, db_arg):
            self.db = db_arg

        def get_selection_read(self, user_id: int):
            assert user_id == 11
            return SimpleNamespace(
                selection=None,
                status=LLMSelectionStatus(
                    status="unset",
                    selectable=False,
                    runnable=False,
                    reason="Reporting model is not configured.",
                ),
            )

    monkeypatch.setattr(
        llm_routes,
        "ReportingLLMSelectionService",
        FakeReportingSelectionService,
    )

    response = TestClient(app).get("/api/llm/reporting-selection")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "selection_status": {
            "status": "unset",
            "selectable": False,
            "runnable": False,
            "reason": "Reporting model is not configured.",
        },
    }
    assert db.commits == 1


def test_reporting_selection_put_validates_and_returns_status(
    monkeypatch: pytest.MonkeyPatch,
    llm_reporting_selection_app,
) -> None:
    app, db = llm_reporting_selection_app
    calls: list[dict] = []

    class FakeReportingSelectionService:
        def __init__(self, db_arg):
            self.db = db_arg

        def set_selection(self, **kwargs):
            calls.append(dict(kwargs))
            return SimpleNamespace(
                provider=kwargs["provider"],
                model=kwargs["model"],
                reasoning_effort=kwargs["reasoning_effort"],
            )

        def get_selection_read(self, user_id: int):
            assert user_id == 11
            return SimpleNamespace(
                selection=object(),
                status=LLMSelectionStatus(
                    status="runnable",
                    selectable=True,
                    runnable=True,
                    reason=None,
                ),
            )

    monkeypatch.setattr(
        llm_routes,
        "ReportingLLMSelectionService",
        FakeReportingSelectionService,
    )

    response = TestClient(app).put(
        "/api/llm/reporting-selection",
        json={
            "provider": "anthropic",
            "model": "claude-haiku-report",
            "reasoning_effort": None,
        },
    )

    assert response.status_code == 200, response.text
    assert calls == [
        {
            "user_id": 11,
            "provider": "anthropic",
            "model": "claude-haiku-report",
            "reasoning_effort": None,
        }
    ]
    assert response.json()["selection_status"]["runnable"] is True
    assert db.commits == 1


def test_reporting_selection_put_maps_provider_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    llm_reporting_selection_app,
) -> None:
    app, db = llm_reporting_selection_app

    class FakeReportingSelectionService:
        def __init__(self, db_arg):
            self.db = db_arg

        def set_selection(self, **_kwargs):
            raise ProviderConfigurationError("model is not available")

    monkeypatch.setattr(
        llm_routes,
        "ReportingLLMSelectionService",
        FakeReportingSelectionService,
    )

    response = TestClient(app).put(
        "/api/llm/reporting-selection",
        json={"provider": "openai", "model": "missing-model"},
    )

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "model is not available"}
    assert db.rollbacks == 1
