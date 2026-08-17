"""Classify runtime cancellation metadata for truthful chat lifecycle projection."""

from collections.abc import Mapping

_TERMINAL_PROCESS_STATES = frozenset(
    {
        "cancelled",
        "canceled",
        "completed",
        "failed",
        "terminated",
        "timed_out",
        "timeout",
    }
)


def supports_terminal_cancellation_projection(cancellation: object) -> bool:
    """Return whether cancellation metadata supports terminal lifecycle facts."""
    if not isinstance(cancellation, Mapping):
        return False
    process_state = str(cancellation.get("process_state") or "").strip().lower()
    if process_state in _TERMINAL_PROCESS_STATES:
        return True
    if process_state == "orphaned_until_terminal":
        return False
    return bool(cancellation.get("runtime_kill_supported")) and bool(
        cancellation.get("runtime_kill_attempted")
    )


__all__ = ["supports_terminal_cancellation_projection"]
