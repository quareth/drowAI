"""Tests for runner control metrics helpers and bounded label policy."""

from __future__ import annotations

from datetime import UTC, datetime

import backend.services.runner_control.metrics as runner_metrics
from backend.services.runner_control.metrics import (
    RunnerControlMetrics,
    heartbeat_latency_seconds,
    heartbeat_staleness_seconds,
)


def test_assignment_failure_reason_labels_are_bounded(monkeypatch) -> None:
    recorded: list[tuple[str, int]] = []

    def _fake_safe_inc(name: str, value: int = 1) -> None:
        recorded.append((name, value))

    monkeypatch.setattr(runner_metrics, "safe_inc", _fake_safe_inc)
    metrics = RunnerControlMetrics()

    metrics.record_assignment_failure(reason_codes=("RUNNER_NOT_ONLINE", "bad reason with spaces"))

    assert ("runner_control.assignment.failure_count", 1) in recorded
    assert ("runner_control.assignment.failure_reason.RUNNER_NOT_ONLINE", 1) in recorded
    assert ("runner_control.assignment.failure_reason.UNKNOWN", 1) in recorded


def test_heartbeat_observation_helpers_handle_valid_and_invalid_timestamps() -> None:
    now = datetime(2026, 5, 23, 16, 0, 10, tzinfo=UTC)

    latency = heartbeat_latency_seconds(created_at="2026-05-23T16:00:00+00:00", now=now)
    assert latency == 10.0

    invalid_latency = heartbeat_latency_seconds(created_at="not-a-timestamp", now=now)
    assert invalid_latency is None

    stale = heartbeat_staleness_seconds(
        last_seen_at=datetime(2026, 5, 23, 15, 59, 55, tzinfo=UTC),
        now=now,
    )
    assert stale == 15.0

    missing = heartbeat_staleness_seconds(last_seen_at=None, now=now)
    assert missing is None


def test_outbound_metric_helpers_emit_non_negative_counter_values(monkeypatch) -> None:
    recorded: list[tuple[str, int]] = []

    def _fake_safe_inc(name: str, value: int = 1) -> None:
        recorded.append((name, value))

    monkeypatch.setattr(runner_metrics, "safe_inc", _fake_safe_inc)
    metrics = RunnerControlMetrics()
    metrics.record_outbound_delivered(count=-4)
    metrics.record_outbound_acked(count=3)
    metrics.record_outbound_failed(count=2)

    assert ("runner_control.outbound.delivered_count", 0) in recorded
    assert ("runner_control.outbound.acked_count", 3) in recorded
    assert ("runner_control.outbound.failed_count", 2) in recorded
