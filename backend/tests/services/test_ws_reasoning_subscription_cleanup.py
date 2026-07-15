"""Regression tests for websocket reasoning subscription cleanup invariants.

Responsibilities:
- keep cursor state derived only from active subscriptions
- verify unsubscribe lifecycle symmetry across unsubscribe variants
- prevent sequence-cursor map growth under subscription churn
"""

from __future__ import annotations

import pytest

from backend.services.streaming.in_memory_hub import InMemoryStreamHub
from backend.services.websocket.reasoning_subscription import WebSocketReasoningSubscriptionManager


class _FakeWebSocket:
    async def send_text(self, payload: str) -> None:  # noqa: ARG002
        return None


@pytest.fixture()
def subscription_manager(monkeypatch: pytest.MonkeyPatch) -> WebSocketReasoningSubscriptionManager:
    hub = InMemoryStreamHub()
    monkeypatch.setattr("backend.services.websocket.reasoning_subscription.get_in_memory_stream_hub", lambda: hub)
    monkeypatch.setattr("backend.services.websocket.reasoning_subscription.metrics.gauge", lambda *_a, **_k: None)
    monkeypatch.setattr("backend.services.websocket.reasoning_subscription.metrics.inc", lambda *_a, **_k: None)
    return WebSocketReasoningSubscriptionManager()


@pytest.mark.asyncio
async def test_unsubscribe_clears_task_cursor_when_last_subscription_removed(
    subscription_manager: WebSocketReasoningSubscriptionManager,
) -> None:
    ws = _FakeWebSocket()
    task_id = 3101

    sub_id = await subscription_manager.subscribe(ws, task_id, last_sequence=0)
    assert subscription_manager._ws_task_last_seq[id(ws)][task_id] == 0

    await subscription_manager.unsubscribe(sub_id)

    assert id(ws) not in subscription_manager._ws_task_last_seq
    assert await subscription_manager.get_subscription_count_async(ws) == 0


@pytest.mark.asyncio
async def test_unsubscribe_keeps_task_cursor_when_same_task_subscription_still_active(
    subscription_manager: WebSocketReasoningSubscriptionManager,
) -> None:
    ws = _FakeWebSocket()
    task_id = 3102

    first_sub = await subscription_manager.subscribe(ws, task_id, last_sequence=0)
    second_sub = await subscription_manager.subscribe(ws, task_id, last_sequence=0)

    await subscription_manager.unsubscribe(first_sub)

    assert id(ws) in subscription_manager._ws_task_last_seq
    assert task_id in subscription_manager._ws_task_last_seq[id(ws)]
    assert await subscription_manager.has_task_subscription(ws, task_id)

    await subscription_manager.unsubscribe(second_sub)
    assert id(ws) not in subscription_manager._ws_task_last_seq


@pytest.mark.asyncio
async def test_unsubscribe_all_clears_stale_cursor_without_active_subscriptions(
    subscription_manager: WebSocketReasoningSubscriptionManager,
) -> None:
    ws = _FakeWebSocket()
    task_id = 3103

    subscription_manager._ws_task_last_seq[id(ws)] = {task_id: 77}
    await subscription_manager.unsubscribe_all(ws)

    assert id(ws) not in subscription_manager._ws_task_last_seq


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_churn_does_not_grow_cursor_map(
    subscription_manager: WebSocketReasoningSubscriptionManager,
) -> None:
    ws = _FakeWebSocket()
    task_id = 3104

    for _ in range(40):
        sub_id = await subscription_manager.subscribe(ws, task_id, last_sequence=0)
        await subscription_manager.unsubscribe(sub_id)

    assert subscription_manager._ws_task_last_seq == {}
    assert await subscription_manager.get_subscription_count_async(ws) == 0
