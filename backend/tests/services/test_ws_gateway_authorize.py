"""Tests for shared websocket gateway authorization orchestration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from backend.services.websocket import gateway as ws_gateway
from backend.services.tenant.authorization import (
    ACTION_STREAM_REPLAY,
    ACTION_STREAM_SUBSCRIBE,
    ACTION_TASK_CONTROL,
)
from backend.services.tenant.context import TenantRequestContext


class _FakeWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] | None = None
        self.sent_payloads: list[dict[str, Any]] = []
        self.close_events: list[tuple[int | None, str | None]] = []
        self.state = SimpleNamespace()

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.accepted_subprotocol = subprotocol
        self.accepted_headers = headers

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


@pytest.mark.asyncio
async def test_extract_ws_token_ignores_query_token_fallback() -> None:
    websocket = _FakeWebSocket(query_params={"token": "legacy-query-token"})

    token, selected_protocol = await ws_gateway.extract_ws_token(websocket)

    assert token is None
    assert selected_protocol is None


@pytest.mark.asyncio
async def test_authenticate_ws_rejects_inactive_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(ws_gateway, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(ws_gateway, "clear_rls_session_context", lambda _db: None)
    monkeypatch.setattr(
        ws_gateway,
        "verify_token_with_error",
        lambda _token: ({"sub": "inactive", "user_id": 44}, None),
    )

    def _inactive_user(_db, _payload):
        raise HTTPException(status_code=403, detail="User account is inactive")

    monkeypatch.setattr(ws_gateway, "resolve_user_from_token_payload", _inactive_user)

    payload, error_code = await ws_gateway.authenticate_ws("jwt-token")

    assert payload is None
    assert error_code == "inactive_user"


@pytest.mark.asyncio
async def test_authorize_ws_connection_accepts_and_returns_auth_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket(
        headers={"sec-websocket-protocol": "Bearer.jwt-token"},
    )
    expected_headers = [(b"deprecation", b"true")]

    async def _authenticate(_token: str | None):
        return {"sub": "owner", "user_id": 17}, None

    def _resolve_tenant_context(*_args, **_kwargs):
        return TenantRequestContext(
            tenant_id=101,
            user_id=17,
            role="owner",
            membership_id=501,
            is_default_tenant=False,
            source="query",
        )

    monkeypatch.setattr(ws_gateway, "authenticate_ws", _authenticate)
    monkeypatch.setattr(ws_gateway, "resolve_ws_user_id", lambda _user_data: 17)
    monkeypatch.setattr(ws_gateway, "resolve_ws_tenant_context", _resolve_tenant_context)

    context = await ws_gateway.authorize_ws_connection(
        websocket,
        accept_headers=expected_headers,
    )

    assert context is not None
    assert context.user_id == 17
    assert context.token == "jwt-token"
    assert context.tenant_context.tenant_id == 101
    assert websocket.accepted_subprotocol == "Bearer.jwt-token"
    assert websocket.accepted_headers == expected_headers


@pytest.mark.asyncio
async def test_authorize_ws_connection_emits_structured_missing_token_error() -> None:
    websocket = _FakeWebSocket()

    context = await ws_gateway.authorize_ws_connection(websocket)

    assert context is None
    assert websocket.sent_payloads == [
        {"type": "error", "message": "Authentication token required", "code": "missing_token"}
    ]
    assert websocket.close_events == [(1008, "Unauthorized")]


def test_resolve_ws_tenant_context_bootstraps_user_lookup_and_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    fake_db = SimpleNamespace(close=lambda: None)
    expected_context = TenantRequestContext(
        tenant_id=201,
        user_id=17,
        role="owner",
        membership_id=9,
        is_default_tenant=False,
        source="single_membership",
    )
    events: list[tuple[str, int, int]] = []

    class _FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int, **_kwargs):
            return expected_context

    monkeypatch.setattr(ws_gateway, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(ws_gateway, "TenantContextService", _FakeTenantContextService)
    monkeypatch.setattr(
        ws_gateway,
        "set_user_lookup_rls_context",
        lambda _db, *, user_id, actor_type: events.append(("lookup", int(user_id), 0)),
    )
    monkeypatch.setattr(
        ws_gateway,
        "set_tenant_rls_context",
        lambda _db, *, tenant_id, user_id, actor_type: events.append(
            ("tenant", int(user_id), int(tenant_id))
        ),
    )
    monkeypatch.setattr(ws_gateway, "clear_rls_session_context", lambda _db: None)

    resolved = ws_gateway.resolve_ws_tenant_context(
        websocket,
        user_id=17,
        user_data={"sub": "owner"},
    )

    assert resolved == expected_context
    assert events == [("lookup", 17, 0), ("tenant", 17, 201)]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ACTION_STREAM_SUBSCRIBE, ACTION_STREAM_REPLAY])
async def test_enforce_ws_task_ownership_checks_user_owned_task_when_action_permitted(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    websocket = _FakeWebSocket()
    websocket.state.tenant_context = TenantRequestContext(
        tenant_id=9,
        user_id=7,
        role="admin",
        membership_id=99,
        is_default_tenant=False,
        source="query",
    )
    observed: dict[str, int] = {}

    def _task_in_tenant(*, task_id: int, tenant_id: int | None = None, user_id: int | None = None) -> bool:
        observed["task_id"] = task_id
        observed["tenant_id"] = int(tenant_id or -1)
        observed["user_id"] = int(user_id or -1)
        return True

    monkeypatch.setattr(ws_gateway, "is_ws_task_in_tenant", _task_in_tenant)

    allowed = await ws_gateway.enforce_ws_task_ownership(
        websocket,
        connection_type="terminal",
        task_id=42,
        user_id=7,
        close_on_forbidden=True,
        action=action,
    )

    assert allowed is True
    assert observed == {"task_id": 42, "tenant_id": 9, "user_id": 7}
    assert websocket.sent_payloads == []
    assert websocket.close_events == []


@pytest.mark.asyncio
async def test_enforce_ws_task_ownership_denies_non_owner_with_forbidden_task_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    websocket.state.tenant_context = TenantRequestContext(
        tenant_id=9,
        user_id=7,
        role="owner",
        membership_id=99,
        is_default_tenant=False,
        source="query",
    )
    observed: dict[str, int] = {}

    def _missing_in_tenant(*, task_id: int, tenant_id: int | None = None, user_id: int | None = None) -> bool:
        observed["task_id"] = task_id
        observed["tenant_id"] = int(tenant_id or -1)
        observed["user_id"] = int(user_id or -1)
        return False

    monkeypatch.setattr(ws_gateway, "is_ws_task_in_tenant", _missing_in_tenant)

    allowed = await ws_gateway.enforce_ws_task_ownership(
        websocket,
        connection_type="terminal",
        task_id=42,
        user_id=7,
        close_on_forbidden=True,
    )

    assert allowed is False
    assert observed == {"task_id": 42, "tenant_id": 9, "user_id": 7}
    assert websocket.sent_payloads == [{"type": "error", "message": "forbidden_task", "taskId": 42}]
    assert websocket.close_events == [(1008, "Forbidden")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("admin", True),
        ("operator", True),
        ("viewer", False),
    ],
)
async def test_enforce_ws_task_ownership_task_control_role_matrix(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    allowed: bool,
) -> None:
    websocket = _FakeWebSocket()
    websocket.state.tenant_context = TenantRequestContext(
        tenant_id=9,
        user_id=7,
        role=role,
        membership_id=99,
        is_default_tenant=False,
        source="query",
    )
    monkeypatch.setattr(ws_gateway, "is_ws_task_in_tenant", lambda **_kwargs: True)

    result = await ws_gateway.enforce_ws_task_ownership(
        websocket,
        connection_type="terminal",
        task_id=42,
        user_id=7,
        close_on_forbidden=True,
        action=ACTION_TASK_CONTROL,
    )

    assert result is allowed
    if allowed:
        assert websocket.sent_payloads == []
        assert websocket.close_events == []
    else:
        assert websocket.sent_payloads == [
            {
                "type": "error",
                "message": "Tenant policy denied websocket stream action.",
                "code": "stream_action_forbidden",
            }
        ]
        assert websocket.close_events == [(1008, "Policy violation")]


@pytest.mark.asyncio
async def test_authorize_ws_connection_closes_with_policy_violation_on_tenant_context_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket(
        headers={"sec-websocket-protocol": "Bearer.jwt-token"},
    )

    async def _authenticate(_token: str | None):
        return {"sub": "owner", "user_id": 17}, None

    def _raise_tenant_error(*_args, **_kwargs):
        raise ws_gateway.WSTenantContextError(
            code="explicit_tenant_required",
            message="Explicit tenant selection is required.",
        )

    monkeypatch.setattr(ws_gateway, "authenticate_ws", _authenticate)
    monkeypatch.setattr(ws_gateway, "resolve_ws_user_id", lambda _user_data: 17)
    monkeypatch.setattr(ws_gateway, "resolve_ws_tenant_context", _raise_tenant_error)

    context = await ws_gateway.authorize_ws_connection(websocket)

    assert context is None
    assert websocket.sent_payloads == [
        {
            "type": "error",
            "message": "Explicit tenant selection is required.",
            "code": "explicit_tenant_required",
        }
    ]
    assert websocket.close_events == [(1008, "Policy violation")]
