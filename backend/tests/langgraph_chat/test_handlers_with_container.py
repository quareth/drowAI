"""Tests for handlers with ChatStateContainer (: State Container & Handler Integration)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


def test_adapter_accumulates_answer_in_container():
    """Streaming adapter appends message_delta content to state container."""
    adapter = LangGraphStreamingAdapter()
    container = ChatStateContainer()
    event = {
        "type": "message_delta",
        "content": "Hello ",
        "conversation_id": "c1",
        "turn_id": "t1",
    }
    adapter.process_streaming_event(event, state_container=container)
    event2 = {"type": "message_delta", "content": "world", "conversation_id": "c1", "turn_id": "t1"}
    adapter.process_streaming_event(event2, state_container=container)
    assert container.get_answer_tokens() == "Hello world"


def test_adapter_accumulates_reasoning_in_container():
    """Streaming adapter appends reasoning_delta content to state container."""
    adapter = LangGraphStreamingAdapter()
    container = ChatStateContainer()
    adapter.process_streaming_event(
        {
            "type": "reasoning_start",
            "conversation_id": "c1",
            "turn_id": "t1",
            "step": "thinking",
        },
        state_container=container,
    )
    event = {
        "type": "reasoning_delta",
        "content": "Thinking... ",
        "conversation_id": "c1",
        "turn_id": "t1",
    }
    adapter.process_streaming_event(event, state_container=container)
    event2 = {
        "type": "reasoning_delta",
        "content": "done.",
        "conversation_id": "c1",
        "turn_id": "t1",
    }
    adapter.process_streaming_event(event2, state_container=container)
    adapter.process_streaming_event(
        {
            "type": "reasoning_section_end",
            "conversation_id": "c1",
            "turn_id": "t1",
            "section_name": "thinking",
        },
        state_container=container,
    )
    assert container.get_reasoning_tokens() == "Thinking... done."


def test_adapter_accumulates_tool_call_on_tool_end():
    """Streaming adapter adds tool call to state container on tool_end."""
    adapter = LangGraphStreamingAdapter()
    container = ChatStateContainer()
    event = {
        "type": "tool_end",
        "tool": "nmap",
        "conversation_id": "c1",
        "turn_id": "t1",
        "status": "success",
        "duration": 1.0,
        "parameters": {"target": "host"},
        "summary": {"output": "done"},
    }
    adapter.process_streaming_event(event, state_container=container)
    calls = container.get_tool_calls()
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "nmap"
    # ToolCall.tool_id is integer-backed; adapter stores name under tool_name.
    assert calls[0]["tool_id"] is None
    assert calls[0]["tool_arguments"] == {"target": "host"}


def test_adapter_without_container_still_returns_processed_event():
    """Adapter works without state_container (backward compatible)."""
    adapter = LangGraphStreamingAdapter()
    event = {
        "type": "message_delta",
        "content": "Hi",
        "conversation_id": "c1",
        "turn_id": "t1",
    }
    processed = adapter.process_streaming_event(event)
    assert processed is not None
    assert processed["content"] == "Hi"
