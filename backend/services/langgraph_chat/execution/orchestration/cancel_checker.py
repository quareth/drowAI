"""Cancellation checker factory for resume and checkpoint-retry execution loops.

This module hosts the throttled ``should_cancel`` callback logic extracted
from ``turn_execution_service.py`` and used by resume/checkpoint-retry flows.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional


def build_cancel_checker(
    lifecycle: Any,
    *,
    task_id: int,
    lifecycle_turn_id: Optional[str],
    throttle_seconds: float = 0.25,
) -> Callable[[], bool]:
    """Create a throttled callback that checks run cancellation state."""
    cancel_cached = False
    checked_at = 0.0

    def _should_cancel() -> bool:
        nonlocal cancel_cached, checked_at
        if cancel_cached:
            return True
        if lifecycle_turn_id is None:
            return False
        now = time.monotonic()
        if now - checked_at < throttle_seconds:
            return False
        checked_at = now
        cancel_cached = lifecycle.is_cancel_requested(task_id=task_id, turn_id=lifecycle_turn_id)
        return cancel_cached

    return _should_cancel
