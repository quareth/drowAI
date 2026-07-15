"""Tests for stream event persistence and in-memory hub integration."""

import asyncio
from unittest.mock import Mock, patch

import pytest

from backend.services.streaming.in_memory_hub import InMemoryStreamHub, _SUBSCRIPTION_CLOSE_SENTINEL
from backend.services.streaming.event_store import (
    StreamEventStore,
    StreamEventTaskMissingError,
)


def _stub_hub_persistence(monkeypatch: pytest.MonkeyPatch, hub: InMemoryStreamHub) -> None:
    async def _noop_seed(_task_id: int) -> None:
        return None

    monkeypatch.setattr(hub, "_ensure_sequence_seeded", _noop_seed)
    monkeypatch.setattr(hub, "_persist_stream_packet", lambda *_args, **_kwargs: None)


def test_append_packet_raises_task_missing_when_tenant_pre_resolution_misses() -> None:
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    store = StreamEventStore(db)

    with pytest.raises(StreamEventTaskMissingError):
        store.append_packet(
            task_id=341,
            packet={
                "sequence": 1,
                "obj": {"type": "status"},
                "conversation_id": "conv-1",
            },
        )
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_in_memory_stream_disables_persistence_after_missing_task() -> None:
    hub = InMemoryStreamHub()
    packet = {
        "sequence": 1,
        "obj": {"type": "status"},
        "conversation_id": "conv-1",
    }

    with patch("backend.services.streaming.in_memory_hub.SessionLocal") as mock_session_local, patch(
        "backend.services.streaming.in_memory_hub.StreamEventStore"
    ) as mock_store_cls:
        db = Mock()
        mock_session_local.return_value = db
        store = Mock()
        store.append_packet.side_effect = StreamEventTaskMissingError("missing task")
        mock_store_cls.return_value = store

        hub._persist_stream_packet(341, packet)
        hub._persist_stream_packet(341, packet)

    # First call fails and disables; second call should not attempt DB write.
    assert 341 in hub._persistence_disabled_tasks
    assert store.append_packet.call_count == 1


@pytest.mark.asyncio
async def test_tool_end_persistence_masks_compact_result_without_masking_live_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = InMemoryStreamHub()
    task_id = 348
    sentinel = "PocSecret-DurableMasking-Sentinel-stream-1"

    async def _noop_seed(_task_id: int) -> None:
        return None

    monkeypatch.setattr(hub, "_ensure_sequence_seeded", _noop_seed)
    subscriber_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    async with hub._lock:
        hub._subs[task_id] = {subscriber_queue}

    with patch("backend.services.streaming.in_memory_hub.SessionLocal") as mock_session_local, patch(
        "backend.services.streaming.in_memory_hub.StreamEventStore"
    ) as mock_store_cls:
        db = Mock()
        mock_session_local.return_value = db
        store = Mock()
        store.append_packet.return_value = object()
        mock_store_cls.return_value = store

        await hub.publish(
            task_id,
            {
                "type": "tool_end",
                "content": "Tool shell.exec completed (success)",
                "metadata": {
                    "conversation_id": "conv-1",
                    "id": "turn-1",
                    "streaming": False,
                    "summary": {
                        "summary": f"captured password={sentinel}",
                        "key_findings": [f"Authorization: Bearer {sentinel}"],
                    },
                    "compact_tool_result": {
                        "schema_version": "2.0",
                        "tool": "shell.exec",
                        "status": "success",
                        "success": True,
                        "summary": f"captured password={sentinel}",
                        "key_findings": [f"Authorization: Bearer {sentinel}"],
                    },
                    "secret_exposure": [
                        {
                            "field": "ftp.request.command_parameter",
                            "kind": "protocol_auth_argument",
                            "proof_mode": "proof_excerpt",
                            "proof_excerpt": sentinel,
                        }
                    ],
                },
            },
        )

    live_packet = subscriber_queue.get_nowait()
    persisted_packet = store.append_packet.call_args[0][1]

    assert sentinel in str(live_packet)
    assert sentinel not in str(persisted_packet)
    assert "<DURABLE_SECRET_MASK:" in str(persisted_packet)


@pytest.mark.asyncio
async def test_non_tool_end_persistence_masks_content_and_metadata_without_masking_live_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = InMemoryStreamHub()
    task_id = 349
    sentinel = "PocSecret-DurableMasking-Sentinel-stream-2"

    async def _noop_seed(_task_id: int) -> None:
        return None

    monkeypatch.setattr(hub, "_ensure_sequence_seeded", _noop_seed)
    subscriber_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    async with hub._lock:
        hub._subs[task_id] = {subscriber_queue}

    with patch("backend.services.streaming.in_memory_hub.SessionLocal") as mock_session_local, patch(
        "backend.services.streaming.in_memory_hub.StreamEventStore"
    ) as mock_store_cls:
        db = Mock()
        mock_session_local.return_value = db
        store = Mock()
        store.append_packet.return_value = object()
        mock_store_cls.return_value = store

        await hub.publish(
            task_id,
            {
                "type": "assistant_delta",
                "content": f"Captured credential password={sentinel}",
                "metadata": {
                    "conversation_id": "conv-1",
                    "id": "turn-1",
                    "streaming": True,
                    "observation": f"Authorization: Bearer {sentinel}",
                },
            },
        )

    live_packet = subscriber_queue.get_nowait()
    persisted_packet = store.append_packet.call_args[0][1]

    assert sentinel in str(live_packet)
    assert sentinel not in str(persisted_packet)
    assert "<DURABLE_SECRET_MASK:" in str(persisted_packet)


@pytest.mark.asyncio
async def test_remove_task_cleans_hub_state_and_closes_subscription() -> None:
    hub = InMemoryStreamHub()
    task_id = 777

    subscription = hub.subscribe(task_id)
    first_next = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    assert task_id in hub._subs
    await hub.remove_task(task_id)

    with pytest.raises(StopAsyncIteration):
        await first_next

    assert task_id not in hub._subs
    assert task_id not in hub._message_queues
    assert task_id in hub._persistence_disabled_tasks


@pytest.mark.asyncio
async def test_publish_disconnects_overflowed_subscriber_and_tracks_metrics(monkeypatch) -> None:
    hub = InMemoryStreamHub()
    _stub_hub_persistence(monkeypatch, hub)
    task_id = 991

    metric_calls: list[str] = []
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.safe_inc",
        lambda name, *_args, **_kwargs: metric_calls.append(name),
    )

    full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"preloaded": True})
    healthy_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    async with hub._lock:
        hub._subs[task_id] = {full_queue, healthy_queue}

    await hub.publish(task_id, {"type": "status", "content": "ok", "metadata": {}})

    assert metric_calls.count("interactive_stream_queue_drops") == 1
    assert metric_calls.count("interactive_stream_overflow_disconnects") == 1
    assert metric_calls.count("interactive_stream_no_subscriber_events") == 0
    async with hub._lock:
        assert full_queue not in hub._subs[task_id]
        assert healthy_queue in hub._subs[task_id]
    assert healthy_queue.qsize() == 1
    assert full_queue.qsize() == 1
    assert full_queue.get_nowait() is _SUBSCRIPTION_CLOSE_SENTINEL


@pytest.mark.asyncio
async def test_overflow_disconnect_preserves_sequence_for_replay_recovery(monkeypatch) -> None:
    hub = InMemoryStreamHub()
    _stub_hub_persistence(monkeypatch, hub)
    task_id = 993

    persisted_packets: list[dict] = []
    monkeypatch.setattr(
        hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted_packets.append(dict(packet)),
    )

    overflow_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    overflow_queue.put_nowait({"preloaded": True})
    async with hub._lock:
        hub._subs[task_id] = {overflow_queue}

    await hub.publish(task_id, {"type": "status", "content": "first", "metadata": {}})
    await hub.publish(task_id, {"type": "status", "content": "second", "metadata": {}})

    assert [packet["sequence"] for packet in persisted_packets] == [1, 2]
    async with hub._lock:
        assert task_id not in hub._subs

    recovered_stream = hub.subscribe(task_id)
    next_packet_task = asyncio.create_task(recovered_stream.__anext__())
    await asyncio.sleep(0)
    await hub.publish(task_id, {"type": "status", "content": "third", "metadata": {}})
    recovered_packet = await next_packet_task
    assert recovered_packet["sequence"] == 3
    persisted_sequences = [packet["sequence"] for packet in persisted_packets]
    assert persisted_sequences == [1, 2, 3, 4]
    await recovered_stream.aclose()


@pytest.mark.asyncio
async def test_publish_tracks_no_subscriber_events(monkeypatch) -> None:
    hub = InMemoryStreamHub()
    _stub_hub_persistence(monkeypatch, hub)

    metric_calls: list[str] = []
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.safe_inc",
        lambda name, *_args, **_kwargs: metric_calls.append(name),
    )

    await hub.publish(992, {"type": "status", "content": "ok", "metadata": {}})

    assert metric_calls.count("interactive_stream_no_subscriber_events") == 1
    assert metric_calls.count("interactive_stream_queue_drops") == 0
