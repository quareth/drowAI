"""Adapter translating internal graph events to facade events."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..infrastructure.events import GraphEvent


class GraphStreamingAdapter:
    """Placeholder streaming adapter; mirrors backend/services LangGraph adapter."""

    def build_events(self, raw_events: Iterable[GraphEvent]) -> List[Dict[str, Any]]:
        """Convert internal graph events to dicts understood by the backend."""

        return [
            {
                "type": event.type.value,
                "content": event.content,
                "metadata": event.metadata,
            }
            for event in raw_events
        ]


__all__ = ["GraphStreamingAdapter"]
