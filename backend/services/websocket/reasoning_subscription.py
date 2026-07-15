"""WebSocket reasoning subscription manager backed by stream packets.

This manager reuses the authoritative task stream sources:
- replay from StreamEventStore (sequence-based)
- live fanout from InMemoryStreamHub
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Literal, Set, TypedDict

from fastapi import WebSocket

from backend.database import SessionLocal
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
from backend.services.metrics import metrics
from backend.services.streaming.event_store import StreamEventStore

logger = logging.getLogger("backend.services.ws_reasoning_subscription")


class AgentReasoningEnvelope(TypedDict):
    """Outgoing multiplex packet envelope for task-bound stream events."""

    type: Literal["agent_reasoning"]
    taskId: int
    sequence: int
    packet: dict


@dataclass
class _Subscription:
    """Track a live hub-forwarding subscription."""

    subscription_id: str
    websocket: WebSocket
    task_id: int
    worker: asyncio.Task


class WebSocketReasoningSubscriptionManager:
    """Manage per-websocket task subscriptions for reasoning stream packets."""

    def __init__(self) -> None:
        self._ws_to_subscriptions: Dict[int, Set[str]] = {}
        self._subscriptions: Dict[str, _Subscription] = {}
        self._ws_task_last_seq: Dict[int, Dict[int, int]] = {}
        self._lock = asyncio.Lock()

    def _subscription_ids_for_ws_locked(self, ws_key: int) -> list[str]:
        """Return active subscription ids for a websocket key."""
        return list(self._ws_to_subscriptions.get(ws_key, set()))

    def _subscription_ids_for_ws_task_locked(self, ws_key: int, task_id: int) -> list[str]:
        """Return active subscription ids for a websocket/task pair."""
        target_ids: list[str] = []
        for sub_id in self._ws_to_subscriptions.get(ws_key, set()):
            subscription = self._subscriptions.get(sub_id)
            if subscription is not None and subscription.task_id == task_id:
                target_ids.append(sub_id)
        return target_ids

    def _cleanup_task_cursor_locked(self, ws_key: int, task_id: int) -> None:
        """Remove websocket task cursor if no active subscription remains for it."""
        task_cursors = self._ws_task_last_seq.get(ws_key)
        if task_cursors is None:
            return
        if self._subscription_ids_for_ws_task_locked(ws_key, task_id):
            return
        task_cursors.pop(task_id, None)
        if not task_cursors:
            self._ws_task_last_seq.pop(ws_key, None)

    def _cleanup_ws_cursor_locked(self, ws_key: int) -> None:
        """Remove websocket cursor map once websocket has no active subscriptions."""
        if self._ws_to_subscriptions.get(ws_key):
            return
        self._ws_task_last_seq.pop(ws_key, None)

    async def subscribe(self, websocket: WebSocket, task_id: int, last_sequence: int = 0) -> str:
        """Replay then subscribe websocket to hub events for a task."""
        ws_key = id(websocket)
        last_sent = max(0, int(last_sequence or 0))
        last_sent, replayed_count = await self._replay_events(websocket, task_id, last_sent)

        subscription_id = str(uuid.uuid4())
        worker = asyncio.create_task(
            self._forward_from_hub(
                subscription_id=subscription_id,
                websocket=websocket,
                task_id=task_id,
                start_after=last_sent,
            )
        )
        subscription = _Subscription(
            subscription_id=subscription_id,
            websocket=websocket,
            task_id=task_id,
            worker=worker,
        )

        async with self._lock:
            self._subscriptions[subscription_id] = subscription
            self._ws_to_subscriptions.setdefault(ws_key, set()).add(subscription_id)
            self._ws_task_last_seq.setdefault(ws_key, {})[task_id] = last_sent
            metrics.gauge("ws_active_connections", len(self._ws_to_subscriptions))
            metrics.inc("ws_subscriptions_opened")

        logger.info(
            "WS subscription opened: ws=%s task=%s sub=%s replayed=%s start_cursor=%s end_cursor=%s",
            ws_key,
            task_id,
            subscription_id,
            replayed_count,
            max(0, int(last_sequence or 0)),
            last_sent,
        )
        return subscription_id

    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe a specific subscription id."""
        ws_key: int | None = None
        task_id: int | None = None
        async with self._lock:
            subscription = self._subscriptions.pop(subscription_id, None)
            if subscription is None:
                return

            ws_key = id(subscription.websocket)
            task_id = subscription.task_id
            ws_subscriptions = self._ws_to_subscriptions.get(ws_key)
            if ws_subscriptions is not None:
                ws_subscriptions.discard(subscription_id)
                if not ws_subscriptions:
                    self._ws_to_subscriptions.pop(ws_key, None)
            metrics.gauge("ws_active_connections", len(self._ws_to_subscriptions))
            metrics.inc("ws_subscriptions_closed")

        if subscription and not subscription.worker.done():
            subscription.worker.cancel()
            try:
                await subscription.worker
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Error while awaiting websocket subscription worker cancel", exc_info=True)

        if ws_key is not None and task_id is not None:
            async with self._lock:
                self._cleanup_task_cursor_locked(ws_key, task_id)
                self._cleanup_ws_cursor_locked(ws_key)

        if subscription:
            logger.info(
                "WS subscription closed: ws=%s task=%s sub=%s",
                ws_key,
                subscription.task_id,
                subscription_id,
            )

    async def unsubscribe_task(self, websocket: WebSocket, task_id: int) -> int:
        """Unsubscribe websocket from a specific task."""
        ws_key = id(websocket)
        async with self._lock:
            target_ids = self._subscription_ids_for_ws_task_locked(ws_key, task_id)
        for sub_id in target_ids:
            await self.unsubscribe(sub_id)

        async with self._lock:
            self._cleanup_task_cursor_locked(ws_key, task_id)
            self._cleanup_ws_cursor_locked(ws_key)
        return len(target_ids)

    async def unsubscribe_all(self, websocket: WebSocket) -> None:
        """Unsubscribe websocket from all tasks and cleanup."""
        ws_key = id(websocket)
        async with self._lock:
            sub_ids = self._subscription_ids_for_ws_locked(ws_key)
        for sub_id in sub_ids:
            await self.unsubscribe(sub_id)
        async with self._lock:
            self._ws_to_subscriptions.pop(ws_key, None)
            self._ws_task_last_seq.pop(ws_key, None)
            metrics.gauge("ws_active_connections", len(self._ws_to_subscriptions))

    async def _replay_events(self, websocket: WebSocket, task_id: int, after: int) -> tuple[int, int]:
        """Replay persisted stream packets after a sequence cursor."""
        # Initial subscribe with cursor=0 should not replay historical backlog.
        if after <= 0:
            return after, 0

        cursor = after
        replayed_count = 0
        batch_limit = 500
        db = SessionLocal()
        try:
            store = StreamEventStore(db)
            while True:
                rows = store.list_after(task_id, cursor, batch_limit)
                if not rows:
                    break
                for row in rows:
                    if not isinstance(row.payload, dict):
                        continue
                    sequence = int(getattr(row, "sequence", 0) or 0)
                    if sequence <= cursor:
                        continue
                    await self._send_event(websocket, task_id, row.payload)
                    cursor = max(cursor, sequence)
                    replayed_count += 1
                    metrics.inc("ws_replay_events")
                if len(rows) < batch_limit:
                    break
        except Exception:
            logger.warning("WS replay failed for task %s", task_id, exc_info=True)
        finally:
            try:
                db.close()
            except Exception:
                pass
        return cursor, replayed_count

    async def _forward_from_hub(
        self,
        *,
        subscription_id: str,
        websocket: WebSocket,
        task_id: int,
        start_after: int,
    ) -> None:
        """Forward live hub events to websocket for a task."""
        last_sent = start_after
        forwarded_count = 0
        hub = get_in_memory_stream_hub()
        subscription = hub.subscribe(task_id)
        try:
            async for event in subscription:
                sequence = int(event.get("sequence", 0) or 0)
                if sequence <= last_sent:
                    continue
                await self._send_event(websocket, task_id, event)
                last_sent = sequence
                async with self._lock:
                    if subscription_id in self._subscriptions:
                        self._ws_task_last_seq.setdefault(id(websocket), {})[task_id] = sequence
                forwarded_count += 1
                metrics.inc("ws_events_sent")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "WebSocket hub forward stopped (task=%s sub=%s)",
                task_id,
                subscription_id,
                exc_info=True,
            )
        finally:
            try:
                await subscription.aclose()
            except Exception:
                pass
            logger.info(
                "WS hot forward stopped: ws=%s task=%s sub=%s forwarded=%s last_sequence=%s",
                id(websocket),
                task_id,
                subscription_id,
                forwarded_count,
                last_sent,
            )

    async def _send_event(self, websocket: WebSocket, task_id: int, packet: dict) -> None:
        if "task_id" not in packet:
            packet = {**packet, "task_id": task_id}
        sequence = int(packet.get("sequence", 0) or 0)
        payload: AgentReasoningEnvelope = {
            "type": "agent_reasoning",
            "taskId": task_id,
            "sequence": sequence,
            "packet": packet,
        }
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            # Caller handles socket disconnect cleanup.
            pass

    def get_subscription_count(self, websocket: WebSocket) -> int:
        return len(self._ws_to_subscriptions.get(id(websocket), set()))

    async def get_subscription_count_async(self, websocket: WebSocket) -> int:
        """Return subscription count for websocket using the manager lock."""
        async with self._lock:
            return len(self._ws_to_subscriptions.get(id(websocket), set()))

    async def has_task_subscription(self, websocket: WebSocket, task_id: int) -> bool:
        """Return True when websocket already has a live subscription for task."""
        ws_key = id(websocket)
        async with self._lock:
            return bool(self._subscription_ids_for_ws_task_locked(ws_key, task_id))

    async def get_subscribed_tasks(self, websocket: WebSocket) -> list[int]:
        """Return sorted task ids currently subscribed by websocket."""
        ws_key = id(websocket)
        async with self._lock:
            task_ids = {
                subscription.task_id
                for sub_id in self._ws_to_subscriptions.get(ws_key, set())
                for subscription in [self._subscriptions.get(sub_id)]
                if subscription is not None
            }
        return sorted(task_ids)

    def get_last_sequence(self, websocket: WebSocket, task_id: int) -> int:
        return self._ws_task_last_seq.get(id(websocket), {}).get(task_id, 0)


ws_reasoning_manager = WebSocketReasoningSubscriptionManager()
