"""E2E continuity checks for multiplex websocket reconnect recovery.

This module validates stream continuity guarantees required by:
- reconnect during active stream replays missed packets from persisted source
- recovered sequence stream stays monotonic without duplicate terminal packets"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.services.streaming.in_memory_hub import InMemoryStreamHub
from backend.services.streaming.event_store import StreamEventStore
from backend.services.websocket.reasoning_subscription import WebSocketReasoningSubscriptionManager


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))


def _seed_task(session_factory: sessionmaker[Session]) -> int:
    db = session_factory()
    try:
        owner = User(username="stream_owner", password="x", email="stream_owner@example.com")
        db.add(owner)
        db.commit()
        db.refresh(owner)

        task = Task(user_id=owner.id, name="continuity-task")
        db.add(task)
        db.commit()
        db.refresh(task)
        return int(task.id)
    finally:
        db.close()


async def _wait_for_payload_count(websocket: _FakeWebSocket, expected: int, timeout_seconds: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while len(websocket.sent_payloads) < expected:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for websocket payloads: have={len(websocket.sent_payloads)} expected={expected}"
            )
        await asyncio.sleep(0.01)


def _packet_content(payload: dict) -> str | None:
    packet = payload.get("packet", {})
    obj = packet.get("obj", {}) if isinstance(packet, dict) else {}
    content = obj.get("content") if isinstance(obj, dict) else None
    return content if isinstance(content, str) else None


async def _wait_for_packet_content(
    websocket: _FakeWebSocket, content: str, timeout_seconds: float = 1.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        for payload in websocket.sent_payloads:
            if _packet_content(payload) == content:
                return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for packet content '{content}'. "
                f"seen={[c for c in (_packet_content(p) for p in websocket.sent_payloads) if c]}"
            )
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_reconnect_replays_missed_packets_with_sequence_continuity(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    task_id = _seed_task(session_factory)

    hub = InMemoryStreamHub()
    monkeypatch.setattr("backend.services.streaming.in_memory_hub.SessionLocal", session_factory)
    monkeypatch.setattr("backend.services.websocket.reasoning_subscription.SessionLocal", session_factory)
    monkeypatch.setattr("backend.services.websocket.reasoning_subscription.get_in_memory_stream_hub", lambda: hub)

    manager = WebSocketReasoningSubscriptionManager()
    ws_first = _FakeWebSocket()
    ws_second = _FakeWebSocket()
    ws_third = _FakeWebSocket()

    first_sub = await manager.subscribe(ws_first, task_id, last_sequence=0)
    try:
        # Allow hub-forward worker to attach before publishing the first live packet.
        await asyncio.sleep(0.05)
        await hub.publish(
            task_id,
            {
                "placement": {"turn_index": 1, "tab_index": 1},
                "obj": {
                    "type": "tool_start",
                    "content": "seq-1",
                    "metadata": {"id": "turn-1", "ind": 1, "turn_sequence": 1},
                },
            },
        )
        await _wait_for_payload_count(ws_first, 1)
        first_payload = await _wait_for_packet_content(ws_first, "seq-1")
        first_sequence = int(first_payload["sequence"])

        await manager.unsubscribe(first_sub)

        await hub.publish(
            task_id,
            {
                "placement": {"turn_index": 1, "tab_index": 1},
                "obj": {
                    "type": "tool_start",
                    "content": "seq-2",
                    "metadata": {"id": "turn-1", "ind": 2, "turn_sequence": 1},
                },
            },
        )
        await hub.publish(
            task_id,
            {
                "placement": {"turn_index": 1, "tab_index": 1},
                "obj": {
                    "type": "assistant_final",
                    "content": "seq-3-terminal",
                    "metadata": {"id": "turn-1", "ind": 3, "turn_sequence": 1},
                },
            },
        )

        second_sub = await manager.subscribe(ws_second, task_id, last_sequence=first_sequence)
        replay_seq_2 = await _wait_for_packet_content(ws_second, "seq-2")
        replay_seq_3 = await _wait_for_packet_content(ws_second, "seq-3-terminal")
        replay_payloads = [replay_seq_2, replay_seq_3]
        replay_sequences = [int(payload["sequence"]) for payload in replay_payloads]
        assert replay_sequences == sorted(replay_sequences)
        assert replay_sequences[0] > first_sequence
        assert len(replay_sequences) == len(set(replay_sequences))
        assert sum(
            1
            for payload in replay_payloads
            if _packet_content(payload) == "seq-3-terminal"
        ) == 1
        await manager.unsubscribe(second_sub)

        await hub.publish(
            task_id,
            {
                "placement": {"turn_index": 2, "tab_index": 1},
                "obj": {
                    "type": "tool_start",
                    "content": "seq-4-live",
                    "metadata": {"id": "turn-2", "ind": 4, "turn_sequence": 2},
                },
            },
        )

        third_sub = await manager.subscribe(ws_third, task_id, last_sequence=max(replay_sequences))
        try:
            continued_payload = await _wait_for_packet_content(ws_third, "seq-4-live")
            assert int(continued_payload["sequence"]) > max(replay_sequences)
        finally:
            await manager.unsubscribe(third_sub)
    finally:
        await manager.unsubscribe_all(ws_first)
        await manager.unsubscribe_all(ws_second)
        await manager.unsubscribe_all(ws_third)

    verify_db = session_factory()
    try:
        stored = StreamEventStore(verify_db).list_after(task_id, 0, limit=10)
        contents = {
            str(((row.payload or {}).get("obj") or {}).get("content"))
            for row in stored
            if isinstance(row.payload, dict)
        }
        assert {"seq-1", "seq-2", "seq-3-terminal", "seq-4-live"}.issubset(contents)
    finally:
        verify_db.close()
        engine.dispose()

