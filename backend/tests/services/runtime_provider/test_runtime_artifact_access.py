"""Tests for shared runtime workspace artifact read access."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.runtime_artifact_access import (
    build_runtime_artifact_read_payload,
    decode_runtime_artifact_binary_delegate,
    execute_runtime_artifact_read_sync,
    normalize_runtime_artifact_relative_path,
    runtime_artifact_wait_metadata,
)
from backend.services.runtime_provider.sync_bridge import run_provider_operation_sync


def test_normalize_runtime_artifact_relative_path_strips_workspace_prefix() -> None:
    assert normalize_runtime_artifact_relative_path("/workspace/artifacts/out.txt") == "artifacts/out.txt"


def test_build_runtime_artifact_read_payload_includes_wait_fields_in_metadata_separately() -> None:
    payload = build_runtime_artifact_read_payload(path="artifacts/out.txt", binary=True)
    assert payload["path"] == "artifacts/out.txt"
    assert payload["artifact_path"] == "artifacts/out.txt"
    assert payload["binary"] is True
    metadata = runtime_artifact_wait_metadata()
    assert metadata["wait_for_result"] is True
    assert metadata["wait_timeout_seconds"] == 30.0


def test_run_provider_operation_sync_keeps_session_open_until_coro_finishes() -> None:
    lifecycle: list[str] = []

    class _Session:
        def close(self) -> None:
            lifecycle.append("closed")

    async def _operation() -> str:
        lifecycle.append("running")
        return "ok"

    async def _runner() -> None:
        def _coro_factory():
            async def _run() -> str:
                session = _Session()
                try:
                    return await _operation()
                finally:
                    session.close()

            return _run()

        result = run_provider_operation_sync(_coro_factory)
        assert result == "ok"

    asyncio.run(_runner())
    assert lifecycle == ["running", "closed"]


def test_execute_runtime_artifact_read_sync_passes_wait_metadata(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeRuntimeOperationService:
        def __init__(self, _db) -> None:
            pass

        def context_for_internal_task(self, **kwargs):
            return object()

        async def run_for_context(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "path": "artifacts/out.txt",
                        "content_base64": "b2s=",
                    }
                },
            )

    monkeypatch.setattr(
        "backend.services.runtime_provider.runtime_artifact_access.RuntimeOperationService",
        _FakeRuntimeOperationService,
    )

    async def _runner() -> None:
        result = execute_runtime_artifact_read_sync(
            object(),
            task_id=49,
            path="/workspace/artifacts/out.txt",
            actor_type=RuntimeActorType.SYSTEM,
            actor_id="artifact_provenance",
            binary=True,
        )
        data, path = decode_runtime_artifact_binary_delegate(result, fallback_path="artifacts/out.txt")
        assert data == b"ok"
        assert path == "artifacts/out.txt"

    asyncio.run(_runner())
    assert calls[0]["metadata"]["wait_for_result"] is True
    assert calls[0]["payload"]["binary"] is True
    assert calls[0]["payload"]["path"] == "artifacts/out.txt"


def test_execute_runtime_artifact_read_sync_uses_worker_thread_under_running_loop() -> None:
    main_thread = threading.get_ident()
    observed: dict[str, int | None] = {"thread": None}

    async def _runner() -> None:
        def _coro_factory():
            async def _run() -> str:
                observed["thread"] = threading.get_ident()
                return "ok"

            return _run()

        result = run_provider_operation_sync(_coro_factory)
        assert result == "ok"

    asyncio.run(_runner())
    assert observed["thread"] is not None
    assert observed["thread"] != main_thread
