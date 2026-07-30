"""Parse streaming Responses API events for the GPT-5 provider.

This module owns provider-local event inspection for streamed text deltas and
done-event detection used by usage-capturing streaming flows.
"""

from __future__ import annotations

import logging
from typing import Any, Optional


def extract_stream_delta(event: Any, logger: logging.Logger) -> Optional[str]:
    """Extract text delta from a streaming event."""
    try:
        event_type = getattr(event, "type", None)
        event_type_str = str(event_type).lower() if event_type else ""

        if "refusal" in event_type_str:
            return None

        if "output_text.delta" in event_type_str or "textdelta" in event_type_str:
            delta = getattr(event, "delta", None)
            if delta is not None:
                return str(delta) if delta else None

        if "content_part.delta" in event_type_str:
            delta = getattr(event, "delta", None)
            if delta is not None:
                return str(delta) if delta else None

        if "delta" in event_type_str and "done" not in event_type_str:
            for attr in ["delta", "text", "content"]:
                val = getattr(event, attr, None)
                if val is not None and isinstance(val, str):
                    return val if val else None

    except (AttributeError, TypeError) as exc:
        logger.debug(f"Failed to extract stream delta: {exc}")

    return None


def extract_output_text_delta(event: Any) -> Optional[str]:
    """Return only documented visible assistant-text delta events.

    Tool argument events also carry a string ``delta`` field. Callers that mix
    text streaming with tool calls must use this strict extractor so internal
    JSON never enters the visible output channel.
    """
    event_type = str(getattr(event, "type", "") or "").strip().lower()
    if event_type != "response.output_text.delta":
        return None
    delta = getattr(event, "delta", None)
    return str(delta) if delta else None


def is_done_event(event: Any) -> bool:
    """Return whether a stream event represents a completed response."""
    event_type = str(getattr(event, "type", "")).lower()
    return "response.done" in event_type or "completed" in event_type
