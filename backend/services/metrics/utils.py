"""Safe metric helper wrappers around the global metrics singleton."""

from __future__ import annotations

import re
from typing import Any

_ALLOWED_METRIC_LABELS = (
    "connection_id",
    "deployment_id",
    "route_id",
    "status",
)
_LABEL_SEGMENT_RE = re.compile(r"[^a-z0-9_-]+")


def safe_inc(name: str, value: int = 1) -> None:
    try:
        from backend.services.metrics import metrics
        metrics.inc(name, value)
    except Exception:
        pass


def safe_inc_labeled(
    name: str,
    labels: dict[str, Any] | None = None,
    value: int = 1,
) -> None:
    """Increment a counter with allowlisted, sanitized name-label segments."""

    safe_inc(_labeled_metric_name(name, labels or {}), value)


def safe_gauge(name: str, value: Any) -> None:
    try:
        from backend.services.metrics import metrics
        metrics.gauge(name, value)
    except Exception:
        pass


def _labeled_metric_name(name: str, labels: dict[str, Any]) -> str:
    base = _metric_segment(name, default="metric", max_length=160, allow_dot=True)
    parts = [base]
    for key in _ALLOWED_METRIC_LABELS:
        if key not in labels:
            continue
        value = _metric_segment(labels[key], default="unknown", max_length=80)
        parts.extend((key, value))
    return ".".join(parts)


def _metric_segment(
    value: Any,
    *,
    default: str,
    max_length: int,
    allow_dot: bool = False,
) -> str:
    raw = str(value or "").strip().lower()
    if allow_dot:
        cleaned = re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("._-")
    else:
        cleaned = _LABEL_SEGMENT_RE.sub("_", raw).strip("_-")
    return (cleaned or default)[:max_length]

