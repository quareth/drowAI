"""Thin router-level tests for reasoning SSE, history, and runtime input routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers import agent_reasoning
from backend.services.task.runtime_input_service import RuntimeInputResult


class _AsyncIterator:
    """Simple async iterator helper for streaming-response tests."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


@pytest.mark.asyncio
async def test_stream_agent_reasoning_sets_sse_headers_and_respects_last_event_id() -> None:
    request = MagicMock()
    request.headers = {"Last-Event-ID": "7"}

    with patch("backend.routers.agent_reasoning._prepare_reasoning_stream_preflight"), patch.object(
        agent_reasoning._reasoning_sse_service,
        "generate",
        return_value=_AsyncIterator([]),
    ) as mock_generate:
        response = await agent_reasoning.stream_agent_reasoning(task_id=123, request=request, after=2)

    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.media_type == "text/event-stream"
    mock_generate.assert_called_once_with(
        123,
        after=7,
        persisted_list_after=agent_reasoning._list_after_persisted_stream_events,
    )


@pytest.mark.asyncio
async def test_stream_agent_reasoning_unauthorized_does_not_start_stream() -> None:
    request = MagicMock()
    request.headers = {}

    with patch(
        "backend.routers.agent_reasoning._prepare_reasoning_stream_preflight",
        side_effect=HTTPException(status_code=401, detail="Authentication required"),
    ), patch.object(agent_reasoning._reasoning_sse_service, "generate") as mock_generate:
        with pytest.raises(HTTPException) as exc:
            await agent_reasoning.stream_agent_reasoning(task_id=123, request=request, after=0)

    assert exc.value.status_code == 401
    mock_generate.assert_not_called()


@pytest.mark.asyncio
async def test_stream_agent_reasoning_preflight_session_closed_before_generator_creation() -> None:
    request = MagicMock()
    request.headers = {}

    mock_db = MagicMock()
    user = SimpleNamespace(id=11, username="tester")
    tenant_context = SimpleNamespace(tenant_id=701, user_id=11, role="owner")

    def _generate_with_assertions(*args, **kwargs):  # noqa: ANN002, ANN003
        assert mock_db.close.called is True
        return _AsyncIterator([])

    with patch("backend.routers.agent_reasoning.SessionLocal", return_value=mock_db), patch(
        "backend.routers.agent_reasoning._get_user_from_request",
        return_value=(user, {"sub": "tester", "user_id": 11}),
    ), patch(
        "backend.routers.agent_reasoning._resolve_tenant_context_for_request",
        return_value=tenant_context,
    ), patch(
        "backend.routers.agent_reasoning.get_owned_task_or_404",
        return_value=object(),
    ), patch.object(
        agent_reasoning._reasoning_sse_service,
        "generate",
        side_effect=_generate_with_assertions,
    ) as mock_generate:
        await agent_reasoning.stream_agent_reasoning(task_id=321, request=request, after=0)

    assert mock_db.close.call_count == 1
    assert mock_generate.called is True


@pytest.mark.asyncio
async def test_reasoning_history_rejects_conflicting_cursors_before_service_delegation() -> None:
    with patch("backend.routers.agent_reasoning.AgentReasoningHistoryService.get_history") as mock_get_history:
        with pytest.raises(HTTPException) as exc:
            await agent_reasoning.get_reasoning_history(
                task_id=5,
                after=1,
                before=2,
                limit=50,
                order="asc",
                db=MagicMock(),
                request=MagicMock(),
            )

    assert exc.value.status_code == 400
    mock_get_history.assert_not_called()


@pytest.mark.asyncio
async def test_reasoning_history_delegates_after_auth_and_ownership_checks() -> None:
    db = MagicMock()
    request = MagicMock()
    request.headers = {}
    user = SimpleNamespace(id=7)
    tenant_context = SimpleNamespace(tenant_id=701, user_id=7, role="owner")
    expected = {"items": [], "nextAfter": 0, "hasMore": False}

    with patch(
        "backend.routers.agent_reasoning._get_user_from_request",
        return_value=(user, {"sub": "tester", "user_id": 7}),
    ), patch(
        "backend.routers.agent_reasoning._resolve_tenant_context_for_request",
        return_value=tenant_context,
    ), patch(
        "backend.routers.agent_reasoning.get_owned_task_or_404",
        return_value=object(),
    ) as mock_access, patch(
        "backend.routers.agent_reasoning.AgentReasoningHistoryService.get_history",
        return_value=expected,
    ) as mock_get_history:
        payload = await agent_reasoning.get_reasoning_history(
            task_id=44,
            after=5,
            before=None,
            limit=25,
            order="desc",
            db=db,
            request=request,
        )

    assert payload == expected
    mock_access.assert_called_once_with(db=db, task_id=44, user_id=7, tenant_id=701)
    mock_get_history.assert_called_once_with(44, after=5, before=None, limit=25, order="desc")


@pytest.mark.asyncio
async def test_reasoning_replay_delegates_after_auth_and_ownership_checks() -> None:
    db = MagicMock()
    request = MagicMock()
    request.headers = {}
    user = SimpleNamespace(id=7)
    tenant_context = SimpleNamespace(tenant_id=701, user_id=7, role="owner")
    expected = {"items": [], "nextAfter": 5, "hasMore": False}

    with patch(
        "backend.routers.agent_reasoning._get_user_from_request",
        return_value=(user, {"sub": "tester", "user_id": 7}),
    ), patch(
        "backend.routers.agent_reasoning._resolve_tenant_context_for_request",
        return_value=tenant_context,
    ), patch(
        "backend.routers.agent_reasoning.get_owned_task_or_404",
        return_value=object(),
    ) as mock_access, patch(
        "backend.routers.agent_reasoning.AgentReasoningHistoryService.get_replay_history",
        return_value=expected,
    ) as mock_get_replay_history:
        payload = await agent_reasoning.get_reasoning_replay(
            task_id=44,
            after=5,
            limit=25,
            db=db,
            request=request,
        )

    assert payload == expected
    mock_access.assert_called_once_with(db=db, task_id=44, user_id=7, tenant_id=701)
    mock_get_replay_history.assert_called_once_with(44, after=5, limit=25)


@pytest.mark.asyncio
async def test_send_user_message_keeps_success_on_non_fatal_signal_failure() -> None:
    db = MagicMock()
    request = MagicMock()
    user = SimpleNamespace(id=5)
    tenant_context = SimpleNamespace(tenant_id=701, user_id=5, role="owner")
    message = agent_reasoning.UserMessage(message="continue")

    mock_conversation_manager = MagicMock()
    mock_conversation_manager.ensure_default_conversation.return_value = "conv-1"
    mock_chat_turn_orchestrator = MagicMock()
    mock_chat_turn_orchestrator.reserve_user_message.return_value = (77, 1)

    with patch(
        "backend.routers.agent_reasoning._get_user_from_request",
        return_value=(user, {"sub": "tester", "user_id": 5}),
    ), patch(
        "backend.routers.agent_reasoning._resolve_tenant_context_for_request",
        return_value=tenant_context,
    ), patch(
        "backend.routers.agent_reasoning.get_owned_task_or_404",
        return_value=object(),
    ), patch(
        "agent.chat.ConversationManager",
        return_value=mock_conversation_manager,
    ), patch(
        "backend.services.chat.turn_orchestrator.ChatTurnOrchestrator",
        return_value=mock_chat_turn_orchestrator,
    ), patch.object(
        agent_reasoning._runtime_input_service,
        "append_and_signal",
        AsyncMock(
            return_value=RuntimeInputResult(
                persisted=True,
                signal_attempted=True,
                signal_sent=False,
                detail="container not running",
            )
        ),
    ) as mock_append:
        payload = await agent_reasoning.send_user_message(task_id=91, message=message, request=request, db=db)

    assert payload == {"success": True, "signal_sent": False, "detail": "container not running"}
    mock_append.assert_awaited_once_with(91, message="continue", strict_persistence=False, user_id=5)
