"""Regression coverage for `/ws` task ownership enforcement.

Responsibilities:
- Validate owner/non-owner behavior across task-scoped websocket channels.
- Verify deterministic `forbidden_task` responses and close semantics.
- Confirm multiplex `agent-multi` deny behavior keeps the socket open.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.services.tenant.authorization import ACTION_TASK_CONTROL
from backend.services.tenant.context import TenantRequestContext
from backend.services.websocket.reasoning_subscription import ws_reasoning_manager

TENANT_ID = 1


class _FakeWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        incoming_messages: list[str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.sent_payloads: list[dict] = []
        self.close_events: list[tuple[int | None, str | None]] = []
        self.accepted = False
        self.accepted_subprotocol: str | None = None
        self._incoming_messages = list(incoming_messages or [])

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.accepted_subprotocol = subprotocol

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def receive_text(self) -> str:
        if self._incoming_messages:
            return self._incoming_messages.pop(0)
        raise RuntimeError("socket_disconnected")

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def seeded_data(session_factory: sessionmaker[Session]) -> dict[str, int]:
    db = session_factory()
    try:
        tenant = Tenant(id=TENANT_ID, slug="default", name="Default Tenant")
        owner = User(username="owner", password="x", email="owner@example.com")
        other = User(username="other", password="x", email="other@example.com")
        db.add_all([tenant, owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        owner_membership = TenantMembership(tenant_id=TENANT_ID, user_id=owner.id, role="owner")
        other_membership = TenantMembership(tenant_id=TENANT_ID, user_id=other.id, role="owner")
        task = Task(user_id=owner.id, tenant_id=TENANT_ID, name="owned-task")
        db.add_all([owner_membership, other_membership, task])
        db.commit()
        db.refresh(owner_membership)
        db.refresh(other_membership)
        db.refresh(task)
        return {
            "owner_id": int(owner.id),
            "other_id": int(other.id),
            "task_id": int(task.id),
            "owner_membership_id": int(owner_membership.id),
            "other_membership_id": int(other_membership.id),
        }
    finally:
        db.close()


def _find_payload(payloads: list[dict], **expect: object) -> dict | None:
    for payload in payloads:
        if all(payload.get(k) == v for k, v in expect.items()):
            return payload
    return None


def _get_main_module():
    return importlib.import_module("backend.main")


def _bind_tenant_context(websocket: _FakeWebSocket, *, user_id: int, membership_id: int, role: str = "owner") -> None:
    websocket._tenant_context = TenantRequestContext(
        tenant_id=TENANT_ID,
        user_id=int(user_id),
        role=role,
        membership_id=int(membership_id),
        is_default_tenant=True,
        source="test",
    )


def _bearer_headers(token: str = "valid-token") -> dict[str, str]:
    return {"sec-websocket-protocol": f"Bearer.{token}"}


async def _authorized_ws_context(websocket, *, user_id: int, membership_id: int):  # noqa: ANN001
    await websocket.accept(subprotocol="Bearer.valid-token")
    context = TenantRequestContext(
        tenant_id=TENANT_ID,
        user_id=int(user_id),
        role="owner",
        membership_id=int(membership_id),
        is_default_tenant=True,
        source="test",
    )
    websocket._tenant_context = context
    return SimpleNamespace(
        user_data={"user_id": int(user_id), "sub": "owner"},
        user_id=int(user_id),
        tenant_context=context,
    )


@pytest.mark.asyncio
async def test_agent_multi_owner_can_subscribe(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr("backend.services.websocket.gateway.SessionLocal", session_factory)

    subscribe_calls: list[int] = []
    cleanup_calls: list[int] = []
    active_tasks: set[int] = set()

    async def _fake_subscribe(websocket, task_id: int, last_sequence: int = 0) -> str:  # noqa: ANN001
        active_tasks.add(task_id)
        subscribe_calls.append(task_id)
        return "sub-multi-owner"

    async def _fake_get_subscription_count(websocket) -> int:  # noqa: ANN001
        return len(active_tasks)

    async def _fake_has_task_subscription(websocket, task_id: int) -> bool:  # noqa: ANN001
        return task_id in active_tasks

    async def _fake_get_subscribed_tasks(websocket) -> list[int]:  # noqa: ANN001
        return sorted(active_tasks)

    async def _fake_unsubscribe_all(websocket) -> None:  # noqa: ANN001
        cleanup_calls.append(id(websocket))
        active_tasks.clear()

    monkeypatch.setattr(ws_reasoning_manager, "subscribe", _fake_subscribe)
    monkeypatch.setattr(ws_reasoning_manager, "get_subscription_count_async", _fake_get_subscription_count)
    monkeypatch.setattr(ws_reasoning_manager, "has_task_subscription", _fake_has_task_subscription)
    monkeypatch.setattr(ws_reasoning_manager, "get_subscribed_tasks", _fake_get_subscribed_tasks)
    monkeypatch.setattr(ws_reasoning_manager, "unsubscribe_all", _fake_unsubscribe_all)

    ws = _FakeWebSocket(
        incoming_messages=[
            json.dumps({"action": "subscribe", "channel": "agent", "taskId": seeded_data["task_id"]}),
        ]
    )
    _bind_tenant_context(ws, user_id=seeded_data["owner_id"], membership_id=seeded_data["owner_membership_id"])
    await main_module.handle_agent_multi_websocket(ws, seeded_data["owner_id"])

    assert _find_payload(ws.sent_payloads, type="subscribed", taskId=seeded_data["task_id"]) is not None
    assert subscribe_calls == [seeded_data["task_id"]]
    assert cleanup_calls == [id(ws)]
    assert ws.close_events == []


@pytest.mark.asyncio
async def test_agent_multi_unsubscribe_routes_through_manager_task_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr("backend.services.websocket.gateway.SessionLocal", session_factory)

    active_tasks: set[int] = set()
    unsubscribe_task_calls: list[int] = []
    cleanup_calls: list[int] = []

    async def _fake_subscribe(websocket, task_id: int, last_sequence: int = 0) -> str:  # noqa: ANN001
        active_tasks.add(task_id)
        return "sub-multi-owner"

    async def _fake_get_subscription_count(websocket) -> int:  # noqa: ANN001
        return len(active_tasks)

    async def _fake_has_task_subscription(websocket, task_id: int) -> bool:  # noqa: ANN001
        return task_id in active_tasks

    async def _fake_unsubscribe_task(websocket, task_id: int) -> int:  # noqa: ANN001
        unsubscribe_task_calls.append(task_id)
        removed = 1 if task_id in active_tasks else 0
        active_tasks.discard(task_id)
        return removed

    async def _fake_get_subscribed_tasks(websocket) -> list[int]:  # noqa: ANN001
        return sorted(active_tasks)

    async def _fake_unsubscribe_all(websocket) -> None:  # noqa: ANN001
        cleanup_calls.append(id(websocket))
        active_tasks.clear()

    monkeypatch.setattr(ws_reasoning_manager, "subscribe", _fake_subscribe)
    monkeypatch.setattr(ws_reasoning_manager, "get_subscription_count_async", _fake_get_subscription_count)
    monkeypatch.setattr(ws_reasoning_manager, "has_task_subscription", _fake_has_task_subscription)
    monkeypatch.setattr(ws_reasoning_manager, "unsubscribe_task", _fake_unsubscribe_task)
    monkeypatch.setattr(ws_reasoning_manager, "get_subscribed_tasks", _fake_get_subscribed_tasks)
    monkeypatch.setattr(ws_reasoning_manager, "unsubscribe_all", _fake_unsubscribe_all)

    ws = _FakeWebSocket(
        incoming_messages=[
            json.dumps({"action": "subscribe", "channel": "agent", "taskId": seeded_data["task_id"]}),
            json.dumps({"action": "unsubscribe", "channel": "agent", "taskId": seeded_data["task_id"]}),
        ]
    )
    _bind_tenant_context(ws, user_id=seeded_data["owner_id"], membership_id=seeded_data["owner_membership_id"])
    await main_module.handle_agent_multi_websocket(ws, seeded_data["owner_id"])

    assert _find_payload(ws.sent_payloads, type="subscribed", taskId=seeded_data["task_id"]) is not None
    assert _find_payload(ws.sent_payloads, type="unsubscribed", taskId=seeded_data["task_id"]) is not None
    assert unsubscribe_task_calls == [seeded_data["task_id"]]
    assert cleanup_calls == [id(ws)]


@pytest.mark.asyncio
async def test_agent_multi_non_owner_gets_forbidden_without_close(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr("backend.services.websocket.gateway.SessionLocal", session_factory)

    ws = _FakeWebSocket(
        incoming_messages=[
            json.dumps({"action": "subscribe", "channel": "agent", "taskId": seeded_data["task_id"]}),
        ]
    )
    _bind_tenant_context(ws, user_id=seeded_data["other_id"], membership_id=seeded_data["other_membership_id"])
    await main_module.handle_agent_multi_websocket(ws, seeded_data["other_id"])

    assert _find_payload(ws.sent_payloads, type="error", message="forbidden_task", taskId=seeded_data["task_id"])
    assert ws.close_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_docker_websocket",
        "handle_metrics_websocket",
        "handle_vpn_status_websocket",
    ],
)
async def test_single_task_channels_deny_non_owner(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    seeded_data: dict[str, int],
    handler_name: str,
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr("backend.services.websocket.gateway.SessionLocal", session_factory)

    handler = getattr(main_module, handler_name)
    ws = _FakeWebSocket()
    _bind_tenant_context(ws, user_id=seeded_data["other_id"], membership_id=seeded_data["other_membership_id"])
    if handler_name in {"handle_vpn_status_websocket", "handle_metrics_websocket"}:
        await handler(ws, seeded_data["task_id"], seeded_data["other_id"])
    else:
        await handler(ws, seeded_data["task_id"], {"sub": "other"}, seeded_data["other_id"])

    assert _find_payload(ws.sent_payloads, type="error", message="forbidden_task", taskId=seeded_data["task_id"])
    assert ws.close_events and ws.close_events[-1] == (1008, "Forbidden")


@pytest.mark.asyncio
async def test_terminal_channel_uses_task_control_authorization_action(
    monkeypatch: pytest.MonkeyPatch,
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()
    observed: dict[str, object] = {}

    async def _fake_ownership_enforcer(_websocket, **kwargs):  # noqa: ANN001
        observed.update(kwargs)
        return False

    monkeypatch.setattr(main_module, "enforce_ws_task_ownership", _fake_ownership_enforcer)

    ws = _FakeWebSocket()
    await main_module.handle_terminal_websocket(
        ws,
        seeded_data["task_id"],
        {"sub": "other"},
        seeded_data["other_id"],
    )

    assert observed["action"] == ACTION_TASK_CONTROL


@pytest.mark.asyncio
async def test_websocket_endpoint_rejects_unresolvable_identity(
    monkeypatch: pytest.MonkeyPatch,
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()

    async def _unresolvable_identity(_token: str | None):  # noqa: ANN001
        return {"foo": "bar"}, None

    monkeypatch.setattr(main_module, "authenticate_ws", _unresolvable_identity)

    async def _fail_handler(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("agent-multi handler must not be called when identity is unresolved")

    monkeypatch.setattr(main_module, "handle_agent_multi_websocket", _fail_handler)

    ws = _FakeWebSocket(
        headers=_bearer_headers(),
        query_params={
            "type": "agent-multi",
        }
    )
    await main_module.websocket_endpoint(ws)

    assert _find_payload(ws.sent_payloads, type="error", message="Unauthorized websocket identity")
    assert ws.close_events and ws.close_events[-1] == (1008, "Unauthorized")


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_channel", ["browser", "agent"])
async def test_websocket_endpoint_rejects_removed_channels(
    monkeypatch: pytest.MonkeyPatch,
    seeded_data: dict[str, int],
    removed_channel: str,
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr(
        main_module,
        "authorize_ws_connection",
        lambda websocket, **_kwargs: _authorized_ws_context(
            websocket,
            user_id=seeded_data["owner_id"],
            membership_id=seeded_data["owner_membership_id"],
        ),
    )

    ws = _FakeWebSocket(
        headers=_bearer_headers(),
        query_params={
            "type": removed_channel,
            "taskId": str(seeded_data["task_id"]),
        }
    )
    await main_module.websocket_endpoint(ws)

    assert _find_payload(ws.sent_payloads, type="error", message="Invalid connection type or missing taskId")


@pytest.mark.asyncio
async def test_websocket_endpoint_routes_metrics_channel(
    monkeypatch: pytest.MonkeyPatch,
    seeded_data: dict[str, int],
) -> None:
    main_module = _get_main_module()
    monkeypatch.setattr(
        main_module,
        "authorize_ws_connection",
        lambda websocket, **_kwargs: _authorized_ws_context(
            websocket,
            user_id=seeded_data["owner_id"],
            membership_id=seeded_data["owner_membership_id"],
        ),
    )

    called: list[tuple[int, int]] = []

    async def _fake_metrics_handler(websocket, task_id: int, user_id: int) -> None:  # noqa: ANN001
        called.append((task_id, user_id))

    monkeypatch.setattr(main_module, "handle_metrics_websocket", _fake_metrics_handler)

    ws = _FakeWebSocket(
        headers=_bearer_headers(),
        query_params={
            "type": "metrics",
            "taskId": str(seeded_data["task_id"]),
        }
    )
    await main_module.websocket_endpoint(ws)

    assert called == [(seeded_data["task_id"], seeded_data["owner_id"])]


@pytest.mark.asyncio
async def test_websocket_endpoint_rejects_expired_token_with_deterministic_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _get_main_module()
    async def _expired_token(_token: str | None):  # noqa: ANN001
        return None, "token_expired"

    monkeypatch.setattr(main_module, "authenticate_ws", _expired_token)

    async def _fail_handler(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("agent-multi handler must not be called when token is expired")

    monkeypatch.setattr(main_module, "handle_agent_multi_websocket", _fail_handler)

    ws = _FakeWebSocket(
        headers=_bearer_headers("expired-token"),
        query_params={
            "type": "agent-multi",
        }
    )
    await main_module.websocket_endpoint(ws)

    assert _find_payload(
        ws.sent_payloads,
        type="error",
        message="Invalid authentication token",
        code="token_expired",
    )
    assert ws.close_events and ws.close_events[-1] == (1008, "Unauthorized")
