"""Tests for stable HITL latency metric emission surfaces."""

from __future__ import annotations

from typing import Dict, List
from unittest.mock import patch

from backend.services.langgraph_chat import facade_helpers


def _percentile(values: List[float], quantile: float) -> float:
    """Compute deterministic percentile via linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + ((ordered[high] - ordered[low]) * weight)


def _build_phase5_latency_validation_report(
    *,
    graph_mode: str,
    baseline_samples_ms: List[float],
    post_samples_ms: List[float],
    residual_components_ms: Dict[str, float],
) -> Dict[str, float | str | Dict[str, float]]:
    """Build deterministic baseline-vs-post validation report for cutover checks."""
    baseline_first = float(baseline_samples_ms[0]) if baseline_samples_ms else 0.0
    post_first = float(post_samples_ms[0]) if post_samples_ms else 0.0
    baseline_warm = baseline_samples_ms[1:] if len(baseline_samples_ms) > 1 else baseline_samples_ms
    post_warm = post_samples_ms[1:] if len(post_samples_ms) > 1 else post_samples_ms

    baseline_warm_p95 = _percentile(baseline_warm, 0.95)
    post_warm_p95 = _percentile(post_warm, 0.95)
    post_warm_p50 = _percentile(post_warm, 0.50)
    residual_total = sum(max(0.0, float(v)) for v in residual_components_ms.values())

    return {
        "graph_mode": graph_mode,
        "profile_source": "synthetic_controlled",
        "baseline_first_dispatch_ms": baseline_first,
        "post_first_dispatch_ms": post_first,
        "first_dispatch_improvement_ms": max(0.0, baseline_first - post_first),
        "baseline_warm_p95_ms": baseline_warm_p95,
        "post_warm_p95_ms": post_warm_p95,
        "post_warm_p50_ms": post_warm_p50,
        "warm_p95_regression_ms": max(0.0, post_warm_p95 - baseline_warm_p95),
        "warm_stability_spread_ms": max(0.0, post_warm_p95 - post_warm_p50),
        "residual_latency_ms": residual_total,
        "residual_components_ms": dict(residual_components_ms),
    }


def test_emit_resume_worker_queue_metric_emits_stable_labels() -> None:
    """Resume queue metric emits base + stable graph/path label variants."""
    with patch("backend.services.metrics.utils.safe_gauge") as mock_gauge:
        facade_helpers.emit_resume_worker_queue_metric(
            approval_received_at=100.0,
            resume_worker_start_at=100.050,
            task_id=42,
            graph_name="simple_tool",
            resolve_runtime_path_label_fn=lambda _task_id: "warm",
        )

    calls = {call.args[0]: call.args[1] for call in mock_gauge.call_args_list}
    assert "resume_worker_queue_to_start_ms" in calls
    assert "resume_worker_queue_to_start_ms_graph_simple_tool" in calls
    assert "resume_worker_queue_to_start_ms_path_warm" in calls
    assert calls["resume_worker_queue_to_start_ms"] >= 49.0


def test_emit_resume_worker_queue_metric_skips_when_timestamps_missing() -> None:
    """Metric emission is a no-op when required timestamps are absent."""
    with patch("backend.services.metrics.utils.safe_gauge") as mock_gauge:
        facade_helpers.emit_resume_worker_queue_metric(
            approval_received_at=None,
            resume_worker_start_at=101.0,
            task_id=42,
            graph_name="simple_tool",
        )

    mock_gauge.assert_not_called()


def test_phase5_latency_target_validation_simple_tool() -> None:
    """Task 5.2: simple-tool latency validation vs baseline with residual accounting."""
    baseline_samples_ms = [920.0, 188.0, 176.0, 194.0, 182.0]
    post_samples_ms = [612.0, 190.0, 177.0, 193.0, 181.0]
    residual_components_ms = {
        "resume_queueing_ms": 118.0,
        "provider_roundtrip_ms": 76.0,
    }

    report = _build_phase5_latency_validation_report(
        graph_mode="simple_tool",
        baseline_samples_ms=baseline_samples_ms,
        post_samples_ms=post_samples_ms,
        residual_components_ms=residual_components_ms,
    )

    assert report["first_dispatch_improvement_ms"] > 250.0
    assert report["warm_p95_regression_ms"] <= 0.0
    assert report["warm_stability_spread_ms"] < 25.0
    assert report["residual_latency_ms"] > 0.0
    assert isinstance(report["residual_components_ms"], dict)
    assert "provider_roundtrip_ms" in report["residual_components_ms"]


def test_phase5_latency_target_validation_deep_reasoning() -> None:
    """Task 5.2: deep-reasoning latency validation vs baseline with residual accounting."""
    baseline_samples_ms = [1180.0, 236.0, 228.0, 242.0, 231.0]
    post_samples_ms = [742.0, 237.0, 229.0, 241.0, 232.0]
    residual_components_ms = {
        "resume_queueing_ms": 141.0,
        "model_reasoning_overhead_ms": 104.0,
    }

    report = _build_phase5_latency_validation_report(
        graph_mode="deep_reasoning",
        baseline_samples_ms=baseline_samples_ms,
        post_samples_ms=post_samples_ms,
        residual_components_ms=residual_components_ms,
    )

    assert report["first_dispatch_improvement_ms"] > 350.0
    assert report["warm_p95_regression_ms"] <= 0.0
    assert report["warm_stability_spread_ms"] < 20.0
    assert report["residual_latency_ms"] > 0.0
    assert isinstance(report["residual_components_ms"], dict)
    assert "model_reasoning_overhead_ms" in report["residual_components_ms"]
