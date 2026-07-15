"""Phase 0 / Task 0.3 — pin the retry stream ordering invariant.

These tests describe the persistence and ordering contract that retry
lifecycle events must obey before the rest of the implementation lands:

  * retry lifecycle events flow through ``InMemoryStreamHub.publish`` exactly
    like every other ``status`` packet — no out-of-band write path that
    bypasses the hub or the per-task sequence counter,
  * ``stream_events.sequence`` stays append-only and strictly monotonic
    across the FAILED → RETRYING → terminal transitions,
  * existing rows persisted by the failing attempt are not deleted,
    rewritten, or renumbered — they remain historical facts and the retry
    rows simply append after them in ascending order.

Today there is no shared retry-state emitter at all (Phase 3 introduces
``emit_retry_state_event``), so importing it raises ``ImportError`` and
these assertions fail for the right reason: the contract is not yet
implemented end-to-end.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from backend.services.streaming.in_memory_hub import InMemoryStreamHub


@pytest.fixture
def stream_hub(monkeypatch: pytest.MonkeyPatch) -> InMemoryStreamHub:
    """Return an isolated hub whose persistence is captured in-memory.

    Mirrors the pattern in ``test_in_memory_stream.py``: stub the
    sequence-seeder and persistence sink so the test can assert on the
    full ordered set of packets that *would* be appended to
    ``stream_events`` without touching a real database.
    """
    hub = InMemoryStreamHub()

    async def _noop_seed(_task_id: int) -> None:
        return None

    monkeypatch.setattr(hub, "_ensure_sequence_seeded", _noop_seed)
    return hub


@pytest.mark.asyncio
async def test_retry_lifecycle_events_appended_through_hub_with_monotonic_sequence(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry lifecycle events must publish through the hub and keep sequence monotonic.

    The Phase 3 contract (Retry Stream Contract section of the guide)
    requires retry lifecycle events to be ``type=status``,
    ``content=retry_state`` packets emitted via the same
    ``InMemoryStreamHub.publish`` path as every other status packet, so
    the hub assigns the next ``sequence`` and ``StreamEventStore`` appends
    a new row.

    This test fails today because ``emit_retry_state_event`` does not yet
    exist in ``stream_status_events``; the import error captures the gap.
    """
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )

    # Force the helper to publish into the test-isolated hub instead of
    # the process-wide singleton. The retry emitter (Phase 3) must route
    # through ``get_in_memory_stream_hub``, so swapping that getter is
    # sufficient to redirect publication for this test.
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    task_id = 1701
    turn_id = "task-1701-turn-2"
    workflow_id = 14
    checkpoint_id = "ckpt-stable-abc123"

    # Simulate the historical (failing-attempt) rows the previous
    # workflow attempt persisted before the retry started. These must
    # remain untouched after the retry lifecycle events are published.
    await stream_hub.publish(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": {
                "task_id": task_id,
                "turn_id": turn_id,
                "state": "FAILED",
            },
        },
    )
    await stream_hub.publish(
        task_id,
        {
            "type": "assistant_final",
            "content": "tool argument invalid",
            "metadata": {
                "task_id": task_id,
                "turn_id": turn_id,
                "status": "error",
                "retryable": True,
            },
        },
    )

    pre_retry_snapshot = [dict(p) for p in persisted]
    pre_retry_max_sequence = persisted[-1]["sequence"]
    assert pre_retry_max_sequence == 2

    # Phase 3's retry emitter contract — importing it today is exactly
    # the failure mode this test is supposed to capture.
    from backend.services.langgraph_chat.streaming.status_events import (
        emit_retry_state_event,
    )

    # Emit the canonical retry lifecycle sequence: accepted → started →
    # completed. Each call must publish through the hub and result in a
    # newly appended persisted row whose sequence is greater than the
    # previous max.
    emit_retry_state_event(
        task_id=task_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        state="accepted",
    )
    emit_retry_state_event(
        task_id=task_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        state="started",
    )
    emit_retry_state_event(
        task_id=task_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        state="completed",
    )

    # The emitter is fire-and-forget via ``loop.create_task``; let the
    # scheduled hub.publish coroutines drain before we inspect ordering.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Existing rows must remain historical facts — same payload, same
    # sequence, same position. The retry path is append-only.
    assert persisted[: len(pre_retry_snapshot)] == pre_retry_snapshot, (
        "retry lifecycle publication must not rewrite or renumber historical rows; "
        "stream_events is append-only across FAILED → RETRYING → terminal."
    )

    # Three retry lifecycle rows must have been appended after the
    # historical rows in publication order.
    retry_rows = persisted[len(pre_retry_snapshot) :]
    assert len(retry_rows) == 3, (
        "expected exactly three appended retry lifecycle rows "
        f"(accepted, started, completed); got {len(retry_rows)}."
    )

    # Every retry row must carry the canonical status/retry_state shape.
    # ``InMemoryStreamHub.publish`` wraps legacy ``{type, content, metadata}``
    # events into the packet envelope ``{obj: {...}, placement, sequence}``
    # via ``normalize_stream_packet``; the legacy fields therefore live
    # under ``obj`` on each persisted packet.
    for row in retry_rows:
        obj = row.get("obj") or {}
        assert obj.get("type") == "status"
        assert obj.get("content") == "retry_state"
        meta = obj.get("metadata") or {}
        assert meta.get("task_id") == task_id
        assert meta.get("turn_id") == turn_id
        assert meta.get("workflow_id") == workflow_id
        assert meta.get("checkpoint_id") == checkpoint_id
        assert meta.get("retry_mode") == "checkpoint"
        assert meta.get("retry_attempt") == 1
        assert meta.get("retry_max_attempts") == 2

    states_in_order = [
        (row.get("obj") or {}).get("metadata", {}).get("state") for row in retry_rows
    ]
    assert states_in_order == ["accepted", "started", "completed"], (
        "retry lifecycle row order must match publication order; the hub "
        "assigns sequence in the order publish() is called."
    )

    # Sequence is monotonically increasing and strictly greater than the
    # last failing-attempt row.
    sequences = [row["sequence"] for row in persisted]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences), (
        "stream_events.sequence must be strictly monotonic across a retry; "
        f"got {sequences}."
    )
    assert retry_rows[0]["sequence"] > pre_retry_max_sequence, (
        "first retry lifecycle row's sequence must be greater than the prior "
        f"task stream sequence; got {retry_rows[0]['sequence']} <= {pre_retry_max_sequence}."
    )


@pytest.mark.asyncio
async def test_reconnect_replay_after_retry_returns_rows_in_ascending_sequence(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay-after-cursor must return rows in ascending sequence order.

    The append-only invariant is what makes the reconnect/list-after
    cursor semantic correct. Concretely: a subscriber that cursored at
    sequence ``N`` (the last failing-attempt row) must, on reconnect,
    receive every retry lifecycle row whose sequence is greater than
    ``N`` in ascending sequence order — and never receive a renumbered
    historical row.

    Today's gap is the same as the test above: ``emit_retry_state_event``
    is not implemented, so this test fails on import.
    """
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    task_id = 1702
    turn_id = "task-1702-turn-9"
    workflow_id = 21
    checkpoint_id = "ckpt-stable-zzz999"

    # Prior failing-attempt rows.
    await stream_hub.publish(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": {"task_id": task_id, "turn_id": turn_id, "state": "RUNNING"},
        },
    )
    await stream_hub.publish(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": {"task_id": task_id, "turn_id": turn_id, "state": "FAILED"},
        },
    )
    cursor = persisted[-1]["sequence"]

    from backend.services.langgraph_chat.streaming.status_events import (
        emit_retry_state_event,
    )

    emit_retry_state_event(
        task_id=task_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        state="started",
    )
    emit_retry_state_event(
        task_id=task_id,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        state="failed",
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # A reconnect at ``cursor`` must yield rows strictly ordered by
    # ascending sequence.
    replay = [row for row in persisted if row["sequence"] > cursor]
    replay_sequences = [row["sequence"] for row in replay]
    assert replay_sequences == sorted(replay_sequences), (
        "replay after cursor must return rows in ascending sequence order; "
        f"got {replay_sequences}."
    )
    # And the replay must include every appended retry lifecycle row.
    # Persisted packets wrap the legacy ``{type, content, metadata}``
    # event under ``obj`` (see ``normalize_stream_packet``).
    assert [
        (row.get("obj") or {}).get("metadata", {}).get("state") for row in replay
    ] == ["started", "failed"], (
        "reconnect replay must include every appended retry lifecycle row "
        "in publication order without reaching back into historical rows."
    )


@pytest.mark.asyncio
async def test_emit_retry_state_event_projects_canonical_identity_through_whitelist(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3.1 helper must whitelist the canonical retry identity keys.

    ``emit_retry_state_event`` accepts the identity mapping produced by
    ``build_checkpoint_retry_identity`` and projects it through the
    canonical retry-identity whitelist before publishing. Anything outside
    that whitelist (for example, a stray ``previous_failure`` blob smuggled
    into the identity carrier by a careless caller) must never appear in
    the streamed metadata.
    """
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    from backend.services.langgraph_chat.streaming.status_events import (
        emit_retry_state_event,
    )

    task_id = 1801
    identity = {
        "task_id": task_id,
        "turn_id": "turn-x",
        "workflow_id": 99,
        "graph_name": "simple_tool",
        "checkpoint_id": "ckpt-x",
        "retry_mode": "checkpoint",
        "retry_attempt": 1,
        "retry_max_attempts": 2,
        "state": "failed",  # the prior workflow state — emitter overrides with lifecycle ``state``
        "already_in_flight": False,
        # Non-whitelisted keys that must be dropped:
        "previous_failure": {"raw_provider_payload": "secret"},
        "api_key": "should-never-stream",
    }

    emit_retry_state_event(
        task_id=task_id,
        retry_identity=identity,
        state="started",
        transcript_resync_required=True,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(persisted) == 1
    obj = persisted[0]["obj"]
    assert obj["type"] == "status"
    assert obj["content"] == "retry_state"
    meta = obj["metadata"]
    assert meta["state"] == "started", (
        "lifecycle ``state`` kwarg must override the workflow ``state`` "
        "value forwarded via the identity mapping."
    )
    assert meta["task_id"] == task_id
    assert meta["turn_id"] == "turn-x"
    assert meta["workflow_id"] == 99
    assert meta["graph_name"] == "simple_tool"
    assert meta["checkpoint_id"] == "ckpt-x"
    assert meta["retry_mode"] == "checkpoint"
    assert meta["retry_attempt"] == 1
    assert meta["retry_max_attempts"] == 2
    assert meta["already_in_flight"] is False
    assert meta["transcript_resync_required"] is True
    assert "previous_failure" not in meta, (
        "non-whitelisted identity keys (e.g. previous_failure) must never "
        "leak into stream metadata."
    )
    assert "api_key" not in meta, (
        "non-whitelisted identity keys (e.g. api_key) must never leak into "
        "stream metadata."
    )


@pytest.mark.asyncio
async def test_emit_retry_state_event_drops_unknown_lifecycle_states(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown lifecycle states must be dropped at the emitter."""
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    from backend.services.langgraph_chat.streaming.status_events import (
        emit_retry_state_event,
    )

    emit_retry_state_event(
        task_id=1802,
        state="bogus_state",  # not in the canonical lifecycle set
        retry_attempt=0,
        retry_max_attempts=2,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert persisted == [], (
        "unknown lifecycle states must be dropped at the emitter and never "
        "reach the persistence sink."
    )


@pytest.mark.asyncio
async def test_emit_retry_state_event_appends_failure_diagnostics(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure annotations (failure_stage, error_code) must surface on the metadata."""
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    from backend.services.langgraph_chat.streaming.status_events import (
        emit_retry_state_event,
    )

    emit_retry_state_event(
        task_id=1803,
        state="failed",
        turn_id="turn-y",
        workflow_id=42,
        graph_name="simple_tool",
        checkpoint_id="ckpt-y",
        retry_mode="checkpoint",
        retry_attempt=1,
        retry_max_attempts=2,
        failure_stage="tool",
        error_code="checkpoint_retry_failed",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(persisted) == 1
    meta = persisted[0]["obj"]["metadata"]
    assert meta["state"] == "failed"
    assert meta["failure_stage"] == "tool"
    assert meta["error_code"] == "checkpoint_retry_failed"


@pytest.mark.asyncio
async def test_emit_checkpoint_rewind_state_event_publishes_generic_rewind_contract(
    stream_hub: InMemoryStreamHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic checkpoint rewind events must carry operation-neutral identity."""
    persisted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        stream_hub,
        "_persist_stream_packet",
        lambda _task_id, packet: persisted.append(dict(packet)),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: stream_hub,
    )

    from backend.services.langgraph_chat.streaming.status_events import (
        emit_checkpoint_rewind_state_event,
    )

    emit_checkpoint_rewind_state_event(
        task_id=1804,
        operation_kind="retry",
        state="started",
        turn_id="turn-rewind",
        workflow_id=77,
        graph_name="simple_tool",
        checkpoint_id="ckpt-rewind",
        retry_attempt=1,
        retry_max_attempts=2,
        transcript_resync_required=True,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(persisted) == 1
    obj = persisted[0]["obj"]
    assert obj["type"] == "status"
    assert obj["content"] == "checkpoint_rewind_state"
    meta = obj["metadata"]
    assert meta["task_id"] == 1804
    assert meta["operation_kind"] == "retry"
    assert meta["state"] == "started"
    assert meta["turn_id"] == "turn-rewind"
    assert meta["workflow_id"] == 77
    assert meta["graph_name"] == "simple_tool"
    assert meta["checkpoint_id"] == "ckpt-rewind"
    assert meta["retry_attempt"] == 1
    assert meta["retry_max_attempts"] == 2
    assert meta["transcript_resync_required"] is True
    assert isinstance(meta["timestamp"], str) and meta["timestamp"]
