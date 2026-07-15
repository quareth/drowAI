"""Phase 3 tenant-authorization matrix tests for remaining chat/runtime surfaces.

Responsibilities:
- Cover LLM conversation, reasoning replay/stream, and interrupt route families.
- Verify same-tenant allow/deny, foreign-tenant no-leakage, and default-tenant parity.
- Preserve compatible response-shape contracts for migrated Phase 3 routes.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers import agent_reasoning as reasoning_routes
from backend.routers import llm as llm_routes
from backend.routers.tasks import interrupt_inbox as interrupt_inbox_routes
from backend.routers.tasks import interrupts as interrupts_routes
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.authorization import (
    ACTION_CHAT_READ,
    ACTION_CHAT_RETRY,
    ACTION_CHAT_WRITE,
    ACTION_STREAM_REPLAY,
    ACTION_STREAM_SUBSCRIBE,
)


class _FakeLLMQuery:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows
        self._filters: list[tuple[str, object]] = []

    def filter(self, *_args):
        for expr in _args:
            left = getattr(expr, "left", None)
            right = getattr(expr, "right", None)
            key = getattr(left, "key", None)
            value = getattr(right, "value", None)
            if key is not None:
                self._filters.append((str(key), value))
        return self

    def order_by(self, *_args):
        return self

    def first(self):
        for row in self._rows:
            if all(getattr(row, key, None) == value for key, value in self._filters):
                return row
        return None


class _FakeDB:
    def __init__(self) -> None:
        self._rows = [
            SimpleNamespace(
                id=1001,
                task_id=11,
                tenant_id=701,
                user_id=1,
                provider="openai",
                model="gpt-5.2",
                conversation_id="seeded-conv-11",
                title="seeded",
                status="active",
                is_active=True,
            )
        ]

    def query(self, _model):
        return _FakeLLMQuery(self._rows)

    def commit(self):
        return None

    def rollback(self):
        return None


class _FakeLLMProviderSelectionService:
    def __init__(self, _db):
        pass

    def get_openai_model_compat(self, _user_id: int):
        return "gpt-4o-mini"


class _FakeReasoningHistoryService:
    def __init__(self, _db):
        pass

    def get_history(self, task_id: int, *, after, before, limit, order):
        return {
            "task_id": task_id,
            "events": [],
            "after": after,
            "before": before,
            "limit": limit,
            "order": order,
        }

    def get_replay_history(self, task_id: int, *, after, limit):
        return {
            "task_id": task_id,
            "items": [],
            "nextAfter": after,
            "hasMore": False,
            "limit": limit,
        }


class _FakeReasoningSSEService:
    async def generate(self, task_id: int, *, after: int, persisted_list_after):
        _ = after, persisted_list_after
        yield f"data: {{\"task_id\": {task_id}, \"event\": \"delta\"}}\n\n"


class _FakeTaskInterruptService:
    def __init__(self, _db):
        pass

    @staticmethod
    def _assert_tenant_task_match(task_id: int, tenant_id: int) -> None:
        task_tenants = {11: 701, 22: 702, 31: 1}
        if task_tenants.get(int(task_id)) != int(tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    async def get_task_interrupt(self, *, task_id: int, user_id: int, interrupt_service, tenant_id: int):
        _ = user_id, interrupt_service
        self._assert_tenant_task_match(task_id, tenant_id)
        return {"has_interrupt": False, "task_id": int(task_id)}

    async def resume_graph_execution(
        self,
        *,
        task_id: int,
        user_id: int,
        interrupt_id: str,
        graph_name,
        response_payload,
        create_task_fn,
        run_resume_generation,
        approval_received_at,
        tenant_id: int,
    ):
        _ = (
            user_id,
            interrupt_id,
            graph_name,
            response_payload,
            create_task_fn,
            run_resume_generation,
            approval_received_at,
        )
        self._assert_tenant_task_match(task_id, tenant_id)
        return {"accepted": True, "task_id": int(task_id)}

    async def retry_graph_execution(
        self,
        *,
        task_id: int,
        user_id: int,
        turn_id: str,
        retry_mode: str,
        graph_name,
        create_task_fn,
        run_checkpoint_retry_generation,
        tenant_id: int,
    ):
        _ = (
            user_id,
            turn_id,
            retry_mode,
            graph_name,
            create_task_fn,
            run_checkpoint_retry_generation,
        )
        self._assert_tenant_task_match(task_id, tenant_id)
        return {"accepted": True, "task_id": int(task_id), "retry_mode": retry_mode}

    def list_pending_interrupts_for_user(self, _user_id: int, *, tenant_id: int):
        if int(tenant_id) == 701:
            return [
                {
                    "task_id": 11,
                    "interrupt_id": "int-11",
                    "interrupt_type": "tool_approval",
                    "graph_name": "main",
                }
            ]
        return []


class _FakeTaskGraphRetryService:
    def __init__(self, _db):
        pass

    async def retry_graph_execution(
        self,
        *,
        task_id: int,
        user_id: int,
        turn_id: str,
        retry_mode: str,
        graph_name,
        create_task_fn,
        run_checkpoint_retry_generation,
        tenant_id: int,
    ):
        svc = _FakeTaskInterruptService(None)
        return await svc.retry_graph_execution(
            task_id=task_id,
            user_id=user_id,
            turn_id=turn_id,
            retry_mode=retry_mode,
            graph_name=graph_name,
            create_task_fn=create_task_fn,
            run_checkpoint_retry_generation=run_checkpoint_retry_generation,
            tenant_id=tenant_id,
        )


def _enforce_fake_action(token: str, action: str) -> tuple[SimpleNamespace, TenantRequestContext]:
    users = {
        "owner-token": SimpleNamespace(id=1, username="owner", is_active=True),
        "viewer-token": SimpleNamespace(id=2, username="viewer", is_active=True),
        "blocked-token": SimpleNamespace(id=3, username="blocked", is_active=True),
        "default-owner-token": SimpleNamespace(id=4, username="default-owner", is_active=True),
        "foreign-token": SimpleNamespace(id=5, username="foreign", is_active=True),
    }
    tenant_contexts = {
        "owner-token": TenantRequestContext(tenant_id=701, user_id=1, role="owner", membership_id=1, is_default_tenant=False),
        "viewer-token": TenantRequestContext(tenant_id=701, user_id=2, role="viewer", membership_id=2, is_default_tenant=False),
        "blocked-token": TenantRequestContext(tenant_id=701, user_id=3, role="unknown", membership_id=3, is_default_tenant=False),
        "default-owner-token": TenantRequestContext(tenant_id=1, user_id=4, role="owner", membership_id=4, is_default_tenant=True),
        "foreign-token": TenantRequestContext(tenant_id=702, user_id=5, role="owner", membership_id=5, is_default_tenant=False),
    }
    allowed_actions = {
        "owner": {
            ACTION_CHAT_READ,
            ACTION_CHAT_WRITE,
            ACTION_CHAT_RETRY,
            ACTION_STREAM_REPLAY,
            ACTION_STREAM_SUBSCRIBE,
        },
        "viewer": {ACTION_CHAT_READ, ACTION_STREAM_REPLAY, ACTION_STREAM_SUBSCRIBE},
        "unknown": set(),
    }
    user = users.get(token)
    tenant_context = tenant_contexts.get(token)
    if user is None or tenant_context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
    if action not in allowed_actions.get(tenant_context.role, set()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Tenant policy denied action '{action}'.")
    return user, tenant_context


def _request(
    client: TestClient,
    *,
    method: str,
    path: str,
    token: str,
    json_body: dict | None = None,
):
    headers = {"Authorization": f"Bearer {token}"}
    if method == "get":
        return client.get(path, headers=headers)
    return client.post(path, json=json_body, headers=headers)


@pytest.fixture
def phase3_chat_runtime_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(llm_routes.router)
    app.include_router(reasoning_routes.router, prefix="/api")
    app.include_router(interrupts_routes.router, prefix="/api/tasks")
    app.include_router(interrupt_inbox_routes.router, prefix="/api/tasks")

    task_tenants = {11: 701, 22: 702, 31: 1}

    def fake_get_db():
        yield _FakeDB()

    def fake_get_current_user(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        return _enforce_fake_action(token, ACTION_CHAT_READ)[0]

    def fake_get_tenant_context(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        return _enforce_fake_action(token, ACTION_CHAT_READ)[1]

    def fake_get_tenant_task_or_404(*, db, task_id: int, tenant_context):
        _ = db
        tenant_id = task_tenants.get(int(task_id))
        if tenant_id is None or int(tenant_id) != int(tenant_context.tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return SimpleNamespace(id=int(task_id), tenant_id=int(tenant_id), name=f"task-{task_id}")

    def fake_authorize_task_action(*, task_id: int, request: Request, db, action: str):
        _ = db
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user, tenant_context = _enforce_fake_action(token, action)
        tenant_id = task_tenants.get(int(task_id))
        if tenant_id is None or int(tenant_id) != int(tenant_context.tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return user, tenant_context

    def fake_prepare_reasoning_stream_preflight(task_id: int, request: Request):
        fake_authorize_task_action(
            task_id=task_id,
            request=request,
            db=None,
            action=ACTION_STREAM_SUBSCRIBE,
        )

    monkeypatch.setattr(llm_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(llm_routes, "LLMProviderSelectionService", _FakeLLMProviderSelectionService)
    monkeypatch.setattr(reasoning_routes, "_authorize_task_action", fake_authorize_task_action)
    monkeypatch.setattr(reasoning_routes, "_prepare_reasoning_stream_preflight", fake_prepare_reasoning_stream_preflight)
    monkeypatch.setattr(reasoning_routes, "AgentReasoningHistoryService", _FakeReasoningHistoryService)
    monkeypatch.setattr(reasoning_routes, "_reasoning_sse_service", _FakeReasoningSSEService())
    monkeypatch.setattr(interrupts_routes, "TaskInterruptService", _FakeTaskInterruptService)
    monkeypatch.setattr(interrupts_routes, "TaskGraphRetryService", _FakeTaskGraphRetryService)
    monkeypatch.setattr(interrupts_routes, "get_interrupt_state_service", lambda: object())
    monkeypatch.setattr(interrupt_inbox_routes, "TaskInterruptService", _FakeTaskInterruptService)

    app.dependency_overrides[llm_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[llm_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[llm_routes.get_db] = fake_get_db
    app.dependency_overrides[interrupts_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[interrupts_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[interrupts_routes.get_db] = fake_get_db
    app.dependency_overrides[interrupt_inbox_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[interrupt_inbox_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[interrupt_inbox_routes.get_db] = fake_get_db
    app.dependency_overrides[reasoning_routes.get_db] = fake_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


@pytest.mark.parametrize(
    ("method", "path", "json_body", "token"),
    [
        ("get", "/api/llm/tasks/11/conversation", None, "owner-token"),
        ("get", "/api/tasks/11/reasoning/history", None, "owner-token"),
        ("get", "/api/tasks/11/reasoning/replay", None, "owner-token"),
        ("get", "/api/tasks/11/reasoning/stream", None, "owner-token"),
        ("get", "/api/tasks/11/interrupt", None, "owner-token"),
        (
            "post",
            "/api/tasks/11/graph/resume",
            {
                "interrupt_id": "int-11",
                "interrupt_type": "tool_approval",
                "response": {"action": "approve"},
            },
            "owner-token",
        ),
        (
            "post",
            "/api/tasks/11/graph/retry",
            {"turn_id": "turn-11", "retry_mode": "checkpoint"},
            "owner-token",
        ),
        ("get", "/api/tasks/interrupts/inbox", None, "owner-token"),
    ],
)
def test_phase3_chat_runtime_surfaces_same_tenant_allowed(
    phase3_chat_runtime_client: TestClient,
    method: str,
    path: str,
    json_body: dict | None,
    token: str,
) -> None:
    response = _request(
        phase3_chat_runtime_client,
        method=method,
        path=path,
        token=token,
        json_body=json_body,
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/llm/tasks/11/conversation", None),
        ("get", "/api/tasks/11/reasoning/history", None),
        ("get", "/api/tasks/11/reasoning/replay", None),
        ("get", "/api/tasks/11/reasoning/stream", None),
        ("get", "/api/tasks/11/interrupt", None),
        (
            "post",
            "/api/tasks/11/graph/resume",
            {
                "interrupt_id": "int-11",
                "interrupt_type": "tool_approval",
                "response": {"action": "approve"},
            },
        ),
        (
            "post",
            "/api/tasks/11/graph/retry",
            {"turn_id": "turn-11", "retry_mode": "checkpoint"},
        ),
        ("get", "/api/tasks/interrupts/inbox", None),
    ],
)
def test_phase3_chat_runtime_surfaces_same_tenant_denied(
    phase3_chat_runtime_client: TestClient,
    method: str,
    path: str,
    json_body: dict | None,
) -> None:
    response = _request(
        phase3_chat_runtime_client,
        method=method,
        path=path,
        token="blocked-token",
        json_body=json_body,
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/llm/tasks/22/conversation", None),
        ("get", "/api/tasks/22/reasoning/history", None),
        ("get", "/api/tasks/22/reasoning/replay", None),
        ("get", "/api/tasks/22/reasoning/stream", None),
        ("get", "/api/tasks/22/interrupt", None),
        (
            "post",
            "/api/tasks/22/graph/resume",
            {
                "interrupt_id": "int-22",
                "interrupt_type": "tool_approval",
                "response": {"action": "approve"},
            },
        ),
        (
            "post",
            "/api/tasks/22/graph/retry",
            {"turn_id": "turn-22", "retry_mode": "checkpoint"},
        ),
    ],
)
def test_phase3_chat_runtime_surfaces_foreign_tenant_no_leakage(
    phase3_chat_runtime_client: TestClient,
    method: str,
    path: str,
    json_body: dict | None,
) -> None:
    response = _request(
        phase3_chat_runtime_client,
        method=method,
        path=path,
        token="owner-token",
        json_body=json_body,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_interrupt_inbox_foreign_tenant_no_leakage(phase3_chat_runtime_client: TestClient) -> None:
    response = phase3_chat_runtime_client.get(
        "/api/tasks/interrupts/inbox",
        headers={"Authorization": "Bearer foreign-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"] == []
    assert payload["count"] == 0


@pytest.mark.parametrize(
    ("method", "path", "json_body", "expected_keys"),
    [
        (
            "get",
            "/api/llm/tasks/31/conversation",
            None,
            ("id", "provider", "model", "conversation_id", "title", "status", "is_active"),
        ),
        (
            "get",
            "/api/tasks/31/reasoning/history",
            None,
            ("task_id", "events", "after", "before", "limit", "order"),
        ),
        (
            "get",
            "/api/tasks/31/reasoning/replay",
            None,
            ("task_id", "items", "nextAfter", "hasMore", "limit"),
        ),
        (
            "get",
            "/api/tasks/31/interrupt",
            None,
            ("has_interrupt", "task_id", "task_missing", "thread_id", "graph_name", "interrupt_id", "checkpoint_id", "interrupt_type", "payload", "resumable"),
        ),
        (
            "post",
            "/api/tasks/31/graph/resume",
            {
                "interrupt_id": "int-31",
                "interrupt_type": "tool_approval",
                "response": {"action": "approve"},
            },
            ("accepted", "task_id"),
        ),
        (
            "post",
            "/api/tasks/31/graph/retry",
            {"turn_id": "turn-31", "retry_mode": "checkpoint"},
            ("accepted", "task_id", "retry_mode"),
        ),
        ("get", "/api/tasks/interrupts/inbox", None, ("items", "count")),
    ],
)
def test_phase3_chat_runtime_surfaces_default_tenant_response_shape(
    phase3_chat_runtime_client: TestClient,
    method: str,
    path: str,
    json_body: dict | None,
    expected_keys: tuple[str, ...],
) -> None:
    response = _request(
        phase3_chat_runtime_client,
        method=method,
        path=path,
        token="default-owner-token",
        json_body=json_body,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == expected_keys


def test_reasoning_stream_default_tenant_response_shape(phase3_chat_runtime_client: TestClient) -> None:
    response = phase3_chat_runtime_client.get(
        "/api/tasks/31/reasoning/stream",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("content-type", "").startswith("text/event-stream")
    assert "data:" in response.text


def test_phase3_llm_conversation_reads_persisted_row(phase3_chat_runtime_client: TestClient) -> None:
    response = phase3_chat_runtime_client.get(
        "/api/llm/tasks/11/conversation",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == 1001
    assert payload["conversation_id"] == "seeded-conv-11"
