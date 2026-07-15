"""Enumerations and schemas for LangGraph streaming events."""

from __future__ import annotations

from enum import Enum


class StreamEventType(str, Enum):
    """Core event types used across LangGraph streaming paths."""

    MESSAGE_START = "message_start"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_END = "message_end"
    REASONING_START = "reasoning_start"
    REASONING_DELTA = "reasoning_delta"
    REASONING_END = "reasoning_end"
    TOOL_START = "tool_start"
    TOOL_DELTA = "tool_delta"
    TOOL_END = "tool_end"
    CITATION_START = "citation_start"
    CITATION_END = "citation_end"


__all__ = ["StreamEventType"]
