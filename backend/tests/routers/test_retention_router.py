"""Router tests for tenant-scoped retention dry-run and apply endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import retention as routes
from backend.services.retention.contracts import (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_TENANT,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunRequest,
    RetentionRunResult,
)
from backend.services.tenant.authorization import ACTION_TENANT_SETTINGS_MANAGE


def _client(
    *,
    tenant_id: int = 701,
    role: str = "owner",
) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=tenant_id, user_id=11, role=role)

    def fake_db():
        yield SimpleNamespace(name="db")

    app.dependency_overrides[routes.get_current_user] = fake_current_user
    app.dependency_overrides[routes.get_tenant_request_context] = fake_tenant_context
    app.dependency_overrides[routes.get_db] = fake_db
    return TestClient(app)


def test_dry_run_defaults_to_active_tenant_and_returns_safe_counts_only(
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, db):
            calls["db"] = db

        def run(self, request: RetentionRunRequest) -> RetentionRunResult:
            calls["request"] = request
            return _retention_result(mode=request.mode, tenant_id=request.tenant_id)

    monkeypatch.setattr(routes, "RetentionOrchestrator", FakeOrchestrator)

    response = _client(tenant_id=701).post("/api/retention/dry-run")

    assert response.status_code == 200
    request = calls["request"]
    assert request.mode == RETENTION_RUN_MODE_DRY_RUN
    assert request.scope == RETENTION_SCOPE_TENANT
    assert request.tenant_id == 701

    payload = response.json()
    assert payload["mode"] == RETENTION_RUN_MODE_DRY_RUN
    assert payload["tenant_id"] == 701
    assert payload["counts"]["candidate_count"] == 2
    assert payload["executor_results"][0]["counts"]["scanned_count"] == 3
    assert "decisions" not in payload["executor_results"][0]
    assert "secret-token-resource" not in response.text


def test_apply_requires_explicit_confirmation(monkeypatch) -> None:
    called = False

    class FakeOrchestrator:
        def __init__(self, db):
            pass

        def run(self, request: RetentionRunRequest) -> RetentionRunResult:
            nonlocal called
            called = True
            return _retention_result(mode=request.mode, tenant_id=request.tenant_id)

    monkeypatch.setattr(routes, "RetentionOrchestrator", FakeOrchestrator)

    response = _client().post("/api/retention/apply", json={"confirm": False})

    assert response.status_code == 400
    assert response.json() == {"detail": "Retention apply requires confirm=true."}
    assert called is False


def test_apply_confirmed_runs_for_active_tenant_only(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, db):
            pass

        def run(self, request: RetentionRunRequest) -> RetentionRunResult:
            calls["request"] = request
            return _retention_result(mode=request.mode, tenant_id=request.tenant_id)

    monkeypatch.setattr(routes, "RetentionOrchestrator", FakeOrchestrator)

    response = _client(tenant_id=802).post(
        "/api/retention/apply",
        json={
            "confirm": True,
            "retention_classes": [RETENTION_CLASS_OPERATIONAL_EPHEMERAL],
            "limit_per_tenant": 5,
        },
    )

    assert response.status_code == 200
    request = calls["request"]
    assert request.mode == RETENTION_RUN_MODE_APPLY
    assert request.scope == RETENTION_SCOPE_TENANT
    assert request.tenant_id == 802
    assert request.retention_classes == (RETENTION_CLASS_OPERATIONAL_EPHEMERAL,)
    assert request.limit_per_tenant == 5


def test_retention_routes_reuse_tenant_settings_manage_authorization(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeOrchestrator:
        def __init__(self, db):
            pass

        def run(self, request: RetentionRunRequest) -> RetentionRunResult:
            return _retention_result(mode=request.mode, tenant_id=request.tenant_id)

    def fake_enforce_tenant_action(*, tenant_context, action, detail=None):
        calls.append(action)

    monkeypatch.setattr(routes, "RetentionOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(routes, "enforce_tenant_action", fake_enforce_tenant_action)

    response = _client().post("/api/retention/dry-run", json={})

    assert response.status_code == 200
    assert calls == [ACTION_TENANT_SETTINGS_MANAGE]


def test_operator_cannot_run_retention() -> None:
    response = _client(role="operator").post("/api/retention/dry-run", json={})

    assert response.status_code == 403


def _retention_result(*, mode: str, tenant_id: int | None) -> RetentionRunResult:
    assert tenant_id is not None
    executor_result = RetentionExecutorResult(
        executor_name="runner_control.retention",
        retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        mode=mode,
        tenant_id=tenant_id,
        counts=RetentionBatchCounts(
            scanned_count=3,
            candidate_count=2,
            protected_count=1,
            applied_count=1 if mode == RETENTION_RUN_MODE_APPLY else 0,
            batch_count=2,
            batch_limit=5,
        ),
        reason_counts={"expired": 2},
        decisions=(
            RetentionDecision(
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                outcome=RETENTION_DECISION_CANDIDATE,
                reason_code="expired",
                resource_id="secret-token-resource",
                count=2,
            ),
        ),
    )
    return RetentionRunResult(
        mode=mode,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=tenant_id,
        results=(executor_result,),
        succeeded=True,
    )
