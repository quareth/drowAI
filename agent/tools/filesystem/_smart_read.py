"""Smart read mode detection for filesystem tools.

 -: Intelligent file reading based on file size.
 -: Consolidated pure Python line count (cross-platform).

This module re-exports smart read functionality from _helpers.py for
backward compatibility. All implementations are in _helpers.py."""

from __future__ import annotations

# Re-export from _helpers.py for backward compatibility
# All implementations consolidated in _helpers.py
from ._helpers import (
    # Smart read thresholds
    SMALL_FILE_LINE_THRESHOLD,
    MEDIUM_FILE_LINE_THRESHOLD,
    SMART_DEFAULT_HEAD_LINES,
    SMART_DEFAULT_TAIL_LINES,
    BYTE_READ_MODE_THRESHOLD,
    # Core functions
    get_line_count_python,
    get_file_size_bytes,
    # Smart read detection
    SmartReadResult,
    smart_read_mode_detection,
    resolve_read_mode_smart,
)

__all__ = [
    # Thresholds
    "SMALL_FILE_LINE_THRESHOLD",
    "MEDIUM_FILE_LINE_THRESHOLD",
    "SMART_DEFAULT_HEAD_LINES",
    "SMART_DEFAULT_TAIL_LINES",
    "BYTE_READ_MODE_THRESHOLD",
    # Core functions
    "get_line_count_python",
    "get_file_size_bytes",
    # Smart read detection
    "SmartReadResult",
    "smart_read_mode_detection",
    "resolve_read_mode_smart",
]
