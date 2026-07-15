"""Tests for the extracted reasoning SSE transport service."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.services.streaming.reasoning_sse_service import ReasoningSSEService


class _StubSubscription:
    def __init__(self, events: list[dict]):
        self._events = asyncio.Queue()
        for event in events:
            self._events.put_nowait(event)

    async def __anext__(self):
        if self._events.empty():
            raise StopAsyncIteration
        return await self._events.get()

    async def aclose(self):
        return None


class _DelayedSubscription:
    def __init__(self):
        self._events = asyncio.Queue()

    def __aiter__(self):
        return self

    async def push(self, event: dict | None) -> None:
        await self._events.put(event)

    async def __anext__(self):
        item = await self._events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def aclose(self):
        await self._events.put(None)
        return None


class _StubHub:
    def __init__(self, events: list[dict], *, latest_sequence: int = 0):
        self._events = events
        self._latest_sequence = latest_sequence

    def subscribe(self, task_id: int):  # noqa: ARG002
        return _StubSubscription(list(self._events))

    def get_latest_sequence(self, task_id: int) -> int:  # noqa: ARG002
        return self._latest_sequence


class _FileTailWatcher:
    def __init__(self) -> None:
        self._tail = _DelayedSubscription()

    async def stream_lines(self, task_id: int):  # noqa: ARG002
        async for line in self._tail:
            yield line


def _extract_data_payloads(chunks: list[str]) -> list[dict]:
    payloads: list[dict] = []
    for line in chunks:
        if line.startswith("data: "):
            payloads.append(json.loads(line.split("data: ", 1)[1]))
    return payloads


def _extract_stream_objects(chunks: list[str]) -> list[dict]:
    objects: list[dict] = []
    for payload in _extract_data_payloads(chunks):
        obj = payload.get("obj")
        if isinstance(obj, dict):
            objects.append(obj)
        else:
            objects.append(payload)
    return objects


@pytest.mark.asyncio
async def test_generate_disables_live_idle_timeout_but_keeps_file_fallback_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ReasoningSSEService()
    captured: dict[str, object] = {}

    async def _raise_hot(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["live_idle_timeout"] = kwargs["idle_timeout"]
        raise RuntimeError("force file fallback")
        if False:
            yield ""

    async def _fake_file(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["file_idle_timeout"] = kwargs["idle_timeout"]
        if False:
            yield ""
        return

    monkeypatch.setattr(
        "backend.services.streaming.reasoning_sse_service.REASONING_SSE_IDLE_TIMEOUT_SEC",
        5,
    )
    monkeypatch.setattr(service, "stream_interactive_events_direct", _raise_hot)
    monkeypatch.setattr(service, "generate_file_based_events", _fake_file)

    chunks: list[str] = []
    async for chunk in service.generate(task_id=1, after=0):
        chunks.append(chunk)

    assert captured["live_idle_timeout"] is None
    assert captured["file_idle_timeout"] == 5.0
    assert any(chunk.startswith("retry: ") for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_interactive_replays_persisted_packets_before_live_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReasoningSSEService()
    persisted_rows = [
        SimpleNamespace(
            sequence=1,
            payload={"sequence": 1, "task_id": 7, "type": "reasoning_delta", "content": "persisted-1", "metadata": {}},
        ),
        SimpleNamespace(
            sequence=2,
            payload={"sequence": 2, "task_id": 7, "type": "reasoning_delta", "content": "persisted-2", "metadata": {}},
        ),
    ]
    hub = _StubHub(
        [
            {
                "sequence": 3,
                "type": "reasoning_delta",
                "content": "live-3",
                "metadata": {},
            }
        ],
        latest_sequence=2,
    )

    monkeypatch.setattr("backend.services.streaming.reasoning_sse_service.get_in_memory_stream_hub", lambda: hub)

    chunks: list[str] = []
    async for chunk in service.stream_interactive_events_direct(
        task_id=7,
        after=0,
        heartbeat_interval=0.1,
        idle_timeout=None,
        build_ping=lambda label: ": ping\n\n",
        on_data_event=lambda: None,
        build_idle_comment=lambda label: ": idle\n\n",
        mark_activity=lambda: None,
        latest_sequence=2,
        persisted_list_after=lambda task_id, after, limit: [row for row in persisted_rows if row.sequence > after][:limit],
        metrics_obj=None,
    ):
        chunks.append(chunk)

    payloads = _extract_data_payloads(chunks)
    objects = _extract_stream_objects(chunks)
    assert [payload["sequence"] for payload in payloads] == [1, 2, 3]
    assert [obj["content"] for obj in objects] == ["persisted-1", "persisted-2", "live-3"]


@pytest.mark.asyncio
async def test_stream_interactive_gap_recovery_emits_resync_when_replay_is_not_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReasoningSSEService()
    hub = _StubHub(
        [
            {
                "sequence": 3,
                "type": "reasoning_delta",
                "content": "live-3",
                "metadata": {},
            }
        ]
    )

    monkeypatch.setattr("backend.services.streaming.reasoning_sse_service.get_in_memory_stream_hub", lambda: hub)

    chunks: list[str] = []
    async for chunk in service.stream_interactive_events_direct(
        task_id=9,
        after=0,
        heartbeat_interval=0.1,
        idle_timeout=None,
        build_ping=lambda label: ": ping\n\n",
        on_data_event=lambda: None,
        build_idle_comment=lambda label: ": idle\n\n",
        mark_activity=lambda: None,
        latest_sequence=0,
        persisted_list_after=lambda task_id, after, limit: [
            SimpleNamespace(
                sequence=1,
                payload={"sequence": 1, "task_id": 9, "type": "reasoning_delta", "content": "persisted-1", "metadata": {}},
            )
        ],
        metrics_obj=None,
    ):
        chunks.append(chunk)

    payloads = _extract_data_payloads(chunks)
    assert len(payloads) == 1
    event = payloads[0].get("obj", payloads[0])
    assert event["type"] == "status"
    assert event["content"] == "resync_required"
    assert event["metadata"]["stream_source"] == "interactive_gap"


@pytest.mark.asyncio
async def test_generate_file_based_events_emits_idle_comment_when_tail_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = _FileTailWatcher()
    service = ReasoningSSEService(log_watcher=watcher)
    monkeypatch.setattr(
        "backend.services.streaming.reasoning_sse_service.read_reasoning_log_entries",
        lambda task_id: [],
    )

    chunks: list[str] = []
    stream_gen = service.generate_file_based_events(
        task_id=4,
        after=0,
        heartbeat_interval=0.01,
        idle_timeout=0.02,
        build_ping=lambda label: f": ping {label}\n\n",
        on_data_event=lambda: None,
        build_idle_comment=lambda label: f": closing {label}\n\n",
        mark_activity=lambda: None,
    )
    try:
        async for chunk in stream_gen:
            chunks.append(chunk)
            if chunk.startswith(": closing "):
                break
    finally:
        await stream_gen.aclose()

    assert any(chunk.startswith(": ping file-tail") for chunk in chunks)
    assert any(chunk.startswith(": closing file-tail") for chunk in chunks)
