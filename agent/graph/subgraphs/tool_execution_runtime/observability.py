"""Observability helpers for tool-execution runtime extraction.

This module centralizes timestamp coercion and metric label/emit utilities
without changing existing metric names or exception behavior.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

from backend.services.metrics.utils import safe_gauge, safe_inc

_METRIC_LABEL_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def coerce_timestamp(value: Any) -> Optional[float]:
    """Best-effort conversion for incoming stage timestamps."""
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def sanitize_metric_label(raw_value: str) -> str:
    """Normalize metric labels to a stable `[a-z0-9_]` shape."""
    normalized = _METRIC_LABEL_SANITIZE_RE.sub("_", str(raw_value).lower()).strip("_")
    return normalized or "unknown"


def resolve_runtime_path_label(configurable: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    """Resolve stable warm/cold/unknown label for latency metric suffixes."""
    raw_path = configurable.get("runtime_path")
    if isinstance(raw_path, str):
        path_label = sanitize_metric_label(raw_path)
        if path_label in {"warm", "cold"}:
            return path_label
    runtime_warm = configurable.get("runtime_warm")
    if isinstance(runtime_warm, bool):
        return "warm" if runtime_warm else "cold"
    metadata_runtime_warm = metadata.get("runtime_warm")
    if isinstance(metadata_runtime_warm, bool):
        return "warm" if metadata_runtime_warm else "cold"
    return "unknown"


def emit_labeled_latency_metric(
    metric_name: str,
    value_ms: float,
    *,
    graph_name: Optional[str],
    runtime_path: str,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
) -> None:
    """Emit base latency metric plus stable graph/path label variants."""
    numeric_value = max(0.0, float(value_ms))
    graph_label = sanitize_metric_label(graph_name or "unknown")
    path_label = sanitize_metric_label(runtime_path or "unknown")
    gauge_fn(metric_name, numeric_value)
    gauge_fn(f"{metric_name}_graph_{graph_label}", numeric_value)
    gauge_fn(f"{metric_name}_path_{path_label}", numeric_value)


def record_compression_observability_metrics(
    *,
    source: str,
    fallback_reason: Optional[str],
    duration_seconds: float,
    compact_size_bytes: int,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
    inc_fn: Callable[[str], None] = safe_inc,
) -> None:
    """Record compact compression counters and histogram-like gauges."""
    gauge_fn("tool_output_compression_duration_seconds", max(0.0, float(duration_seconds)))
    gauge_fn("tool_output_compact_size_bytes", max(0, int(compact_size_bytes)))

    if source == "deterministic":
        inc_fn("tool_output_compression_fallback_total")
        reason_label = sanitize_metric_label(fallback_reason or "unknown")
        inc_fn(f"tool_output_compression_fallback_total_{reason_label}")
    else:
        inc_fn("tool_output_compression_success_total")


def emit_hitl_stage(
    *,
    stage: str,
    timestamp: Optional[float],
    task_id: Optional[int],
    logger: Any,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    """Emit standardized stage timestamps with HITL correlation identifiers."""
    if timestamp is None:
        return
    logger.info(
        "[HITL_TIMING] stage=%s task_id=%s interrupt_id=%s tool_call_id=%s ts=%.9f",
        stage,
        task_id if task_id is not None else "unknown",
        interrupt_id or "unknown",
        tool_call_id or "unknown",
        float(timestamp),
    )


def resolve_dr_iteration(metadata: Mapping[str, Any]) -> int:
    """Return current DR iteration index from metadata."""
    dr_meta = metadata.get("dr_iteration_meta")
    if isinstance(dr_meta, Mapping):
        active_iteration = dr_meta.get("active_iteration")
        if isinstance(active_iteration, int) and active_iteration > 0:
            return active_iteration
        counter = dr_meta.get("counter")
        if isinstance(counter, int) and counter > 0:
            return counter
    return 1


def record_batch_validation_metrics(
    *,
    candidate_count: int,
    committed_count: int,
    requested_strategy: str,
    effective_strategy: str,
    strategy_downgraded: bool,
    downgrade_reason: Optional[str],
    rejected_reason: Optional[str],
    inc_fn: Callable[[str], None] = safe_inc,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
) -> None:
    """Record batch validator + selector telemetry (Phase 8 Task 8.2).

    Surfaces ``candidate_count``/``committed_count`` gauges plus a
    ``downgrade_reason`` and ``validation_rejected_reason`` histogram
    (counter-per-label) so the operator can spot policy downgrades and
    repeated builder mistakes without grepping logs.
    """
    gauge_fn("batch_candidate_count", max(0.0, float(candidate_count)))
    gauge_fn("batch_committed_count", max(0.0, float(committed_count)))

    requested_label = sanitize_metric_label(requested_strategy)
    effective_label = sanitize_metric_label(effective_strategy)
    inc_fn(f"batch_requested_strategy_{requested_label}")
    inc_fn(f"batch_effective_strategy_{effective_label}")

    if strategy_downgraded:
        inc_fn("batch_strategy_downgraded_total")
        downgrade_label = sanitize_metric_label(downgrade_reason or "unknown")
        inc_fn(f"batch_downgrade_reason_{downgrade_label}")

    if rejected_reason:
        rejected_label = sanitize_metric_label(rejected_reason)
        inc_fn(f"batch_validation_rejected_reason_{rejected_label}")


def record_batch_aggregate_metrics(
    *,
    aggregate_status: str,
    duration_ms: float,
    inc_fn: Callable[[str], None] = safe_inc,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
) -> None:
    """Record batch-level aggregate status + duration (Phase 8 Task 8.2)."""
    status_label = sanitize_metric_label(aggregate_status)
    inc_fn(f"batch_aggregate_status_{status_label}")
    gauge_fn("batch_duration_ms", max(0.0, float(duration_ms)))


def record_per_call_metrics(
    *,
    tool_id: str,
    status: str,
    duration_ms: float,
    raw_result: Optional[Mapping[str, Any]] = None,
    inc_fn: Callable[[str], None] = safe_inc,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
) -> None:
    """Record per-tool-call status + duration (Phase 8 Task 8.2).

    Status counters distinguish ``failed`` / ``denied`` / ``cancelled`` so
    the per-call failure mix is observable without the underlying batch
    rollup.
    """
    status_label = sanitize_metric_label(status)
    tool_label = sanitize_metric_label(tool_id or "unknown")
    inc_fn(f"tool_call_status_{status_label}")
    inc_fn(f"tool_call_status_{tool_label}_{status_label}")
    gauge_fn("tool_call_duration_ms", max(0.0, float(duration_ms)))

    lane_label = "unknown"
    authority_label = "unknown"
    if isinstance(raw_result, Mapping):
        result_metadata = raw_result.get("metadata")
        if isinstance(result_metadata, Mapping):
            route_policy = result_metadata.get("route_policy")
            if isinstance(route_policy, Mapping):
                lane_candidate = route_policy.get("selected_lane")
                authority_candidate = route_policy.get("selected_authority")
                if isinstance(lane_candidate, str) and lane_candidate.strip():
                    lane_label = sanitize_metric_label(lane_candidate)
                if isinstance(authority_candidate, str) and authority_candidate.strip():
                    mapped_authority = {
                        "container_runner_transport": "runner",
                        "backend_direct": "cloud",
                        "artifact_direct": "data_plane",
                        "container_local_transport": "local",
                    }.get(authority_candidate.strip(), authority_candidate.strip())
                    authority_label = sanitize_metric_label(mapped_authority)

    inc_fn(f"tool_call_lane_{lane_label}")
    inc_fn(f"tool_call_authority_{authority_label}")
    inc_fn(f"tool_call_lane_authority_{lane_label}_{authority_label}")


def record_per_call_metrics_for_batch(
    result: Any,
    *,
    inc_fn: Callable[[str], None] = safe_inc,
    gauge_fn: Callable[[str, float], None] = safe_gauge,
) -> None:
    """Emit per-call metrics for every row in a ``BatchResult`` (Phase 1.2).

    Uniform single-site emission so that validator-rejected batches,
    full-denial batches, and normal-execution batches all produce one
    metric record per concrete call. ``result`` is duck-typed to anything
    with ``call_results`` iterable of objects exposing ``tool_id``,
    ``status`` (str-enum), and ``duration_ms``.
    """
    for call_result in getattr(result, "call_results", ()):
        status = getattr(call_result, "status", None)
        status_str = (
            status.value if hasattr(status, "value") else str(status or "unknown")
        )
        record_per_call_metrics(
            tool_id=str(getattr(call_result, "tool_id", "") or ""),
            status=status_str,
            duration_ms=float(getattr(call_result, "duration_ms", 0) or 0),
            raw_result=(
                raw_result
                if isinstance((raw_result := getattr(call_result, "raw_result", None)), Mapping)
                else None
            ),
            inc_fn=inc_fn,
            gauge_fn=gauge_fn,
        )
