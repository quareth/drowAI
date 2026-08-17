"""Parity tests for shell-session operational logs and metrics."""

from __future__ import annotations

import logging

import pytest

from backend.services.metrics import metrics
from backend.services.terminal.shell_session_observability import (
    ShellSessionOperationalObserver,
)
from runtime_shared.shell_session_contracts import (
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
)


def _identity(*, placement: str = "runner") -> ShellSessionIdentity:
    return ShellSessionIdentity(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:secret-owner",
        runtime_placement_mode=placement,
        workspace_id="task-11",
        workspace_path="/workspace/private",
        runner_id="private-runner",
        execution_site_id=None,
    )


def test_observer_preserves_open_complete_close_logs_and_metrics(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = ShellSessionOperationalObserver()
    metrics.counters.clear()
    metrics.gauges.clear()
    monkeypatch.setattr(metrics, "enabled", True)

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.terminal.shell_session_service",
    ):
        observer.session_opened(
            identity=_identity(),
            public_session_id="shs_private",
            active_placements=("runner", "local"),
        )
        observer.process_completed(
            identity=_identity(),
            public_session_id="shs_private",
            process_status=ShellProcessStatus.COMPLETED,
        )
        observer.session_closed(
            identity=_identity(),
            public_session_id="shs_private",
            close_reason="Unsafe reason!?",
        )

    log_text = caplog.text
    assert "event=session_opened" in log_text
    assert "event=process_completed" in log_text
    assert "event=session_closed" in log_text
    assert "close_reason=unsafe_reason" in log_text
    assert "placement=runner" in log_text
    assert "main:secret-owner" not in log_text
    assert "shs_private" not in log_text
    assert "/workspace/private" not in log_text
    assert "private-runner" not in log_text
    assert metrics.counters["shell_session_starts"] == 1
    assert metrics.counters["shell_session_terminal_outcomes.completed"] == 1
    assert metrics.gauges["shell_session_active_sessions.runner"] == 1.0
    assert metrics.gauges["shell_session_active_sessions.local"] == 1.0


def test_observer_normalizes_failures_and_zero_active_gauges(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = ShellSessionOperationalObserver()
    metrics.counters.clear()
    metrics.gauges.clear()
    monkeypatch.setattr(metrics, "enabled", True)

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.terminal.shell_session_service",
    ):
        observer.operation_failed(
            identity=_identity(placement="unsafe/value"),
            error_code=ShellSessionErrorCode.SESSION_UNAVAILABLE,
            public_session_id="",
        )
        observer.active_session_gauges(
            changed_placements=("unsafe/value",),
            active_placements=(),
        )

    assert "placement=unknown" in caplog.text
    assert "error_code=session_unavailable" in caplog.text
    assert metrics.counters[
        "shell_session_operation_failures.session_unavailable"
    ] == 1
    assert metrics.gauges["shell_session_active_sessions.unknown"] == 0.0
