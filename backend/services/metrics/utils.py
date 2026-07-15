"""Safe metric helper wrappers around the global metrics singleton."""

from __future__ import annotations

from typing import Any


def safe_inc(name: str, value: int = 1) -> None:
    try:
        from backend.services.metrics import metrics
        metrics.inc(name, value)
    except Exception:
        pass


def safe_gauge(name: str, value: Any) -> None:
    try:
        from backend.services.metrics import metrics
        metrics.gauge(name, value)
    except Exception:
        pass


