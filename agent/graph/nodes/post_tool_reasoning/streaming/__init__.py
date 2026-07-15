"""Streaming adapters for post-tool reasoning.

This package provides capability-specific streaming adapters that handle
streaming events while keeping core logic capability-agnostic.
"""

from .base import StreamingAdapter
from .dr_adapter import DRStreamingAdapter
from .simple_adapter import SimpleStreamingAdapter
from .factory import StreamingAdapterFactory

__all__ = [
    "StreamingAdapter",
    "DRStreamingAdapter",
    "SimpleStreamingAdapter",
    "StreamingAdapterFactory",
]

