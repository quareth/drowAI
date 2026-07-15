"""tenant_isolation regression tests for fail-closed tenant ownership resolvers.

These tests verify shared write-path resolver boundaries raise when the
authoritative parent tenant cannot be resolved.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.tool_call_repository import ToolCallRepository
from backend.services.chat.turn_event_service import ChatTurnEventService
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import InterruptTicketService
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowService
from backend.services.streaming.event_store import StreamEventStore, StreamEventTaskMissingError
from backend.services.streaming.reasoning_store import AgentReasoningStore, AgentReasoningTaskMissingError


def _scalar_one_or_none_result(value):
    result = Mock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalar_result(value):
    result = Mock()
    result.scalar.return_value = value
    return result


def test_chat_message_resolver_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(ValueError, match="chat message write"):
        ChatMessageService(db)._resolve_task_tenant_id(999)


def test_chat_turn_event_resolver_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(ValueError, match="chat turn event write"):
        ChatTurnEventService(db)._resolve_task_tenant_id(999)


def test_tool_call_resolver_raises_when_message_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(ValueError, match="tool call write"):
        ToolCallRepository(db)._resolve_message_tenant_id(123)


def test_stream_event_resolver_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(StreamEventTaskMissingError, match="stream event write"):
        StreamEventStore(db)._resolve_task_tenant_id(999)


def test_reasoning_store_append_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.side_effect = [
        _scalar_result(None),
        _scalar_one_or_none_result(None),
    ]
    db.rollback = Mock()

    with pytest.raises(AgentReasoningTaskMissingError, match="reasoning write"):
        AgentReasoningStore(db).append_step(task_id=999, step={"type": "reasoning", "content": "x"})


def test_turn_workflow_resolver_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(ValueError, match="turn workflow write"):
        TurnWorkflowService(db)._resolve_task_tenant_id(999)


def test_interrupt_ticket_resolver_raises_when_task_tenant_missing() -> None:
    db = Mock()
    db.execute.return_value = _scalar_one_or_none_result(None)

    with pytest.raises(ValueError, match="interrupt ticket write"):
        InterruptTicketService(db)._resolve_task_tenant_id(999)
