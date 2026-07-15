"""Overflow-focused unit tests for in-memory stream hub fanout reliability."""

import asyncio

import pytest

from backend.services.streaming.in_memory_hub import InMemoryStreamHub, _SUBSCRIPTION_CLOSE_SENTINEL


@pytest.fixture
def stream_hub(monkeypatch: pytest.MonkeyPatch) -> InMemoryStreamHub:
    hub = InMemoryStreamHub()

    async def _noop_seed(_task_id: int) -> None:
        return None

    monkeypatch.setattr(hub, "_ensure_sequence_seeded", _noop_seed)
    monkeypatch.setattr(hub, "_persist_stream_packet", lambda *_args, **_kwargs: None)
    return hub


@pytest.mark.asyncio
async def test_publish_queue_overflow_records_disconnect_metrics(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metric_calls: list[str] = []
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.safe_inc",
        lambda name, *_args, **_kwargs: metric_calls.append(name),
    )

    task_id = 1301
    overflow_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    overflow_queue.put_nowait({"preloaded": True})
    async with stream_hub._lock:
        stream_hub._subs[task_id] = {overflow_queue}

    await stream_hub.publish(task_id, {"type": "status", "content": "live", "metadata": {}})

    assert metric_calls.count("interactive_stream_queue_drops") == 1
    assert metric_calls.count("interactive_stream_overflow_disconnects") == 1
    assert overflow_queue.qsize() == 1
    assert overflow_queue.get_nowait() is _SUBSCRIPTION_CLOSE_SENTINEL


@pytest.mark.asyncio
async def test_publish_disconnects_only_overflowed_subscriber(
    stream_hub: InMemoryStreamHub,
) -> None:
    task_id = 1302
    overflow_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    overflow_queue.put_nowait({"preloaded": True})
    healthy_queue: asyncio.Queue = asyncio.Queue(maxsize=2)

    async with stream_hub._lock:
        stream_hub._subs[task_id] = {overflow_queue, healthy_queue}

    await stream_hub.publish(task_id, {"type": "status", "content": "live", "metadata": {}})

    async with stream_hub._lock:
        assert overflow_queue not in stream_hub._subs[task_id]
        assert healthy_queue in stream_hub._subs[task_id]

    delivered = healthy_queue.get_nowait()
    assert delivered["sequence"] == 1
    assert delivered["obj"]["content"] == "live"


@pytest.mark.asyncio
async def test_persisted_sequences_allow_replay_recovery_after_overflow(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_packets: list[dict] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted_packets.append(dict(packet)),
    )

    task_id = 1303
    overflow_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    overflow_queue.put_nowait({"preloaded": True})
    async with stream_hub._lock:
        stream_hub._subs[task_id] = {overflow_queue}

    await stream_hub.publish(task_id, {"type": "status", "content": "first", "metadata": {}})
    await stream_hub.publish(task_id, {"type": "status", "content": "second", "metadata": {}})
    assert [packet["sequence"] for packet in persisted_packets] == [1, 2]

    replay_after_zero = [packet for packet in persisted_packets if packet["sequence"] > 0]
    replay_after_one = [packet for packet in persisted_packets if packet["sequence"] > 1]
    assert [packet["sequence"] for packet in replay_after_zero] == [1, 2]
    assert [packet["sequence"] for packet in replay_after_one] == [2]

    recovered_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    async with stream_hub._lock:
        stream_hub._subs[task_id] = {recovered_queue}
    await stream_hub.publish(task_id, {"type": "status", "content": "third", "metadata": {}})

    recovered_event = recovered_queue.get_nowait()
    assert recovered_event["sequence"] == 3
    assert [packet["sequence"] for packet in persisted_packets] == [1, 2, 3]
