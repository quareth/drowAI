"""Simple-tool adapter wrapper around shared observation streaming logic."""

from __future__ import annotations

from .base import StreamingAdapter


class SimpleStreamingAdapter(StreamingAdapter):
    """Streaming adapter for simple tool execution capability."""

    def __init__(self) -> None:
        super().__init__(
            usage_source="post_tool_reasoning_simple",
            log_prefix="SIMPLE_STREAMING",
        )


__all__ = ["SimpleStreamingAdapter"]
