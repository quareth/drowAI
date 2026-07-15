"""Unit tests for streaming adapter event processing."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


def test_process_message_start():
    """Test processing message_start event."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "message_start",
        "conversation_id": "conv1",
        "turn_id": "turn1",
        "timestamp": time.time(),
    }
    
    processed = adapter.process_streaming_event(event)
    
    assert processed is not None
    assert processed["type"] == "message_start"
    assert processed["metadata"]["subtype"] == "message_start"
    assert processed["metadata"]["source"] == "langgraph_stream"
    assert "timestamp" in processed["metadata"]


def test_process_message_delta():
    """Test processing message_delta event."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "message_delta",
        "content": "Hello world",
        "conversation_id": "conv1",
        "turn_id": "turn1",
    }
    
    with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
        processed = adapter.process_streaming_event(event)
        
        assert processed is not None
        assert processed["content"] == "Hello world"
        assert processed["metadata"]["source"] == "langgraph_stream"
        
        # Should increment metrics
        mock_inc.assert_called_with("langgraph_stream_deltas_processed")


def test_process_message_delta_missing_content():
    """Test message_delta without content is filtered."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "message_delta",
        "conversation_id": "conv1",
        "turn_id": "turn1",
    }
    
    processed = adapter.process_streaming_event(event)
    assert processed is None


def test_process_section_end():
    """Test processing section_end event."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "section_end",
        "section_name": "synthesis",
    }
    
    processed = adapter.process_streaming_event(event)
    
    assert processed is not None
    assert processed["type"] == "section_end"
    assert processed["metadata"]["section_name"] == "synthesis"
    assert processed["content"] == "[Section complete: synthesis]"


def test_process_section_end_default():
    """Test section_end uses default section_name."""
    adapter = LangGraphStreamingAdapter()
    
    event = {"type": "section_end"}
    
    processed = adapter.process_streaming_event(event)
    
    assert processed is not None
    assert processed["metadata"]["section_name"] == "final_answer"


def test_process_stream_error():
    """Test processing stream_error event."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "stream_error",
        "error": "Connection timeout",
        "recoverable": True,
        "details": {"retry_after": 5},
    }
    
    with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
        processed = adapter.process_streaming_event(event)
        
        assert processed is not None
        assert processed["type"] == "stream_error"
        assert processed["metadata"]["error"] == "Connection timeout"
        assert processed["metadata"]["recoverable"] is True
        assert processed["metadata"]["details"] == {"retry_after": 5}
        
        # Should increment metrics
        mock_inc.assert_called_with("langgraph_stream_errors_processed")


def test_process_unknown_event_type():
    """Test unknown event types are filtered with debug log."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "type": "unknown_type",
        "data": "something",
    }
    
    processed = adapter.process_streaming_event(event)
    assert processed is None


def test_process_event_missing_type():
    """Test events without type field are filtered."""
    adapter = LangGraphStreamingAdapter()
    
    event = {
        "content": "Hello",
        "conversation_id": "conv1",
    }
    
    processed = adapter.process_streaming_event(event)
    assert processed is None


def test_event_enrichment_timestamp():
    """Test all processed events get timestamp enrichment."""
    adapter = LangGraphStreamingAdapter()
    
    events = [
        {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
        {"type": "message_delta", "content": "x", "conversation_id": "c1", "turn_id": "t1"},
        {"type": "section_end"},
        {"type": "stream_error", "error": "test", "recoverable": True},
    ]
    
    for event in events:
        processed = adapter.process_streaming_event(event)
        if processed:
            assert "timestamp" in processed.get("metadata", {})

