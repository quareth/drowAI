"""Safe metrics emission helpers for retention maintenance runs.

This module converts retention run results into flat in-process metric names.
Metric names intentionally omit tenant ids and never include executor payloads,
object keys, prompt text, transcripts, or exception messages.
"""

from __future__ import annotations

import re

from backend.services.metrics.utils import safe_gauge, safe_inc
from backend.services.retention.contracts import (
    RetentionExecutorResult,
    RetentionRunResult,
    normalize_reason_code,
    validate_retention_class,
    validate_run_mode,
    validate_safe_identifier,
)


_DEFAULT_FAILURE_CODE = "retention_executor_failed"
_METRIC_PART_PATTERN = re.compile(r"[^a-z0-9_]+")


def emit_retention_run_metrics(
    result: RetentionRunResult,
    *,
    duration_seconds: float,
) -> None:
    """Emit safe aggregate counters and duration for one retention run."""

    mode = _metric_part(validate_run_mode(result.mode))
    status = "succeeded" if result.succeeded else "failed"
    failed_executors = sum(1 for item in result.results if not item.succeeded)

    safe_inc("retention.run.total")
    safe_inc(f"retention.run.{mode}.{status}")
    safe_inc("retention.run.executors", len(result.results))
    if failed_executors:
        safe_inc("retention.run.failed_executors", failed_executors)
    safe_gauge("retention.run.duration_seconds", _duration_value(duration_seconds))


def emit_retention_executor_metrics(
    result: RetentionExecutorResult,
    *,
    duration_seconds: float,
) -> None:
    """Emit safe counters and duration for one executor result."""

    executor_name = _metric_part(validate_safe_identifier(result.executor_name))
    retention_class = _metric_part(validate_retention_class(result.retention_class))
    mode = _metric_part(validate_run_mode(result.mode))
    status = "succeeded" if result.succeeded else "failed"

    safe_inc("retention.executor.runs")
    safe_inc(f"retention.executor.{executor_name}.runs")
    safe_inc(f"retention.executor.{executor_name}.{mode}.{status}")
    safe_gauge(
        f"retention.executor.{executor_name}.duration_seconds",
        _duration_value(duration_seconds),
    )

    _emit_count(
        executor_name=executor_name,
        retention_class=retention_class,
        metric_name="candidates",
        value=result.counts.candidate_count,
    )
    _emit_count(
        executor_name=executor_name,
        retention_class=retention_class,
        metric_name="applied",
        value=result.counts.applied_count,
    )
    _emit_count(
        executor_name=executor_name,
        retention_class=retention_class,
        metric_name="protected",
        value=result.counts.protected_count,
    )
    _emit_count(
        executor_name=executor_name,
        retention_class=retention_class,
        metric_name="failures",
        value=result.counts.failed_count,
    )
    if not result.succeeded or result.error_code:
        error_code = _metric_part(
            normalize_reason_code(result.error_code or _DEFAULT_FAILURE_CODE)
        )
        safe_inc(f"retention.executor.{executor_name}.failure.{error_code}")


def _emit_count(
    *,
    executor_name: str,
    retention_class: str,
    metric_name: str,
    value: int,
) -> None:
    normalized_value = int(value)
    if normalized_value <= 0:
        return
    safe_inc(f"retention.executor.{executor_name}.{metric_name}", normalized_value)
    safe_inc(f"retention.class.{retention_class}.{metric_name}", normalized_value)


def _metric_part(value: str) -> str:
    normalized = _METRIC_PART_PATTERN.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise ValueError("metric name part cannot be empty")
    return normalized


def _duration_value(duration_seconds: float) -> float:
    return max(0.0, float(duration_seconds))


__all__ = [
    "emit_retention_executor_metrics",
    "emit_retention_run_metrics",
]
