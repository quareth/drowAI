"""Tests for runtime prior-turn reference materialization.

The materializer must resolve classifier hints against canonical
``ChatMessage`` rows only. Classifier anchors remain resolver metadata,
not transcript truth.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
    empty_prior_turn_references_context,
)
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.intent.prior_turn_references import (
    METADATA_KEY_PRIOR_TURN_REFERENCES,
    PriorTurnReferenceMaterializer,
)
from backend.services.langgraph_chat.routing.selectors import ChatBranch


def _row(
    *,
    message_id: int,
    message_type: str,
    message: str,
    turn_number: int,
) -> SimpleNamespace:
    """Build a minimal ChatMessage-like row for materializer tests."""
    return SimpleNamespace(
        id=message_id,
        message_type=message_type,
        message=message,
        turn_number=turn_number,
    )


def _reference(hints: List[Dict[str, Any]], *, operation: str = "reference_resolution") -> Dict[str, Any]:
    """Build a normalized prior-turn reference payload."""
    return {
        "required": True,
        "operation": operation,
        "status": "resolved",
        "confidence": 0.9,
        "hints": hints,
    }


def test_materializer_resolves_rendered_turn_hint_to_canonical_row() -> None:
    rows = [
        _row(message_id=1, message_type="user", message="What should we capture?", turn_number=10),
        _row(
            message_id=2,
            message_type="assistant",
            message="Capture packets from that traffic.",
            turn_number=10,
        ),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "rendered_turn",
                "turn_number": 1,
                "speaker": "assistant",
                "anchor_text": "Capture packets from that traffic.",
                "reason": "The user asks about the prior assistant phrase.",
                "confidence": 0.91,
            }
        ]
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=rows,
    )

    assert result["status"] == "ok"
    assert result["unresolved_hints"] == []
    assert result["materialized_turns"] == [
        {
            "turn_number": 10,
            "rendered_turn_number": 1,
            "speaker": "user",
            "message_id": 1,
            "text": "What should we capture?",
            "matched_by": "rendered_turn",
            "classifier_confidence": 0.91,
        },
        {
            "turn_number": 10,
            "rendered_turn_number": 1,
            "speaker": "assistant",
            "message_id": 2,
            "text": "Capture packets from that traffic.",
            "matched_by": "rendered_turn",
            "classifier_confidence": 0.91,
        },
    ]


def test_materializer_uses_classifier_prompt_history_for_rendered_turn_numbers() -> None:
    rows = [
        _row(message_id=1, message_type="user", message="What traffic?", turn_number=1),
        _row(
            message_id=2,
            message_type="assistant",
            message="Capture packets from that traffic.",
            turn_number=1,
        ),
        _row(message_id=3, message_type="user", message="What traffic?", turn_number=10),
        _row(
            message_id=4,
            message_type="assistant",
            message="Capture packets from that traffic.",
            turn_number=10,
        ),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "rendered_turn",
                "turn_number": 1,
                "speaker": "assistant",
                "anchor_text": "Capture packets from that traffic.",
                "reason": "The classifier saw this as rendered turn 1 after summary shaping.",
                "confidence": 0.91,
            }
        ]
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=rows,
        prompt_messages=[
            {"role": "system", "content": "Earlier conversation summary."},
            {"role": "user", "content": "What traffic?"},
            {"role": "assistant", "content": "Capture packets from that traffic."},
        ],
    )

    assert result["status"] == "ok"
    assert [item["message_id"] for item in result["materialized_turns"]] == [3, 4]
    assert [item["turn_number"] for item in result["materialized_turns"]] == [10, 10]
    assert [item["rendered_turn_number"] for item in result["materialized_turns"]] == [1, 1]


def test_materializer_marks_ambiguous_anchor_unresolved() -> None:
    rows = [
        _row(message_id=1, message_type="assistant", message="Run the scan next.", turn_number=1),
        _row(message_id=2, message_type="assistant", message="Run the scan next.", turn_number=2),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "anchor_text",
                "turn_number": None,
                "speaker": "assistant",
                "anchor_text": "Run the scan next.",
                "reason": "Two assistant turns contain the same anchor.",
                "confidence": 0.6,
            }
        ]
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=rows,
    )

    assert result["status"] == "unresolved"
    assert result["materialized_turns"] == []
    assert result["unresolved_hints"][0]["status"] == "ambiguous"


def test_materializer_resolves_continuation_of_specific_prior_user_request() -> None:
    rows = [
        _row(message_id=1, message_type="user", message="Run nikto against the web app.", turn_number=1),
        _row(message_id=2, message_type="assistant", message="I can do that next.", turn_number=1),
        _row(message_id=3, message_type="user", message="Revise that request to use nuclei.", turn_number=2),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "anchor_text",
                "turn_number": None,
                "speaker": "user",
                "anchor_text": "Run nikto against the web app.",
                "reason": "The current user asks to revise the prior request.",
                "confidence": 0.84,
            }
        ],
        operation="revision",
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=rows,
    )

    assert result["operation"] == "revision"
    assert result["status"] == "ok"
    assert [item["speaker"] for item in result["materialized_turns"]] == [
        "user",
        "assistant",
    ]
    assert result["materialized_turns"][0]["text"] == "Run nikto against the web app."
    assert result["materialized_turns"][1]["text"] == "I can do that next."


def test_materializer_marks_unknown_required_reference_unresolved() -> None:
    reference = _reference(
        [
            {
                "reference_kind": "unknown",
                "turn_number": None,
                "speaker": "unknown",
                "anchor_text": None,
                "reason": "Prior-turn dependence is present but no target is known.",
                "confidence": 0.42,
            }
        ],
        operation="continuation",
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=[
            _row(message_id=1, message_type="assistant", message="Prior text.", turn_number=1)
        ],
    )

    assert result["status"] == "unresolved"
    assert result["materialized_turns"] == []
    assert result["unresolved_hints"][0]["status"] == "unresolved"
    assert result["unresolved_hints"][0]["reference_kind"] == "unknown"


def test_materializer_marks_bad_rendered_turn_hint_unresolved() -> None:
    rows = [
        _row(message_id=1, message_type="user", message="First request.", turn_number=1),
        _row(message_id=2, message_type="assistant", message="First response.", turn_number=1),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "rendered_turn",
                "turn_number": 99,
                "speaker": "assistant",
                "anchor_text": "not authoritative",
                "reason": "Classifier emitted a stale turn number.",
                "confidence": 0.73,
            }
        ]
    )

    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference=reference,
        chat_messages=rows,
    )

    assert result["status"] == "unresolved"
    assert result["materialized_turns"] == []
    assert result["unresolved_hints"][0]["status"] == "unresolved"
    assert result["unresolved_hints"][0]["turn_number"] == 99


def test_materializer_returns_empty_context_when_reference_not_required() -> None:
    result = PriorTurnReferenceMaterializer().materialize(
        prior_turn_reference={
            "required": False,
            "operation": "none",
            "status": "none",
            "confidence": None,
            "hints": [],
        },
        chat_messages=[
            _row(message_id=1, message_type="assistant", message="Prior text.", turn_number=1)
        ],
    )

    assert result == empty_prior_turn_references_context()


class _StubContextBuilder:
    def __init__(self, runtime_config: LangGraphRuntimeConfig) -> None:
        self.runtime_config = runtime_config

    def build_runtime_config(self, *, chat_inputs: ChatInputs, metadata: Dict[str, Any] | None = None) -> LangGraphRuntimeConfig:
        return self.runtime_config


class _StubIntentClassifier:
    def __init__(self, reference: Dict[str, Any]) -> None:
        self.reference = reference

    async def enrich_runtime_config(self, runtime_config: LangGraphRuntimeConfig) -> None:
        runtime_config.metadata["intent_prior_turn_reference"] = self.reference
        runtime_config.metadata["intent_classifier_label"] = "simple_chat"


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeConversationHistoryReader:
    def __init__(self, rows: List[SimpleNamespace]) -> None:
        self.rows = rows
        self.calls: List[Dict[str, Any]] = []

    def get_conversation_history(self, *, task_id: int, conversation_id: str) -> List[SimpleNamespace]:
        self.calls.append({"task_id": task_id, "conversation_id": conversation_id})
        return list(self.rows)


@pytest.mark.asyncio
async def test_facade_materializes_after_classifier_before_handler_dispatch() -> None:
    rows = [
        _row(message_id=1, message_type="user", message="What next?", turn_number=4),
        _row(message_id=2, message_type="assistant", message="Capture packets.", turn_number=4),
        _row(
            message_id=3,
            message_type="user",
            message="What did you mean by capture packets?",
            turn_number=5,
        ),
    ]
    reference = _reference(
        [
            {
                "reference_kind": "relative_turn",
                "turn_number": None,
                "speaker": "user",
                "anchor_text": None,
                "reason": "The current turn asks about the previous user turn.",
                "confidence": 0.88,
            }
        ]
    )
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=7,
        message="What did you mean by capture packets?",
        conversation_id="conv-42",
        history=[{"role": "user", "content": "What next?"}],
        api_key="test-key",
    )
    runtime_config = LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata={
            "turn_number": 5,
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-42",
                turn_id="turn-5",
                turn_sequence=5,
                messages=chat_inputs.history,
                current_message=chat_inputs.message,
            ),
        },
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )
    session = _FakeSession()
    history_reader = _FakeConversationHistoryReader(rows)
    captured: Dict[str, Any] = {}

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        captured["metadata"] = dict(config.metadata)
        return LangGraphChatResult(final_text="ok", conversation_id=config.chat_inputs.conversation_id)

    facade = LangGraphChatFacade(
        context_builder=_StubContextBuilder(runtime_config),
        intent_classifier=_StubIntentClassifier(reference),  # type: ignore[arg-type]
        session_factory=lambda: session,
        conversation_history_reader_factory=lambda _db: history_reader,  # type: ignore[arg-type]
    )
    facade._handlers = {ChatBranch.NORMAL_CHAT: SimpleNamespace(handle=_handle)}

    result = await facade.handle_turn(chat_inputs)

    assert result.final_text == "ok"
    assert session.closed is True
    assert history_reader.calls == [{"task_id": 42, "conversation_id": "conv-42"}]
    materialized = captured["metadata"][METADATA_KEY_PRIOR_TURN_REFERENCES]
    assert materialized["status"] == "ok"
    assert [item["message_id"] for item in materialized["materialized_turns"]] == [1, 2]
    assert materialized["materialized_turns"][0]["text"] == "What next?"
    assert materialized["materialized_turns"][1]["text"] == "Capture packets."
    bundle_references = captured["metadata"][METADATA_CONTEXT_BUNDLE_KEY]["prior_turn_references"]
    assert bundle_references == materialized
