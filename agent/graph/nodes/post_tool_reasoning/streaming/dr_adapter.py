"""DR-specific adapter wrapper around shared observation streaming logic."""

from __future__ import annotations

from .base import StreamingAdapter


class DRStreamingAdapter(StreamingAdapter):
    """Streaming adapter for deep reasoning capability."""

    def __init__(self) -> None:
        super().__init__(
            usage_source="post_tool_reasoning_dr",
            log_prefix="DR_STREAMING",
        )


__all__ = ["DRStreamingAdapter"]
