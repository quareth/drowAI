"""Reconnect backoff math and redacted reconnect reason formatting.

Pure functions only; no I/O and no state. Imports backoff defaults from
``constants`` and the log redactor from ``runtime_shared``.
"""

from __future__ import annotations

from drowai_runner.control_channel.constants import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_JITTER_RATIO,
    BACKOFF_MAX_SECONDS,
)
from runtime_shared.runner_protocol import sanitize_log_message


def format_reconnect_error_reason(exc: Exception) -> str:
    """Return a single-line, redacted exception message safe for reconnect logs."""
    return sanitize_log_message(str(exc))


def compute_reconnect_delay_seconds(
    *,
    attempt: int,
    random_fraction: float,
    base_seconds: float = BACKOFF_BASE_SECONDS,
    max_seconds: float = BACKOFF_MAX_SECONDS,
    jitter_ratio: float = BACKOFF_JITTER_RATIO,
) -> float:
    """Return bounded exponential backoff with positive jitter."""
    safe_attempt = max(1, int(attempt))
    bounded_base = min(max_seconds, base_seconds * (2 ** (safe_attempt - 1)))
    jitter_window = max(0.0, bounded_base * max(0.0, float(jitter_ratio)))
    bounded_random = max(0.0, min(float(random_fraction), 1.0))
    return min(max_seconds, bounded_base + (jitter_window * bounded_random))
