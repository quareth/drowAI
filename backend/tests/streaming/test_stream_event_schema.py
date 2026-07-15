from backend.services.streaming.stream_event_schema import (
    Placement,
    normalize_stream_event,
    normalize_stream_packet,
)
from agent.graph.contracts.streaming_constants import (
    REASONING_PHASE_INDEX,
    STEP_REASONING_DELTA,
    STEP_RETRY_ATTEMPT,
)


def test_normalize_stream_event_defaults_reasoning_delta():
    event = {
        "type": "reasoning_delta",
        "content": "Thinking...",
        "metadata": {"conversation_id": "conv-1", "id": "turn-1"},
    }

    normalized = normalize_stream_event(event)
    assert normalized is not None
    metadata = normalized["metadata"]
    assert metadata["conversationId"] == "conv-1"
    assert metadata["step_type"] == STEP_REASONING_DELTA
    assert metadata["ind"] == REASONING_PHASE_INDEX
    assert metadata["streaming"] is True


def test_normalize_stream_event_preserves_streaming_false():
    event = {
        "type": "tool_end",
        "content": "Tool completed",
        "metadata": {"streaming": False},
    }

    normalized = normalize_stream_event(event)
    assert normalized is not None
    metadata = normalized["metadata"]
    assert metadata["streaming"] is False


def test_normalize_stream_event_coerces_sequence():
    event = {
        "type": "assistant_message",
        "content": "Done",
        "sequence": "42",
        "metadata": {},
    }

    normalized = normalize_stream_event(event)
    assert normalized is not None
    assert normalized["sequence"] == 42
    assert normalized["metadata"]["sequence"] == 42
    assert normalized["metadata"]["turn_sequence"] == 42


def test_normalize_retry_attempt_defaults_to_reasoning_phase():
    event = {
        "type": "retry_attempt",
        "content": "Retrying with alternative approach (attempt 2)",
        "metadata": {"conversation_id": "conv-1", "id": "turn-1"},
    }

    normalized = normalize_stream_event(event)
    assert normalized is not None
    metadata = normalized["metadata"]
    assert metadata["step_type"] == STEP_RETRY_ATTEMPT
    assert metadata["ind"] == REASONING_PHASE_INDEX


def test_normalize_stream_event_coerces_metadata_error_to_string():
    event = {
        "type": "message_delta",
        "content": "chunk",
        "metadata": {"error": True},
    }

    normalized = normalize_stream_event(event)
    assert normalized is not None
    assert normalized["metadata"]["error"] == "True"


def test_normalize_stream_packet_fallback_is_json_safe():
    event = {
        "placement": Placement(turn_index=1, tab_index=2),
        "obj": {
            "type": "message_delta",
            "content": "chunk",
            "metadata": {"error": True},
        },
    }

    normalized = normalize_stream_packet(event, task_id=7)
    assert normalized is not None
    assert isinstance(normalized["placement"], dict)
    assert normalized["placement"]["turn_index"] == 1


def test_normalize_stream_event_accepts_clarify_graph_interrupt():
    event = {
        "type": "graph_interrupt",
        "content": "",
        "interrupt_id": "deep_reasoning:checkpoint:cp-clarify-1",
        "checkpoint_id": "cp-clarify-1",
        "interrupt_type": "clarify_request",
        "graph_name": "deep_reasoning",
        "payload": {
            "type": "clarify_request",
            "questions": [
                {
                    "question_id": "target",
                    "input_type": "select",
                    "label": "What host should I scan?",
                    "options": ["10.0.0.1", "10.0.0.2"],
                    "required": True,
                }
            ],
        },
        "metadata": {"conversation_id": "conv-clarify", "id": "turn-clarify-1"},
    }

    normalized = normalize_stream_event(event, task_id=123)
    assert normalized is not None
    assert normalized["type"] == "graph_interrupt"
    assert normalized["interrupt_type"] == "clarify_request"
    assert normalized["task_id"] == 123
