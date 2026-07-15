"""Runtime-safe metrics helpers.

This module provides backend-free metric emitters that runtime-image modules can
import without introducing management-plane dependencies.
"""

from __future__ import annotations


def safe_gauge(_name: str, _value: float) -> None:
    """No-op gauge emitter for runtime-image-safe call sites."""
    return
