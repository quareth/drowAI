"""Tenant/user-authorization matrix tests for task scope/logs/container routes.

Responsibilities:
- Verify same-tenant owner and role behavior on migrated task routes.
- Verify cross-tenant task ids fail closed with stable contracts.
- Preserve compatibility response envelopes for Phase 3 surfaces.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers.tasks import container as container_routes
from backend.routers.tasks import files as files_routes
from backend.routers.tasks import logs as logs_routes
from backend.routers.tasks import metrics as metrics_routes
from backend.routers.tasks import runtime as runtime_routes
from backend.routers.tasks import scope as scope_routes
from backend.routers.tasks import vpn as vpn_routes
from backend.domain.task_lifecycle import TaskStatus
from backend.services.runtime_provider.contracts import RuntimeActorType, RuntimeCallScope


class _FakeQueryService:
    def __init__(self, _db) -> None:
        pass

    async def read_scope_markdown(self, *, task, user_id):
        _ = task, user_id
        return "target: 10.0.0.5"


class _FakeScopeParser:
    def parse_markdown_content(self, _content):
        return SimpleNamespace(to_dict=lambda: {"targets": ["10.0.0.5"]})

    def get_validation_errors(self):
        return []

    def get_warnings(self):
        return []

    def has_errors(self):
        return False


class _FakeRuntimeOperationService:
    def __init__(self, _db) -> None:
        pass

    @staticmethod
    def context_from_authorized_task(*, task, user_id, runtime_call_scope):
        return SimpleNamespace(
            task_id=task.id,
            user_id=user_id,
            tenant_id=task.tenant_id,
            runtime_call_scope=runtime_call_scope,
        )

    async def run_for_context(self, *, context, operation, call, payload=None, metadata=None):
        assert isinstance(context.runtime_call_scope, RuntimeCallScope)
        _ = context, operation, call, payload, metadata
        return SimpleNamespace(
            ok=True,
            error_code=None,
            metadata={"delegate_result": (True, "running", {"status": "running"})},
        )

    async def run_authorized_task_operation(
        self, *, task, user_id, operation, call, payload=None, metadata=None
    ):
        _ = task, user_id, operation, call, payload, metadata
        return SimpleNamespace(
            ok=True,
            error_code=None,
            metadata={"delegate_result": {"cpu_percent": 5.0}},
        )


class _FakeTaskLifecycleService:
    def __init__(self, _db) -> None:
        self._db = _db

    async def retry_task_vpn_connection_async(self, *, task, user_id, db, actor_type):
        assert db is self._db
        assert task.id in {11, 31}
        assert user_id in {1, 4}
        assert actor_type is RuntimeActorType.USER
        return SimpleNamespace(
            ok=True,
            error_code=None,
            metadata={
                "delegate_result": [{"message": "vpn retry complete"}],
            },
        )


class _FakeTaskRuntimeService:
    def __init__(self, _db) -> None:
        pass

    async def start_task(self, *, task_id: int, user_id: int, tenant_id: int):
        _ = user_id, tenant_id
        return SimpleNamespace(id=task_id, container_id=f"container-{task_id}")

    async def pause_task(self, *, task_id: int, user_id: int, tenant_id: int):
        _ = task_id, user_id, tenant_id
        return {"message": "Task is already paused"}


class _FakeRuntimeFileExplorerService:
    def __init__(self, _db, *, runtime_call_scope) -> None:
        assert isinstance(runtime_call_scope, RuntimeCallScope)
        self.runtime_call_scope = runtime_call_scope

    async def search_files(self, *, task, user_id, query, path=None):
        _ = task, user_id, path
        return {"query": query, "matches": [], "count": 0}


class _FakeVPNService:
    def __init__(self, _db) -> None:
        pass

    async def update_vpn_status(
        self,
        *,
        task_id: int,
        status: str,
        ip_address: str | None = None,
        error_message: str | None = None,
    ) -> None:
        assert task_id in {11, 31}
        assert status == "reconnecting"
        assert ip_address is None
        assert error_message is None


class _FakeLogScalarResult:
    def all(self):
        return [
            SimpleNamespace(
                id=1,
                task_id=11,
                sequence=1,
                type="info",
                content="hello",
                log_metadata={"source": "test"},
                timestamp=None,
            )
        ]


class _FakeLogExecuteResult:
    def scalars(self):
        return _FakeLogScalarResult()


class _FakeTaskQuery:
    def __init__(self, tasks):
        self._tasks = tasks

    def filter(self, _expr):
        return self

    def all(self):
        return list(self._tasks)


class _FakeDB:
    def __init__(self, tasks):
        self._tasks = tasks

    def execute(self, _stmt):
        return _FakeLogExecuteResult()

    def query(self, _model):
        return _FakeTaskQuery(self._tasks)


@pytest.fixture
def tasks_authz_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(scope_routes.router, prefix="/api/tasks")
    app.include_router(logs_routes.router, prefix="/api/tasks")
    app.include_router(container_routes.router, prefix="/api/tasks")
    app.include_router(runtime_routes.router, prefix="/api/tasks")
    app.include_router(metrics_routes.router, prefix="/api/tasks")
    app.include_router(vpn_routes.router, prefix="/api/tasks")
    app.include_router(files_routes.router, prefix="/api/tasks")

    users = {
        "owner-token": SimpleNamespace(id=1, username="owner", is_active=True),
        "viewer-token": SimpleNamespace(id=2, username="viewer", is_active=True),
        "blocked-token": SimpleNamespace(id=1, username="blocked-owner-role", is_active=True),
        "default-owner-token": SimpleNamespace(id=4, username="default-owner", is_active=True),
        "foreign-token": SimpleNamespace(id=4, username="default-owner", is_active=True),
    }
    tenant_contexts = {
        "owner-token": SimpleNamespace(tenant_id=701, user_id=1, role="owner"),
        "viewer-token": SimpleNamespace(tenant_id=701, user_id=2, role="viewer"),
        "blocked-token": SimpleNamespace(tenant_id=701, user_id=1, role="unknown"),
        "default-owner-token": SimpleNamespace(tenant_id=1, user_id=4, role="owner"),
        "foreign-token": SimpleNamespace(tenant_id=702, user_id=4, role="owner"),
    }
    task_tenants = {11: 701, 22: 702, 31: 1}
    task_owners = {11: 1, 22: 4, 31: 4}

    def fake_get_current_user(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = users.get(token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        return user

    def fake_get_tenant_context(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        context = tenant_contexts.get(token)
        if context is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        return context

    def fake_get_db():
        tasks = [
            SimpleNamespace(id=31, tenant_id=1),
        ]
        yield _FakeDB(tasks)

    def fake_get_tenant_task_or_404(*, db, task_id: int, tenant_context):
        _ = db
        tenant_id = task_tenants.get(int(task_id))
        owner_id = task_owners.get(int(task_id))
        if (
            tenant_id is None
            or owner_id is None
            or int(tenant_id) != int(tenant_context.tenant_id)
            or int(owner_id) != int(tenant_context.user_id)
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return SimpleNamespace(
            id=int(task_id),
            name=f"task-{task_id}",
            scope="fallback",
            tenant_id=int(tenant_id),
            user_id=tenant_context.user_id,
            vpn_enabled=True,
            status=TaskStatus.RUNNING.value,
        )

    monkeypatch.setattr(scope_routes, "TaskWorkspaceQueryService", _FakeQueryService)
    monkeypatch.setattr(scope_routes, "ScopeParser", _FakeScopeParser)
    monkeypatch.setattr(scope_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)

    monkeypatch.setattr(logs_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)

    monkeypatch.setattr(container_routes, "RuntimeOperationService", _FakeRuntimeOperationService)
    monkeypatch.setattr(container_routes, "TaskRuntimeService", _FakeTaskRuntimeService)
    monkeypatch.setattr(container_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(runtime_routes, "TaskRuntimeService", _FakeTaskRuntimeService)
    monkeypatch.setattr(runtime_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(metrics_routes, "RuntimeOperationService", _FakeRuntimeOperationService)
    monkeypatch.setattr(metrics_routes, "normalize_runtime_metrics_snapshot", lambda _value: {"cpu_percent": 5.0})
    monkeypatch.setattr(metrics_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(vpn_routes, "TaskLifecycleService", _FakeTaskLifecycleService)
    monkeypatch.setattr(vpn_routes, "VPNService", _FakeVPNService)
    monkeypatch.setattr(vpn_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(files_routes, "RuntimeFileExplorerService", _FakeRuntimeFileExplorerService)
    monkeypatch.setattr(files_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)

    app.dependency_overrides[scope_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[scope_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[scope_routes.get_db] = fake_get_db

    app.dependency_overrides[logs_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[logs_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[logs_routes.get_db] = fake_get_db

    app.dependency_overrides[container_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[container_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[container_routes.get_db] = fake_get_db
    app.dependency_overrides[runtime_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[runtime_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[runtime_routes.get_db] = fake_get_db
    app.dependency_overrides[metrics_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[metrics_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[metrics_routes.get_db] = fake_get_db
    app.dependency_overrides[vpn_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[vpn_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[vpn_routes.get_db] = fake_get_db
    app.dependency_overrides[files_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[files_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[files_routes.get_db] = fake_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_same_tenant_owner_allowed_role_succeeds(tasks_authz_client: TestClient) -> None:
    response = tasks_authz_client.get(
        "/api/tasks/11/scope",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert set(payload.keys()) >= {"success", "task_id", "task_name", "parsed_scope"}


def test_same_tenant_non_owner_fails_without_leakage(tasks_authz_client: TestClient) -> None:
    response = tasks_authz_client.get(
        "/api/tasks/11/scope",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_same_tenant_owner_denied_role_fails(tasks_authz_client: TestClient) -> None:
    response = tasks_authz_client.post(
        "/api/tasks/11/container/create",
        headers={"Authorization": "Bearer blocked-token"},
    )
    assert response.status_code == 403, response.text


def test_foreign_tenant_task_id_fails_without_leakage(tasks_authz_client: TestClient) -> None:
    response = tasks_authz_client.get(
        "/api/tasks/22/scope",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_default_tenant_standalone_parity_and_response_shape(tasks_authz_client: TestClient) -> None:
    response = tasks_authz_client.get(
        "/api/tasks/containers/list",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("containers", "total")
    assert isinstance(payload["containers"], list)
    assert payload["total"] == len(payload["containers"])


@pytest.mark.parametrize(
    ("method", "path", "token"),
    [
        ("post", "/api/tasks/11/pause", "owner-token"),
        ("get", "/api/tasks/11/metrics", "owner-token"),
        ("post", "/api/tasks/11/vpn/retry", "owner-token"),
        ("get", "/api/tasks/11/files/search?q=scope", "owner-token"),
    ],
)
def test_phase3_remaining_task_surfaces_same_tenant_allowed(
    tasks_authz_client: TestClient,
    method: str,
    path: str,
    token: str,
) -> None:
    response = getattr(tasks_authz_client, method)(
        path,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("method", "path", "token"),
    [
        ("post", "/api/tasks/11/pause", "blocked-token"),
        ("get", "/api/tasks/11/metrics", "blocked-token"),
        ("post", "/api/tasks/11/vpn/retry", "viewer-token"),
        ("get", "/api/tasks/11/files/search?q=scope", "blocked-token"),
    ],
)
def test_phase3_remaining_task_surfaces_same_tenant_denied(
    tasks_authz_client: TestClient,
    method: str,
    path: str,
    token: str,
) -> None:
    response = getattr(tasks_authz_client, method)(
        path,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/tasks/22/pause"),
        ("get", "/api/tasks/22/metrics"),
        ("post", "/api/tasks/22/vpn/retry"),
        ("get", "/api/tasks/22/files/search?q=scope"),
    ],
)
def test_phase3_remaining_task_surfaces_foreign_tenant_no_leakage(
    tasks_authz_client: TestClient,
    method: str,
    path: str,
) -> None:
    response = getattr(tasks_authz_client, method)(
        path,
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


@pytest.mark.parametrize(
    ("method", "path", "expected_keys"),
    [
        ("post", "/api/tasks/31/pause", ("message",)),
        ("get", "/api/tasks/31/metrics", ("task_id", "metrics")),
        (
            "post",
            "/api/tasks/31/vpn/retry",
            ("message", "accepted", "connection_status", "exit_code", "logs"),
        ),
        ("get", "/api/tasks/31/files/search?q=scope", ("query", "matches", "count")),
    ],
)
def test_phase3_remaining_task_surfaces_default_tenant_response_shape(
    tasks_authz_client: TestClient,
    method: str,
    path: str,
    expected_keys: tuple[str, ...],
) -> None:
    response = getattr(tasks_authz_client, method)(
        path,
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 200, response.text
    assert tuple(response.json().keys()) == expected_keys
