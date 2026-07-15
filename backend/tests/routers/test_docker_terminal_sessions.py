"""Regression tests for terminal session route HTTP error semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.routers import docker_terminal_sessions as routes
from backend.services.tenant.context import TenantRequestContext


def _tenant_context(
    *,
    user_id: int,
    role: str = "owner",
    tenant_id: int = 701,
    is_default_tenant: bool = False,
) -> TenantRequestContext:
    return TenantRequestContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        membership_id=1,
        is_default_tenant=is_default_tenant,
    )


class _ScalarResult:
    """Small SQLAlchemy result stub exposing scalars().all()."""

    def __init__(self, values: list[int]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[int]:
        return self._values


class _TerminalSessionStub:
    """Route-level session stub with task metadata and serialization."""

    def __init__(self, *, session_id: str, task_id: int, user_id: int = 5) -> None:
        self.session_id = session_id
        self.task_id = task_id
        self.user_id = user_id

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "user_id": self.user_id,
        }


@pytest.mark.asyncio
async def test_create_terminal_session_reraises_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expected HTTP errors should not be converted into a generic 500."""

    async def _raise_expected(_: int, __: int, *, authorized_task):
        assert getattr(authorized_task, "id", None) == 77
        raise HTTPException(status_code=404, detail="missing")

    monkeypatch.setattr(routes, "get_tenant_task_or_404", lambda **_kwargs: SimpleNamespace(id=77, tenant_id=701))
    monkeypatch.setattr(routes.terminal_session_manager, "create_session", _raise_expected)

    with pytest.raises(HTTPException) as exc_info:
        await routes.create_terminal_session(
            task_id=77,
            current_user=SimpleNamespace(id=5),
            tenant_context=_tenant_context(user_id=5),
            db=object(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "missing"


@pytest.mark.asyncio
async def test_close_terminal_session_keeps_not_found_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route-authored 404 responses should survive route-level exception handling."""

    monkeypatch.setattr(routes.terminal_session_manager, "get_session", lambda _sid: None)

    with pytest.raises(HTTPException) as exc_info:
        await routes.close_terminal_session(
            session_id="missing-session",
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_close_terminal_session_uses_owned_task_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task control is authorized through the central user-owned task helper."""

    monkeypatch.setattr(
        routes.terminal_session_manager,
        "get_session",
        lambda _sid: SimpleNamespace(session_id="s-1", task_id=44, user_id=2),
    )
    monkeypatch.setattr(routes, "get_tenant_task_or_404", lambda **_kwargs: SimpleNamespace(id=44, tenant_id=701))

    async def _close(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(routes.terminal_session_manager, "close_session", _close)

    response = await routes.close_terminal_session(
        session_id="s-1",
        current_user=SimpleNamespace(id=2),
        tenant_context=_tenant_context(user_id=2, role="operator"),
        db=object(),
    )

    assert response == {"success": True}


@pytest.mark.asyncio
async def test_get_terminal_sessions_filters_to_active_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session listing must not leak sessions from another tenant."""

    sessions = [
        _TerminalSessionStub(session_id="tenant-a", task_id=101),
        _TerminalSessionStub(session_id="tenant-b", task_id=202),
    ]
    monkeypatch.setattr(routes.terminal_session_manager, "get_user_sessions", lambda _user_id: sessions)
    db = SimpleNamespace(execute=lambda _query: _ScalarResult([101]))

    response = await routes.get_terminal_sessions(
        current_user=SimpleNamespace(id=5),
        tenant_context=_tenant_context(user_id=5, tenant_id=701),
        db=db,
    )

    assert response["total"] == 1
    assert response["sessions"] == [{"session_id": "tenant-a", "task_id": 101, "user_id": 5}]


@pytest.mark.asyncio
async def test_get_terminal_sessions_denies_roles_without_task_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewer role should be blocked by centralized tenant action policy."""

    called = False

    def _get_user_sessions(_user_id: int) -> list[_TerminalSessionStub]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(routes.terminal_session_manager, "get_user_sessions", _get_user_sessions)

    with pytest.raises(HTTPException) as exc_info:
        await routes.get_terminal_sessions(
            current_user=SimpleNamespace(id=5),
            tenant_context=_tenant_context(user_id=5, role="viewer", tenant_id=701),
            db=SimpleNamespace(execute=lambda _query: _ScalarResult([])),
        )

    assert exc_info.value.status_code == 403
    assert called is False


@pytest.mark.asyncio
async def test_get_terminal_sessions_keeps_default_tenant_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-tenant installs should keep listing behavior for tenant-1 tasks."""

    sessions = [
        _TerminalSessionStub(session_id="default-1", task_id=1),
        _TerminalSessionStub(session_id="default-2", task_id=2),
    ]
    monkeypatch.setattr(routes.terminal_session_manager, "get_user_sessions", lambda _user_id: sessions)
    db = SimpleNamespace(execute=lambda _query: _ScalarResult([1, 2]))

    response = await routes.get_terminal_sessions(
        current_user=SimpleNamespace(id=5),
        tenant_context=_tenant_context(user_id=5, tenant_id=1, is_default_tenant=True),
        db=db,
    )

    assert response["total"] == 2
    assert response["sessions"] == [
        {"session_id": "default-1", "task_id": 1, "user_id": 5},
        {"session_id": "default-2", "task_id": 2, "user_id": 5},
    ]
