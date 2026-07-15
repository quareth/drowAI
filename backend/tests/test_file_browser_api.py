"""File-browser API tests for runner workspace and product-local rejection paths."""

import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient

from backend.routers import tasks as tasks_router
from backend.routers.tasks import files as files_routes


class _FakeRuntimeResult:
    def __init__(
        self,
        *,
        ok: bool,
        metadata: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.ok = ok
        self.metadata = metadata or {}
        self.error_code = error_code
        self.error_message = error_message
        self.provider = "cloud_runner"
        self.status = SimpleNamespace(value="succeeded" if ok else "rejected")


def _install_runner_workspace_provider(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_run_for_context(self, *, context, operation, payload=None, metadata=None, **kwargs):
        del self, kwargs
        calls.append(
            {
                "placement": context.runtime_placement_mode,
                "operation": operation,
                "payload": payload or {},
                "metadata": metadata or {},
            }
        )
        if operation == "query_runtime_artifacts":
            return _FakeRuntimeResult(
                ok=True,
                metadata={
                    "delegate_result": {
                        "items": [
                            {"path": "scans/nmap.xml", "size": 8, "modified": "2026-07-09T00:00:00+00:00"},
                            {"path": "reports/one.txt", "size": 1, "modified": "2026-07-09T00:00:00+00:00"},
                            {"path": "two.txt", "size": 1, "modified": "2026-07-09T00:00:00+00:00"},
                            {"path": "ScanResult.txt", "size": 2, "modified": "2026-07-09T00:00:00+00:00"},
                        ]
                    }
                },
            )
        if operation == "read_runtime_artifact_file":
            path = str((payload or {}).get("path") or "")
            if ".." in path:
                return _FakeRuntimeResult(
                    ok=False,
                    error_code="RUNNER_WORKSPACE_PATH_OUTSIDE_SCOPE",
                    error_message="Path resolves outside workspace.",
                )
            if (payload or {}).get("binary"):
                content_base64 = {
                    "note.txt": "aGVsbG8=",
                    "reports/one.txt": "MQ==",
                    "two.txt": "Mg==",
                    "ScanResult.txt": "b2s=",
                }.get(path, "Ynl0ZXM=")
                return _FakeRuntimeResult(
                    ok=True,
                    metadata={"delegate_result": {"path": path, "content_base64": content_base64, "size": 5}},
                )
            text = '{"payload":"<script>x</script>"}' if path == "data.json" else "ok"
            return _FakeRuntimeResult(
                ok=True,
                metadata={"delegate_result": {"path": path, "content": text, "encoding": "utf-8", "size": len(text)}},
            )
        raise AssertionError(f"Unexpected runtime operation: {operation}")

    monkeypatch.setattr(
        "backend.services.workspace.runtime_file_explorer_service.RuntimeOperationService.run_for_context",
        fake_run_for_context,
    )
    return calls


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[int, int], dict[int, str]]:
    app = FastAPI()
    app.include_router(tasks_router.router, prefix="/api/tasks")

    users_by_token = {
        "owner-token": SimpleNamespace(id=1, username="owner", is_active=True),
        "other-token": SimpleNamespace(id=2, username="other", is_active=True),
    }
    task_tenants: dict[int, int] = {}
    task_placements: dict[int, str] = {}

    def fake_get_current_user(request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = auth.split(" ", 1)[1].strip()
        user = users_by_token.get(token)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    def fake_get_db():
        yield object()

    def fake_get_tenant_task_or_404(*, db, task_id: int, tenant_context):
        tenant_id = task_tenants.get(task_id)
        if tenant_id is None or int(tenant_id) != int(tenant_context.tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return SimpleNamespace(
            id=task_id,
            user_id=1,
            tenant_id=tenant_id,
            workspace_id=f"task-{task_id}",
            runtime_placement_mode=task_placements.get(task_id, "runner"),
            graph_thread_id="a" * 32,
            runner_id="runner-1" if task_placements.get(task_id, "runner") == "runner" else None,
            execution_site_id="site-1" if task_placements.get(task_id, "runner") == "runner" else None,
        )

    def fake_get_tenant_context(request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        token = auth.split(" ", 1)[1].strip()
        user = users_by_token.get(token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        if token == "owner-token":
            return SimpleNamespace(tenant_id=1, user_id=user.id, role="owner")
        return SimpleNamespace(tenant_id=2, user_id=user.id, role="owner")

    app.dependency_overrides[files_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[files_routes.get_db] = fake_get_db
    app.dependency_overrides[files_routes.get_tenant_request_context] = fake_get_tenant_context
    monkeypatch.setattr(files_routes, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(files_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)

    return TestClient(app), task_tenants, task_placements


def test_files_tree_endpoint_returns_runner_workspace_tree(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    calls = _install_runner_workspace_provider(monkeypatch)
    task_id = 1001
    task_tenants[task_id] = 1

    resp = client.get(f"/api/tasks/{task_id}/files/tree", headers={"Authorization": "Bearer owner-token"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["type"] == "folder"
    assert any(child["name"] == "scans" for child in data["children"])
    assert calls[0]["operation"] == "query_runtime_artifacts"
    assert calls[0]["placement"] == "runner"


def test_files_content_endpoint_returns_runner_sanitized_content(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    calls = _install_runner_workspace_provider(monkeypatch)
    task_id = 1002
    task_tenants[task_id] = 1

    resp = client.get(
        f"/api/tasks/{task_id}/files/content",
        params={"path": "/data.json"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preview_type"] == "json"
    assert "&lt;script&gt;x&lt;/script&gt;" in data["content"]
    assert calls[0]["operation"] == "read_runtime_artifact_file"
    assert calls[0]["payload"]["path"] == "data.json"


def test_files_download_endpoint_streams_runner_single_file(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    calls = _install_runner_workspace_provider(monkeypatch)
    task_id = 1003
    task_tenants[task_id] = 1

    resp = client.get(
        f"/api/tasks/{task_id}/files/download",
        params={"path": "/note.txt"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"hello"
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert calls[0]["operation"] == "read_runtime_artifact_file"
    assert calls[0]["payload"]["binary"] is True


def test_files_download_multiple_endpoint_streams_runner_zip(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    calls = _install_runner_workspace_provider(monkeypatch)
    task_id = 1004
    task_tenants[task_id] = 1

    resp = client.post(
        f"/api/tasks/{task_id}/files/download-multiple",
        json={"paths": ["/reports", "/two.txt"]},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type", "").startswith("application/zip")

    with zipfile.ZipFile(BytesIO(resp.content)) as archive:
        members = set(archive.namelist())
        assert "reports/one.txt" in members
        assert "two.txt" in members
    assert {call["operation"] for call in calls} == {
        "query_runtime_artifacts",
        "read_runtime_artifact_file",
    }


def test_files_search_endpoint_filters_runner_files_by_filename(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    calls = _install_runner_workspace_provider(monkeypatch)
    task_id = 1005
    task_tenants[task_id] = 1

    resp = client.get(
        f"/api/tasks/{task_id}/files/search",
        params={"q": "scan"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["results"][0]["name"] == "ScanResult.txt"
    assert calls[0]["operation"] == "query_runtime_artifacts"


def test_files_endpoints_require_authentication(api_client: tuple[TestClient, dict[int, int], dict[int, str]]) -> None:
    client, _task_tenants, _task_placements = api_client
    resp = client.get("/api/tasks/1/files/tree")
    assert resp.status_code in (401, 403), resp.text


def test_files_endpoints_validate_task_ownership(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
) -> None:
    client, task_tenants, _task_placements = api_client
    task_id = 1006
    task_tenants[task_id] = 1

    resp = client.get(
        f"/api/tasks/{task_id}/files/tree",
        headers={"Authorization": "Bearer other-token"},
    )
    assert resp.status_code == 404, resp.text


def test_files_content_blocks_path_traversal(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, _task_placements = api_client
    _install_runner_workspace_provider(monkeypatch)
    task_id = 1007
    task_tenants[task_id] = 1

    resp = client.get(
        f"/api/tasks/{task_id}/files/content",
        params={"path": "../../etc/passwd"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 403, resp.text


def test_files_content_rejects_product_local_task_before_workspace_fallback(
    api_client: tuple[TestClient, dict[int, int], dict[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task_tenants, task_placements = api_client
    task_id = 1008
    task_tenants[task_id] = 1
    task_placements[task_id] = "local"

    monkeypatch.setattr(
        "backend.config.workspace_config.WorkspaceConfig.get_task_workspace_path",
        lambda incoming_task_id: (_ for _ in ()).throw(
            AssertionError(f"Product local task {incoming_task_id} must not resolve a host workspace path.")
        ),
    )

    resp = client.get(
        f"/api/tasks/{task_id}/files/content",
        params={"path": "/note.txt"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"
