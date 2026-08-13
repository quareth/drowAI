"""Own shell-session timeout defaults and bounded timeout calculations.

The helpers are backend-free so agent planning and backend enforcement apply
the same limits. Idle cleanup remains distinct from process and interaction
deadlines even though their defaults are centralized here.
"""

from __future__ import annotations

import math


DEFAULT_TOOL_TIMEOUT_SECONDS = 600.0

SHELL_SESSION_DEFAULT_YIELD_TIME_MS = 10_000
SHELL_SESSION_MAX_YIELD_TIME_MS = 30_000
SHELL_SESSION_PREPARATION_TIMEOUT_SEC = 8.0
SHELL_SESSION_CONTROL_TIMEOUT_SEC = 5.0
SHELL_SESSION_CLEANUP_TIMEOUT_SEC = 12.0
SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC = 120
SHELL_SESSION_MAX_RUNTIME_SEC = 1_800

SHELL_SESSION_DEFAULT_IDLE_TIMEOUT_SEC = 300
SHELL_SESSION_DEFAULT_CLEANUP_INTERVAL_SEC = 60
SHELL_SESSION_DEFAULT_TERMINATION_GRACE_SEC = 1
SHELL_SESSION_DEFAULT_TERMINAL_IO_GRACE_SEC = 2
SHELL_SESSION_DEFAULT_OUTPUT_QUIESCENCE_SEC = 0.05
SHELL_SESSION_DEFAULT_INITIAL_QUIET_WINDOW_SEC = 0.25


def shell_preparation_timeout_sec(
    *,
    tool_timeout_max_seconds: float,
    maximum_seconds: float = SHELL_SESSION_PREPARATION_TIMEOUT_SEC,
) -> float:
    """Reserve preparation time while leaving one second for process runtime."""
    return max(
        0.0,
        min(float(maximum_seconds), float(tool_timeout_max_seconds) - 1.0),
    )


def clamp_shell_runtime_sec(
    requested_runtime_sec: float | int | None,
    *,
    tool_timeout_max_seconds: float,
    preparation_seconds: float | None = None,
) -> int:
    """Clamp process lifetime so preparation plus runtime fits the tool maximum."""
    if preparation_seconds is None:
        preparation_seconds = shell_preparation_timeout_sec(
            tool_timeout_max_seconds=tool_timeout_max_seconds,
        )
    runtime_cap = math.floor(
        min(
            float(SHELL_SESSION_MAX_RUNTIME_SEC),
            float(tool_timeout_max_seconds) - preparation_seconds,
        )
    )
    requested = (
        float(SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC)
        if requested_runtime_sec is None
        else float(requested_runtime_sec)
    )
    return max(1, min(int(math.ceil(requested)), runtime_cap))


def clamp_shell_yield_time_ms(
    requested_yield_time_ms: float | int | None,
    *,
    reserved_seconds: float,
    tool_timeout_max_seconds: float,
) -> int:
    """Clamp one interaction wait to its shell and whole-operation ceilings."""
    requested = (
        float(SHELL_SESSION_DEFAULT_YIELD_TIME_MS)
        if requested_yield_time_ms is None
        else max(0.0, float(requested_yield_time_ms))
    )
    available_ms = math.floor(
        max(0.0, float(tool_timeout_max_seconds) - float(reserved_seconds)) * 1000
    )
    return min(int(requested), SHELL_SESSION_MAX_YIELD_TIME_MS, available_ms)


def shell_control_timeout_sec(*, tool_timeout_max_seconds: float) -> float:
    """Return the bounded control-write portion of one shell interaction."""
    return min(SHELL_SESSION_CONTROL_TIMEOUT_SEC, float(tool_timeout_max_seconds))
