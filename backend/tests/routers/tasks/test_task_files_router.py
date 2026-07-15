"""Router tests for task file-browser download response contracts.

Scope:
- Verifies `/api/tasks/{task_id}/files/download` preserves the requested filename.
- Covers runner/cloud live workspace downloads that stream from temporary files.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers.tasks import files as task_files_router
from backend.routers.tasks.deps import map_file_browser_exception
from backend.services.runtime_provider.contracts import RuntimeCallScope
from backend.services.workspace.runtime_file_explorer_service import RuntimeDownloadPath
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspacePathError,
)


@pytest.fixture
def files_api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(task_files_router.router, prefix="/api/tasks")

    class _StubQueryService:
        def __init__(self, db, *, runtime_call_scope) -> None:
            del db, runtime_call_scope

        async def resolve_download_path(self, *, task, user_id: int, path: str) -> RuntimeDownloadPath:
            del task, user_id, path
            temp_file = tmp_path / "tmp-download.bin"
            temp_file.write_bytes(b"runner-bytes")
            return RuntimeDownloadPath(path=temp_file, cleanup_after_response=False)

    def _fake_get_db():
        yield object()

    def _fake_get_current_user(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if auth_header != "Bearer owner-token":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )
        return SimpleNamespace(id=1, username="owner", is_active=True)

    monkeypatch.setattr(task_files_router, "RuntimeFileExplorerService", _StubQueryService)
    monkeypatch.setattr(task_files_router, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(
        task_files_router,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )
    app.dependency_overrides[task_files_router.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=1, user_id=1, role="owner")
    )
    app.dependency_overrides[task_files_router.get_db] = _fake_get_db
    app.dependency_overrides[task_files_router.get_current_user] = _fake_get_current_user

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_download_route_uses_requested_file_name_for_cloud_runner_download(files_api_client) -> None:
    response = files_api_client.get(
        "/api/tasks/42/files/download",
        params={"path": "/reports/final-report.txt"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    content_disposition = response.headers.get("content-disposition", "")
    assert 'filename="final-report.txt"' in content_disposition


@pytest.mark.parametrize(
    ("deterministic_mode", "expected_scope"),
    [
        (False, RuntimeCallScope.PRODUCT_TASK),
        (True, RuntimeCallScope.TEST),
    ],
)
def test_file_explorer_scope_is_test_only_in_deterministic_mode(
    monkeypatch: pytest.MonkeyPatch,
    deterministic_mode: bool,
    expected_scope: RuntimeCallScope,
) -> None:
    captured: dict[str, object] = {}

    class _ScopeCaptureService:
        def __init__(self, db, *, runtime_call_scope) -> None:
            captured.update(db=db, scope=runtime_call_scope)

    monkeypatch.setattr(task_files_router, "E2E_DETERMINISTIC_MODE", deterministic_mode)
    monkeypatch.setattr(task_files_router, "RuntimeFileExplorerService", _ScopeCaptureService)

    task_files_router._file_explorer_service(object())

    assert captured["scope"] is expected_scope


@pytest.mark.parametrize(
    "exc",
    [
        WorkspacePathError("workspace path must stay inside the workspace"),
        WorkspaceEntryUnsafeError("workspace entry is unsafe"),
    ],
)
def test_workspace_scope_rejections_map_to_forbidden(exc: ValueError) -> None:
    mapped = map_file_browser_exception(exc)

    assert mapped.status_code == status.HTTP_403_FORBIDDEN
    assert mapped.detail == str(exc)


def test_non_security_value_errors_map_to_bad_request() -> None:
    mapped = map_file_browser_exception(ValueError("Directory entry limit exceeded."))

    assert mapped.status_code == status.HTTP_400_BAD_REQUEST
