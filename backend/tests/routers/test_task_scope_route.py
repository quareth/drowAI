"""Regression tests for provider-mediated task scope route behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.routers.tasks import scope as scope_router


class _FakeRuntimeOperationService:
    scope_markdown = None

    def __init__(self, _db):
        pass

    async def read_scope_markdown(self, *, task, user_id):
        _ = task, user_id
        if isinstance(self.scope_markdown, Exception):
            raise self.scope_markdown
        return self.scope_markdown


class _ParsedScope:
    def to_dict(self):
        return {"targets": ["10.0.0.1"]}


class _FakeScopeParser:
    def parse_scope_file(self, _path):
        return _ParsedScope()

    def parse_markdown_content(self, _content):
        return _ParsedScope()

    def get_validation_errors(self):
        return []

    def get_warnings(self):
        return []

    def has_errors(self):
        return False


@pytest.mark.asyncio
async def test_task_scope_route_parses_provider_resolved_scope_file(monkeypatch):
    task = SimpleNamespace(id=5, name="scope-task", scope="durable")
    _FakeRuntimeOperationService.scope_markdown = "10.0.0.1"

    monkeypatch.setattr(scope_router, "get_tenant_task_or_404", lambda **_kwargs: task)
    monkeypatch.setattr(scope_router, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(scope_router, "TaskWorkspaceQueryService", _FakeRuntimeOperationService)
    monkeypatch.setattr(scope_router, "ScopeParser", _FakeScopeParser)

    response = await scope_router.get_task_scope(
        task_id=5,
        current_user=SimpleNamespace(id=7),
        tenant_context=SimpleNamespace(tenant_id=701, user_id=7, role="owner"),
        db=object(),
    )

    assert response["success"] is True
    assert response["parsed_scope"] == {"targets": ["10.0.0.1"]}


@pytest.mark.asyncio
async def test_task_scope_route_falls_back_to_durable_scope_when_file_missing(monkeypatch):
    task = SimpleNamespace(id=5, name="scope-task", scope="durable")
    _FakeRuntimeOperationService.scope_markdown = None

    monkeypatch.setattr(scope_router, "get_tenant_task_or_404", lambda **_kwargs: task)
    monkeypatch.setattr(scope_router, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(scope_router, "TaskWorkspaceQueryService", _FakeRuntimeOperationService)

    response = await scope_router.get_task_scope(
        task_id=5,
        current_user=SimpleNamespace(id=7),
        tenant_context=SimpleNamespace(tenant_id=701, user_id=7, role="owner"),
        db=object(),
    )

    assert response["success"] is False
    assert response["error"] == "Scope file not found"
    assert response["raw_scope"] == "durable"
