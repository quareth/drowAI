"""Regression tests for runtime-provider snapshot normalization contracts."""

from __future__ import annotations

import math

from backend.services.runtime_provider.snapshot_normalization import (
    normalize_runtime_metrics_snapshot,
)


def test_normalize_runtime_metrics_preserves_local_metrics_shape() -> None:
    """Local provider metrics keep their existing values while gaining missing defaults."""
    metrics = normalize_runtime_metrics_snapshot(
        {
            "cpu_percent": 12.5,
            "memory_usage_mb": 256.25,
            "memory_limit_mb": 1024.5,
            "memory_percent": 25.0,
            "storage": {
                "used_bytes": 1048576,
                "size_root_fs": 10737418240,
                "used_mb": 1.0,
                "used_gb": 0.001,
            },
            "network": {"rx_bytes": 100, "tx_bytes": 200},
            "timestamp": "2026-06-27T00:00:00Z",
            "status": "running",
        }
    )

    assert metrics is not None
    assert metrics["cpu_percent"] == 12.5
    assert metrics["memory_usage_mb"] == 256.25
    assert metrics["memory_limit_mb"] == 1024.5
    assert metrics["memory_percent"] == 25.0
    assert metrics["storage"]["used_mb"] == 1.0
    assert metrics["network"] == {"rx_bytes": 100, "tx_bytes": 200}
    assert metrics["timestamp"] == "2026-06-27T00:00:00Z"
    assert metrics["status"] == "running"


def test_normalize_runtime_metrics_projects_runner_byte_fields() -> None:
    """Runner-native byte metrics become the client-facing MB contract."""
    metrics = normalize_runtime_metrics_snapshot(
        {
            "runtime_job_id": "job-1",
            "metrics": {
                "memory_usage": 128 * 1024 * 1024,
                "memory_limit": 1024 * 1024 * 1024,
                "cpu_total_usage": 55,
                "status": "running",
                "container_running": True,
            },
        }
    )

    assert metrics is not None
    assert metrics["memory_usage_mb"] == 128.0
    assert metrics["memory_limit_mb"] == 1024.0
    assert metrics["memory_percent"] == 12.5
    assert metrics["cpu_percent"] == 0.0
    assert metrics["storage"] == {
        "used_bytes": 0,
        "size_root_fs": 0,
        "used_mb": 0.0,
        "used_gb": 0.0,
    }
    assert metrics["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert metrics["status"] == "running"
    assert metrics["container_running"] is True
    assert isinstance(metrics["timestamp"], str)


def test_normalize_runtime_metrics_defaults_invalid_numbers() -> None:
    """Invalid provider numbers should not leak NaN or missing client fields."""
    metrics = normalize_runtime_metrics_snapshot(
        {
            "metrics": {
                "cpu_percent": math.nan,
                "memory_usage": "not-a-number",
                "memory_limit": -1,
                "memory_percent": float("inf"),
                "storage": {"used_bytes": math.nan, "used_mb": "bad"},
                "network": {"rx_bytes": -10, "tx_bytes": "bad"},
            },
        }
    )

    assert metrics is not None
    assert metrics["cpu_percent"] == 0.0
    assert metrics["memory_usage_mb"] == 0.0
    assert metrics["memory_limit_mb"] == 0.0
    assert metrics["memory_percent"] == 0.0
    assert metrics["storage"] == {
        "used_bytes": 0,
        "size_root_fs": 0,
        "used_mb": 0.0,
        "used_gb": 0.0,
    }
    assert metrics["network"] == {"rx_bytes": 0, "tx_bytes": 0}


def test_normalize_runtime_metrics_rejects_empty_direct_snapshot() -> None:
    """An empty provider snapshot is not a valid metrics sample."""
    assert normalize_runtime_metrics_snapshot({}) is None
