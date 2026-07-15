"""Unit coverage for host resource snapshots exposed to system settings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.platform.system_metrics_service import SystemMetricsService


def test_collect_reports_memory_storage_and_uptime(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.virtual_memory",
        lambda: SimpleNamespace(total=16_000, used=9_000, available=7_000, percent=56.25),
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.disk_usage",
        lambda path: SimpleNamespace(total=100_000, used=40_000, free=60_000, percent=40.0),
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.boot_time",
        lambda: 1_000.0,
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.time.time",
        lambda: 91_000.9,
    )

    metrics = SystemMetricsService(storage_path=tmp_path).collect()

    assert metrics.memory.model_dump() == {
        "total_bytes": 16_000,
        "used_bytes": 9_000,
        "available_bytes": 7_000,
        "usage_percent": 56.25,
    }
    assert metrics.storage.model_dump() == {
        "total_bytes": 100_000,
        "used_bytes": 40_000,
        "available_bytes": 60_000,
        "usage_percent": 40.0,
    }
    assert metrics.uptime_seconds == 90_000


def test_collect_clamps_negative_uptime_after_clock_adjustment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.virtual_memory",
        lambda: SimpleNamespace(total=1, used=0, available=1, percent=0.0),
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.disk_usage",
        lambda path: SimpleNamespace(total=1, used=0, free=1, percent=0.0),
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.psutil.boot_time",
        lambda: 2_000.0,
    )
    monkeypatch.setattr(
        "backend.services.platform.system_metrics_service.time.time",
        lambda: 1_000.0,
    )

    assert SystemMetricsService(storage_path=tmp_path).collect().uptime_seconds == 0
