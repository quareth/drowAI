"""Tests for extracted websocket log streamer behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.services.websocket.log_streamer import (
    _normalize_log_entries,
    _normalize_startup_progress,
    stream_logs_to_client,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))


@pytest.mark.asyncio
async def test_stream_logs_sanitizes_generic_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services.websocket import log_streamer as ws_log_streamer

    async def _raise_generic(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("unexpected secret details")

    async def _cancel_sleep(_seconds: float):  # noqa: ANN001
        raise asyncio.CancelledError

    monkeypatch.setattr(ws_log_streamer, "_run_stream_operation", _raise_generic)
    monkeypatch.setattr(ws_log_streamer.asyncio, "sleep", _cancel_sleep)

    ws = _FakeWebSocket()
    await stream_logs_to_client(ws, 123)

    assert any(p.get("type") == "error" and p.get("message") == "stream_error" for p in ws.sent_payloads)


@pytest.mark.asyncio
async def test_stream_logs_reports_unassigned_runner_task_without_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.websocket import log_streamer as ws_log_streamer

    async def _should_not_run_provider(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("provider should not be called for unassigned runner task")

    async def _cancel_sleep(_seconds: float):  # noqa: ANN001
        raise asyncio.CancelledError

    monkeypatch.setattr(ws_log_streamer, "_runner_assignment_pending", lambda *, task_id: task_id == 456)
    monkeypatch.setattr(ws_log_streamer, "_run_stream_operation", _should_not_run_provider)
    monkeypatch.setattr(ws_log_streamer.asyncio, "sleep", _cancel_sleep)

    ws = _FakeWebSocket()
    await stream_logs_to_client(ws, 456)

    assert ws.sent_payloads == [
        {
            "type": "container_status",
            "task_id": 456,
            "status": "starting",
            "message": "Runtime is waiting for runner assignment.",
            "timestamp": ws.sent_payloads[0]["timestamp"],
        }
    ]


def test_runner_startup_progress_normalizes_running_container() -> None:
    progress = _normalize_startup_progress(
        {
            "job_status": "running",
            "container_status": "running",
            "startup_phase": "ready",
        }
    )

    assert progress["container_exists"] is True
    assert progress["status"] == "running"


def test_runner_raw_logs_normalize_to_stream_rows() -> None:
    logs = _normalize_log_entries(
        {
            "logs": "first line\n2026-05-22T17:43:00Z second line",
            "runtime_job_id": "task-115",
        }
    )

    assert [entry["message"] for entry in logs] == ["first line", "second line"]
    assert all(entry["service"] == "kali-container" for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "error_message"),
    [
        ("RUNNER_OPERATION_RESULT_TIMEOUT", "operation timed out"),
        ("RUNNER_OPERATION_RESULT_MISMATCH", "result mismatch"),
        ("RUNNER_ASSIGNMENT_REQUIRED", "runner offline"),
    ],
)
async def test_stream_logs_fails_closed_on_provider_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    error_message: str,
) -> None:
    from backend.services.websocket import log_streamer as ws_log_streamer

    async def _fake_run_stream_operation(*, task_id, operation, call, metadata=None):
        assert task_id == 321
        assert operation == "get_runtime_logs"
        return SimpleNamespace(
            ok=False,
            provider="cloud_runner",
            status=SimpleNamespace(value="failed"),
            error_code=error_code,
            error_message=error_message,
            metadata={},
        )

    monkeypatch.setattr(ws_log_streamer, "_run_stream_operation", _fake_run_stream_operation)

    ws = _FakeWebSocket()
    await stream_logs_to_client(ws, 321)

    assert ws.sent_payloads
    failure = ws.sent_payloads[-1]
    assert failure["type"] == "container_status"
    assert failure["status"] == "stopped"
    assert failure["operation"] == "get_runtime_logs"
    assert failure["error_code"] == error_code
    assert failure["error_message"] == error_message
