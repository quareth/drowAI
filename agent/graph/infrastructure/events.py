"""Typed events emitted by graph nodes during execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .stream_schema import StreamEventType


@dataclass(slots=True)
class GraphEvent:
    """Base representation for events emitted to streaming adapters."""

    type: StreamEventType
    content: str
    metadata: Dict[str, object]


@dataclass(slots=True)
class ToolEvent(GraphEvent):
    """Event representing a tool execution result."""

    tool_name: Optional[str] = None
    status: Optional[str] = None


@dataclass(slots=True)
class ReasoningEvent(GraphEvent):
    """Event capturing intermediate reasoning output."""

    reasoning_type: str = "thought"


__all__ = ["GraphEvent", "ToolEvent", "ReasoningEvent"]
