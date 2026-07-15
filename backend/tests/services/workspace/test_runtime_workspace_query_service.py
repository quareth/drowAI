"""Tests for live file explorer and scope workspace routing."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest
from fastapi import HTTPException

from backend.services.runtime_provider.contracts import RuntimeCallScope
from backend.services.workspace.runtime_file_explorer_service import RuntimeFileExplorerService
from backend.services.workspace.runtime_workspace_query_service import TaskWorkspaceQueryService


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


def test_runner_file_explorer_uses_runtime_provider_read_query_only() -> None:
    service = RuntimeFileExplorerService(db=object())
    task = SimpleNamespace(id=9001)
    context = SimpleNamespace(runtime_placement_mode="runner", tenant_id=77)
    calls: list[dict[str, Any]] = []

    async def _fake_context_for_task(*, task, user_id):  # type: ignore[no-redef]
        del task, user_id
        return context

    async def _fake_run_for_context(*, operation, payload=None, metadata=None, **kwargs):
        del kwargs
        calls.append({"operation": operation, "payload": payload, "metadata": metadata})
        if operation == "query_runtime_artifacts":
            return _FakeRuntimeResult(
                ok=True,
                metadata={
                    "delegate_result": {
                        "items": [
                            {"path": "reports/a.txt", "size": 5, "modified": "2026-05-28T00:00:00+00:00"},
                            {"path": "reports/nested/b.txt", "size": 6},
                        ]
                    }
                },
            )
        if operation == "read_runtime_artifact_file":
            path = payload["path"]
            if payload.get("binary"):
                content_base64 = "Ynl0ZXM="
                return _FakeRuntimeResult(
                    ok=True,
                    metadata={"delegate_result": {"path": path, "content_base64": content_base64, "size": 5}},
                )
            return _FakeRuntimeResult(
                ok=True,
                metadata={"delegate_result": {"path": path, "content": "hello", "encoding": "utf-8", "size": 5}},
            )
        raise AssertionError(f"Unexpected operation: {operation}")

    service._context_for_task = _fake_context_for_task  # type: ignore[assignment]
    service._runtime_operations = SimpleNamespace(run_for_context=_fake_run_for_context)

    tree = asyncio.run(service.get_directory_tree(task=task, user_id=1, path="/reports"))
    content = asyncio.run(service.get_file_content(task=task, user_id=1, path="/reports/a.txt"))
    download = asyncio.run(service.resolve_download_path(task=task, user_id=1, path="/reports/a.txt"))
    zip_path = asyncio.run(service.create_zip_archive(task=task, user_id=1, file_paths=["/reports"]))
    search = asyncio.run(service.search_files(task=task, user_id=1, query="report", path="/reports"))

    assert tree["path"] == "/reports"
    assert content["content"] == "hello"
    assert download.path.exists()
    assert download.cleanup_after_response is True
    assert zip_path.exists()
    assert search["total_count"] == 0

    assert {call["operation"] for call in calls} == {
        "query_runtime_artifacts",
        "read_runtime_artifact_file",
    }
    assert all(call["metadata"]["wait_for_result"] is True for call in calls)
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ["reports/a.txt", "reports/nested/b.txt"]

    download.path.unlink(missing_ok=True)
    zip_path.unlink(missing_ok=True)


def test_product_local_file_explorer_rejects_before_workspace_materialization() -> None:
    service = RuntimeFileExplorerService(db=object())
    task = SimpleNamespace(id=52)
    context = SimpleNamespace(
        task_id=52,
        runtime_placement_mode="local",
        runtime_call_scope=RuntimeCallScope.PRODUCT_TASK,
    )

    async def _fake_context_for_task(*, task, user_id):  # type: ignore[no-redef]
        del task, user_id
        return context

    service._context_for_task = _fake_context_for_task  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(service.get_file_content(task=task, user_id=1, path="/reports/scan.txt"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["reason_code"] == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"


def test_runner_scope_markdown_uses_data_plane_content() -> None:
    class _FakeDataPlaneService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def get_file_content(self, **kwargs):
            self.calls.append(("content", kwargs))
            return {"content": "# scope"}

    fake_data_plane = _FakeDataPlaneService()
    service = TaskWorkspaceQueryService(db=object(), artifact_file_browser_service=fake_data_plane)
    task = SimpleNamespace(id=41)
    context = SimpleNamespace(runtime_placement_mode="runner", tenant_id=12)

    async def _fake_context_for_task(*, task, user_id):  # type: ignore[no-redef]
        del task, user_id
        return context

    service._context_for_task = _fake_context_for_task  # type: ignore[assignment]
    scope_markdown = asyncio.run(service.read_scope_markdown(task=task, user_id=3))

    assert scope_markdown == "# scope"
    assert fake_data_plane.calls == [
        ("content", {"tenant_id": 12, "task_id": 41, "path": "scope.md"})
    ]


def test_local_mode_download_uses_workspace_resolution() -> None:
    service = RuntimeFileExplorerService(db=object(), runtime_call_scope=RuntimeCallScope.DIAGNOSTIC)
    task = SimpleNamespace(
        id=51,
        user_id=1,
        tenant_id=99,
        workspace_id="task-51",
        runtime_placement_mode="local",
        graph_thread_id="a" * 32,
        runner_id=None,
        execution_site_id=None,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir).resolve()
        nested = workspace / "reports"
        nested.mkdir(parents=True, exist_ok=True)
        expected = nested / "scan.txt"
        expected.write_text("ok", encoding="utf-8")

        calls: list[dict[str, Any]] = []

        async def _fake_run_for_context(*, context, operation, payload=None, **kwargs):
            del payload, kwargs
            calls.append({"operation": operation, "scope": context.runtime_call_scope})
            return _FakeRuntimeResult(ok=True, metadata={"delegate_result": {"workspace_path": workspace}})

        service._runtime_operations = SimpleNamespace(run_for_context=_fake_run_for_context)

        resolved = asyncio.run(service.resolve_download_path(task=task, user_id=1, path="/reports/scan.txt"))
        try:
            assert resolved.path != expected
            assert resolved.path.read_text(encoding="utf-8") == "ok"
            assert resolved.cleanup_after_response is True
        finally:
            resolved.path.unlink(missing_ok=True)
        assert calls == [{"operation": "materialize_runtime_workspace", "scope": RuntimeCallScope.DIAGNOSTIC}]


def test_local_mode_get_file_content_uses_materialized_workspace() -> None:
    service = RuntimeFileExplorerService(db=object(), runtime_call_scope=RuntimeCallScope.DIAGNOSTIC)
    task = SimpleNamespace(
        id=61,
        user_id=2,
        tenant_id=11,
        workspace_id="task-61",
        runtime_placement_mode="local",
        graph_thread_id="b" * 32,
        runner_id=None,
        execution_site_id=None,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir).resolve()
        reports = workspace / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        local_file = reports / "report.md"
        local_file.write_text("hello local", encoding="utf-8")

        calls: list[dict[str, Any]] = []

        async def _fake_run_for_context(*, context, operation, payload=None, **kwargs):
            del payload, kwargs
            calls.append({"operation": operation, "scope": context.runtime_call_scope})
            return _FakeRuntimeResult(ok=True, metadata={"delegate_result": {"workspace_path": workspace}})

        service._runtime_operations = SimpleNamespace(run_for_context=_fake_run_for_context)

        payload = asyncio.run(service.get_file_content(task=task, user_id=2, path="/reports/report.md"))

    assert payload["path"] == "/reports/report.md"
    assert "hello local" in payload["content"]
    assert calls == [{"operation": "materialize_runtime_workspace", "scope": RuntimeCallScope.DIAGNOSTIC}]
