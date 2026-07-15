"""Compatibility SSE transport for reasoning stream replay and fallback.

This service owns the reasoning SSE generator stack, including persisted
replay bootstrap, gap recovery, heartbeat handling, and file-backed fallback
without coupling HTTP request/auth concerns into the transport layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from backend.config import (
    DB_STREAM_HEARTBEAT_INTERVAL_SEC,
    REASONING_DB_PERSIST,
    REASONING_DB_STREAM,
    REASONING_SSE_IDLE_TIMEOUT_SEC,
)
from backend.core.time_utils import format_iso, utc_now
from .log_watcher import AgentLogWatcher, read_reasoning_log_entries
from .in_memory_hub import get_in_memory_stream_hub
from .stream_event_schema import normalize_stream_packet

logger = logging.getLogger(__name__)

REPLAY_BATCH_SIZE = 200
_STREAM_NEXT_TIMEOUT = object()
_STREAM_NEXT_ENDED = object()
PersistedListAfter = Callable[[int, int, int], List[Any]]


async def _next_stream_item_with_timeout(
    async_iter: Any,
    timeout_seconds: float,
    pending_next_task: Optional[asyncio.Task] = None,
) -> tuple[Any, Optional[asyncio.Task]]:
    """Await the next item without cancelling the live iterator on heartbeat timeout."""
    next_task = pending_next_task or asyncio.create_task(async_iter.__anext__())
    try:
        item = await asyncio.wait_for(asyncio.shield(next_task), timeout=timeout_seconds)
        return item, None
    except asyncio.TimeoutError:
        return _STREAM_NEXT_TIMEOUT, next_task
    except asyncio.CancelledError:
        next_task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await next_task
        raise
    except StopAsyncIteration:
        return _STREAM_NEXT_ENDED, None


class ReasoningSSEService:
    """Generate compatibility SSE output for reasoning streams."""

    def __init__(self, *, log_watcher: AgentLogWatcher | None = None) -> None:
        self._log_watcher = log_watcher or AgentLogWatcher()

    async def generate(
        self,
        task_id: int,
        *,
        after: int = 0,
        persisted_list_after: PersistedListAfter | None = None,
    ) -> AsyncGenerator[str, None]:
        """Generate SSE output with persisted replay, live hub delivery, and file fallback."""
        logger.info(
            "[SSE] start stream task_id=%s db_persist=%s db_stream=%s",
            task_id,
            REASONING_DB_PERSIST,
            REASONING_DB_STREAM,
        )

        metrics_obj = None
        try:
            from backend.services.metrics import metrics as metrics_obj

            metrics_obj.inc("sse_starts")
        except Exception:
            metrics_obj = None

        heartbeat_interval = float(DB_STREAM_HEARTBEAT_INTERVAL_SEC or 0)
        if heartbeat_interval <= 0:
            heartbeat_interval = 15.0

        idle_timeout = float(REASONING_SSE_IDLE_TIMEOUT_SEC or 0)
        if idle_timeout <= 0:
            idle_timeout = None

        live_idle_timeout = None
        file_idle_timeout = idle_timeout

        retry_delay_ms = 5000
        last_data_ts = time.monotonic()
        ping_count = 0
        count = 0

        def mark_activity() -> None:
            nonlocal last_data_ts
            last_data_ts = time.monotonic()

        def on_data_event() -> None:
            nonlocal count
            count += 1
            mark_activity()

        def build_ping(comment: str = "heartbeat") -> str:
            nonlocal ping_count
            ping_count += 1
            if metrics_obj:
                try:
                    metrics_obj.inc("sse_pings")
                except Exception:
                    pass
            timestamp = int(time.time())
            return f": ping {comment} {timestamp}\n\n"

        def build_idle_comment(source: str) -> str:
            if metrics_obj:
                try:
                    metrics_obj.inc("sse_idle_disconnects")
                except Exception:
                    pass
            logger.info("[SSE] Idle timeout reached for task %s (source=%s)", task_id, source)
            timestamp = int(time.time())
            return f": closing idle-timeout source={source} ts={timestamp}\n\n"

        yield f"retry: {retry_delay_ms}\n\n"
        yield f": stream-start task={task_id}\n\n"

        latest_sequence = self._resolve_latest_sequence(task_id, after)
        try:
            async for event in self.stream_interactive_events_direct(
                task_id,
                after,
                heartbeat_interval=heartbeat_interval,
                idle_timeout=live_idle_timeout,
                build_ping=build_ping,
                on_data_event=on_data_event,
                build_idle_comment=build_idle_comment,
                mark_activity=mark_activity,
                latest_sequence=latest_sequence,
                persisted_list_after=persisted_list_after,
                metrics_obj=metrics_obj,
            ):
                yield event
            return
        except Exception as exc:
            logger.error("[SSE] Interactive hot streaming failed for task %s: %s", task_id, exc, exc_info=True)

        async for event in self.generate_file_based_events(
            task_id,
            after,
            heartbeat_interval=heartbeat_interval,
            idle_timeout=file_idle_timeout,
            build_ping=build_ping,
            on_data_event=on_data_event,
            build_idle_comment=build_idle_comment,
            mark_activity=mark_activity,
        ):
            yield event

        logger.info(
            "[SSE] end stream task_id=%s events_sent=%s pings=%s",
            task_id,
            count,
            ping_count,
        )
        if metrics_obj:
            try:
                metrics_obj.inc("sse_ends")
                metrics_obj.inc("sse_events_sent", count)
                metrics_obj.inc("sse_ping_events", ping_count)
            except Exception:
                pass

    async def stream_interactive_events_direct(
        self,
        task_id: int,
        after: int,
        *,
        heartbeat_interval: float,
        idle_timeout: Optional[float],
        build_ping: Callable[[str], str],
        on_data_event: Callable[[], None],
        build_idle_comment: Callable[[str], str],
        mark_activity: Callable[[], None],
        latest_sequence: Optional[int],
        persisted_list_after: Optional[PersistedListAfter] = None,
        replay_batch_size: int = REPLAY_BATCH_SIZE,
        metrics_obj=None,
    ) -> AsyncGenerator[str, None]:
        """Direct interactive SSE path backed by the in-memory stream hub."""
        logger.info("[SSE] Starting direct interactive streaming for task %s", task_id)

        try:
            if metrics_obj:
                metrics_obj.inc("interactive_direct_streams")
        except Exception:
            pass

        async for event in self._stream_hot_events_with_replay(
            task_id,
            after,
            source_label="interactive",
            source_prefix="interactive",
            heartbeat_interval=heartbeat_interval,
            idle_timeout=idle_timeout,
            build_ping=build_ping,
            on_data_event=on_data_event,
            build_idle_comment=build_idle_comment,
            mark_activity=mark_activity,
            latest_sequence=latest_sequence,
            persisted_list_after=persisted_list_after,
            replay_batch_size=replay_batch_size,
        ):
            yield event

    async def generate_file_based_events(
        self,
        task_id: int,
        after: int,
        *,
        heartbeat_interval: float,
        idle_timeout: Optional[float],
        build_ping: Callable[[str], str],
        on_data_event: Callable[[], None],
        build_idle_comment: Callable[[str], str],
        mark_activity: Callable[[], None],
    ) -> AsyncGenerator[str, None]:
        """Generate file-based SSE events with heartbeat support."""
        last_data_ts = time.monotonic()

        try:
            for item in read_reasoning_log_entries(task_id):
                packet = normalize_stream_packet(item, task_id=task_id) or item
                seq = int(packet.get("sequence", 0))
                if seq > after:
                    yield f"id: {seq}\n"
                    mark_activity()
                    yield f"data: {json.dumps(packet)}\n\n"
                    on_data_event()
                    last_data_ts = time.monotonic()
        except Exception as exc:
            logger.error("[SSE] replay error task_id=%s err=%s", task_id, exc)

        last_seq = 0
        existing = read_reasoning_log_entries(task_id)
        if existing:
            last_seq = int(existing[-1].get("sequence", 0))

        tail_iter = self._log_watcher.stream_lines(task_id)
        pending_next_task: Optional[asyncio.Task] = None
        while True:
            try:
                log_line, pending_next_task = await _next_stream_item_with_timeout(
                    tail_iter,
                    heartbeat_interval,
                    pending_next_task,
                )
                if log_line is _STREAM_NEXT_TIMEOUT:
                    if idle_timeout and (time.monotonic() - last_data_ts) >= idle_timeout:
                        yield build_idle_comment("file-tail")
                        return
                    yield build_ping("file-tail")
                    continue
                if log_line is _STREAM_NEXT_ENDED:
                    break
            except asyncio.TimeoutError:
                if idle_timeout and (time.monotonic() - last_data_ts) >= idle_timeout:
                    yield build_idle_comment("file-tail")
                    return
                yield build_ping("file-tail")
                continue
            except StopAsyncIteration:
                break
            except Exception as exc:
                logger.error("[SSE] tail error task_id=%s err=%s", task_id, exc)
                break

            if log_line.json_data and log_line.json_data.get("type") == "react_step":
                last_seq += 1
                payload = {**log_line.json_data, "sequence": last_seq}
                packet = normalize_stream_packet(payload, task_id=task_id) or payload
                seq_value = int(packet.get("sequence", last_seq))
                yield f"id: {seq_value}\n"
                mark_activity()
                yield f"data: {json.dumps(packet)}\n\n"
                on_data_event()
                last_data_ts = time.monotonic()

        if pending_next_task is not None:
            pending_next_task.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending_next_task

    async def _stream_hot_events_with_replay(
        self,
        task_id: int,
        after: int,
        *,
        source_label: str,
        source_prefix: str,
        heartbeat_interval: float,
        idle_timeout: Optional[float],
        build_ping: Callable[[str], str],
        on_data_event: Callable[[], None],
        build_idle_comment: Callable[[str], str],
        mark_activity: Callable[[], None],
        latest_sequence: Optional[int],
        persisted_list_after: Optional[PersistedListAfter] = None,
        replay_batch_size: int = REPLAY_BATCH_SIZE,
    ) -> AsyncGenerator[str, None]:
        """Shared hot-stream implementation with replay bootstrap and gap recovery."""
        last_data_ts = time.monotonic()

        def bump_after_data() -> None:
            nonlocal last_data_ts
            on_data_event()
            last_data_ts = time.monotonic()

        def bump_activity_only() -> None:
            nonlocal last_data_ts
            mark_activity()
            last_data_ts = time.monotonic()

        def replay_missing_tail_before_ping() -> tuple[list[str], bool]:
            """Try heartbeat-time tail recovery before ping."""
            nonlocal current_sequence
            chunks: list[str] = []
            latest_known_sequence = self._resolve_latest_sequence(task_id, current_sequence)
            if latest_known_sequence <= current_sequence:
                return chunks, False

            gap_start = current_sequence + 1
            replay_packets, replay_satisfied = self._collect_persisted_replay_packets(
                persisted_list_after=persisted_list_after,
                task_id=task_id,
                after=current_sequence,
                upto_sequence=latest_known_sequence,
                batch_size=replay_batch_size,
            )
            replay_contiguous = self._is_contiguous_replay_range(
                replay_packets,
                expected_start=gap_start,
                expected_end=latest_known_sequence,
            )
            if replay_satisfied and replay_contiguous:
                for replay_packet in replay_packets:
                    replay_seq = replay_packet.get("sequence")
                    if not isinstance(replay_seq, int):
                        continue
                    if replay_seq <= current_sequence:
                        continue
                    if not replay_packet.get("task_id"):
                        replay_packet["task_id"] = task_id
                    chunks.append(f"id: {replay_seq}\n")
                    bump_activity_only()
                    chunks.append(f"data: {json.dumps(replay_packet)}\n\n")
                    bump_after_data()
                    current_sequence = replay_seq
                return chunks, False

            resync_event = self._build_resync_status_event(task_id, latest_known_sequence, f"{source_prefix}_gap")
            chunks.append(f"id: {resync_event['sequence']}\n")
            bump_activity_only()
            chunks.append(f"data: {json.dumps(resync_event)}\n\n")
            bump_after_data()
            return chunks, True

        hub = get_in_memory_stream_hub()
        current_sequence = after
        subscription = hub.subscribe(task_id)
        pending_next_task: Optional[asyncio.Task] = asyncio.create_task(subscription.__anext__())

        try:
            if isinstance(latest_sequence, int) and latest_sequence > after:
                replay_packets, replay_satisfied = self._collect_persisted_replay_packets(
                    persisted_list_after=persisted_list_after,
                    task_id=task_id,
                    after=after,
                    upto_sequence=latest_sequence,
                    batch_size=replay_batch_size,
                )
                replay_contiguous = self._is_contiguous_replay_range(
                    replay_packets,
                    expected_start=after + 1,
                    expected_end=latest_sequence,
                )
                if replay_satisfied and replay_contiguous:
                    for replay_packet in replay_packets:
                        seq_value = replay_packet.get("sequence")
                        if not isinstance(seq_value, int):
                            continue
                        if seq_value <= current_sequence:
                            continue
                        if not replay_packet.get("task_id"):
                            replay_packet["task_id"] = task_id
                        yield f"id: {seq_value}\n"
                        bump_activity_only()
                        yield f"data: {json.dumps(replay_packet)}\n\n"
                        bump_after_data()
                        current_sequence = seq_value
                else:
                    resync_event = self._build_resync_status_event(task_id, latest_sequence, f"{source_prefix}_hub")
                    yield f"id: {resync_event['sequence']}\n"
                    bump_activity_only()
                    yield f"data: {json.dumps(resync_event)}\n\n"
                    bump_after_data()
                    return

            while True:
                try:
                    packet, pending_next_task = await _next_stream_item_with_timeout(
                        subscription,
                        heartbeat_interval,
                        pending_next_task,
                    )
                    if packet is _STREAM_NEXT_TIMEOUT:
                        if idle_timeout and (time.monotonic() - last_data_ts) >= idle_timeout:
                            yield build_idle_comment(source_label)
                            return
                        replay_chunks, terminate_stream = replay_missing_tail_before_ping()
                        for replay_chunk in replay_chunks:
                            yield replay_chunk
                        if terminate_stream:
                            return
                        if replay_chunks:
                            continue
                        yield build_ping(source_label)
                        continue
                    if packet is _STREAM_NEXT_ENDED:
                        break
                except asyncio.TimeoutError:
                    if idle_timeout and (time.monotonic() - last_data_ts) >= idle_timeout:
                        yield build_idle_comment(source_label)
                        return
                    replay_chunks, terminate_stream = replay_missing_tail_before_ping()
                    for replay_chunk in replay_chunks:
                        yield replay_chunk
                    if terminate_stream:
                        return
                    if replay_chunks:
                        continue
                    yield build_ping(source_label)
                    continue
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    logger.error(
                        "[SSE] %s hot streaming error for task %s: %s",
                        source_label,
                        task_id,
                        exc,
                        exc_info=True,
                    )
                    break

                seq_value = packet.get("sequence")
                if not isinstance(seq_value, int):
                    seq_value = current_sequence + 1
                    packet["sequence"] = seq_value

                if seq_value <= current_sequence:
                    continue
                if seq_value > current_sequence + 1:
                    gap_start = current_sequence + 1
                    gap_end = seq_value - 1
                    replay_packets, replay_satisfied = self._collect_persisted_replay_packets(
                        persisted_list_after=persisted_list_after,
                        task_id=task_id,
                        after=current_sequence,
                        upto_sequence=gap_end,
                        batch_size=replay_batch_size,
                    )
                    replay_contiguous = self._is_contiguous_replay_range(
                        replay_packets,
                        expected_start=gap_start,
                        expected_end=gap_end,
                    )
                    if replay_satisfied and replay_contiguous:
                        for replay_packet in replay_packets:
                            replay_seq = replay_packet.get("sequence")
                            if not isinstance(replay_seq, int):
                                continue
                            if replay_seq <= current_sequence:
                                continue
                            if not replay_packet.get("task_id"):
                                replay_packet["task_id"] = task_id
                            yield f"id: {replay_seq}\n"
                            bump_activity_only()
                            yield f"data: {json.dumps(replay_packet)}\n\n"
                            bump_after_data()
                            current_sequence = replay_seq
                    else:
                        latest_known_sequence = self._resolve_latest_sequence(task_id, seq_value)
                        resync_event = self._build_resync_status_event(
                            task_id,
                            latest_known_sequence,
                            f"{source_prefix}_gap",
                        )
                        yield f"id: {resync_event['sequence']}\n"
                        bump_activity_only()
                        yield f"data: {json.dumps(resync_event)}\n\n"
                        bump_after_data()
                        return

                if not packet.get("task_id"):
                    packet["task_id"] = task_id

                yield f"id: {seq_value}\n"
                bump_activity_only()
                yield f"data: {json.dumps(packet)}\n\n"
                bump_after_data()
                current_sequence = seq_value
        finally:
            if pending_next_task is not None:
                pending_next_task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending_next_task
            await subscription.aclose()

    def _build_packet_from_stream_event_row(self, row: Any, task_id: int) -> Optional[Dict[str, Any]]:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            return None
        packet = normalize_stream_packet(payload, task_id=task_id) or payload
        if not isinstance(packet, dict):
            return None
        row_sequence = getattr(row, "sequence", None)
        if not isinstance(packet.get("sequence"), int) and isinstance(row_sequence, int):
            packet["sequence"] = row_sequence
        if not packet.get("task_id"):
            packet["task_id"] = task_id
        return packet

    def _collect_persisted_replay_packets(
        self,
        *,
        persisted_list_after: Optional[PersistedListAfter],
        task_id: int,
        after: int,
        upto_sequence: Optional[int],
        batch_size: int,
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Collect persisted packets after ``after``, optionally up to a target sequence."""
        if persisted_list_after is None:
            return [], False

        cursor = after
        collected: List[Dict[str, Any]] = []
        try:
            while True:
                rows = persisted_list_after(task_id, cursor, batch_size)
                if not rows:
                    break

                advanced = False
                for row in rows:
                    row_sequence = getattr(row, "sequence", None)
                    if not isinstance(row_sequence, int):
                        continue
                    if row_sequence <= cursor:
                        continue
                    if isinstance(upto_sequence, int) and row_sequence > upto_sequence:
                        return collected, True

                    packet = self._build_packet_from_stream_event_row(row, task_id)
                    cursor = max(cursor, row_sequence)
                    advanced = True
                    if packet is None:
                        continue

                    packet_sequence = packet.get("sequence")
                    if not isinstance(packet_sequence, int):
                        packet_sequence = row_sequence
                        packet["sequence"] = packet_sequence
                    if packet_sequence <= after:
                        continue
                    collected.append(packet)
                    cursor = max(cursor, packet_sequence)
                    if isinstance(upto_sequence, int) and packet_sequence >= upto_sequence:
                        return collected, True

                if len(rows) < batch_size or not advanced:
                    break
        except Exception:
            logger.exception(
                "[SSE] Failed to replay persisted packets task_id=%s after=%s upto=%s",
                task_id,
                after,
                upto_sequence,
            )
            return [], False

        if isinstance(upto_sequence, int):
            return collected, cursor >= upto_sequence
        return collected, True

    def _is_contiguous_replay_range(
        self,
        packets: List[Dict[str, Any]],
        *,
        expected_start: int,
        expected_end: int,
    ) -> bool:
        """Validate contiguous sequence coverage for a replay range."""
        if expected_end < expected_start:
            return True

        expected = expected_start
        for packet in packets:
            seq_value = packet.get("sequence")
            if not isinstance(seq_value, int):
                return False
            if seq_value < expected:
                continue
            if seq_value > expected_end:
                break
            if seq_value != expected:
                return False
            expected += 1
            if expected > expected_end:
                return True
        return expected > expected_end

    def _build_resync_status_event(self, task_id: int, latest_sequence: int, source: str) -> Dict[str, Any]:
        """Return a status event instructing the client to reload history."""
        base_event = {
            "sequence": latest_sequence,
            "task_id": task_id,
            "type": "status",
            "content": "resync_required",
            "metadata": {
                "reason": "resync_required",
                "resync_required": True,
                "stream_source": source,
            },
            "timestamp": format_iso(utc_now()),
        }
        normalized = normalize_stream_packet(base_event, task_id=task_id)
        return normalized or base_event

    def _resolve_latest_sequence(self, task_id: int, after: int) -> int:
        """Return the latest sequence known to the in-memory hub."""
        latest = after
        try:
            hub = get_in_memory_stream_hub()
            hub_seq = hub.get_latest_sequence(task_id)
            if isinstance(hub_seq, int):
                latest = max(latest, hub_seq)
        except Exception:
            logger.debug("Unable to determine hub sequence for task %s", task_id, exc_info=True)
        return latest


__all__ = ["PersistedListAfter", "ReasoningSSEService"]
