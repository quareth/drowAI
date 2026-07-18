"""
In-memory streaming hub for Interactive chat mode.

This hub allows the backend to publish streaming events (delta chunks and
final assistant messages) directly to connected SSE clients while also
persisting normalized stream packets for history and replay.

Events shape (dict):
- type: str (e.g., "assistant_delta", "assistant_final")
- content: str
- metadata: dict (should include conversation_id, id (stable turn id), streaming flag for deltas)

Usage:
- from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
- hub = get_in_memory_stream_hub()
- await hub.publish(task_id, event)
- async for ev in hub.subscribe(task_id): ...
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Dict, Any, Set, List, Optional, Mapping
import logging
from dataclasses import dataclass
from datetime import datetime
from backend.core.time_utils import utc_now
from backend.services.chat.event_builders import build_user_message_event
from backend.services.metrics.utils import safe_inc, safe_gauge
from .stream_event_schema import normalize_stream_packet
from backend.database import SessionLocal
from .event_store import StreamEventStore, StreamEventTaskMissingError
from backend.config import E2E_DETERMINISTIC_MODE, REASONING_SSE_MAX_QUEUE
from runtime_shared.durable_secret_masking import mask_durable_secrets

logger = logging.getLogger("backend.services.in_memory_stream")
_SUBSCRIPTION_CLOSE_SENTINEL = object()


def _mask_replayable_stream_packet(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a persistence-only stream packet copy with durable secrets masked."""
    masked = mask_durable_secrets(dict(packet), source="stream_event.replay")
    return masked if isinstance(masked, dict) else dict(packet)


@dataclass
class QueuedMessage:
    """Represents a queued user message waiting to be sent."""
    content: str
    conversation_id: str
    user_id: int
    created_at: datetime
    task_id: int
    retry_count: int = 0
    client_message_id: Optional[str] = None
    user_message_id: Optional[int] = None
    assistant_message_id: Optional[int] = None
    turn_id: Optional[str] = None
    turn_number: Optional[int] = None
    user_sequence: Optional[int] = None
    anchor_sequence: Optional[int] = None
    user_event_published: bool = False
    requested_mode: Optional[Any] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    credential_ref: Optional[Dict[str, Any]] = None
    runtime_selection: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    deterministic_mode: bool = False
    # Phase 6: preserve the normalized (agent_mode, plan_mode) pair
    # across queue handoffs so a queued Plan-overlay turn still executes
    # with the forced deep-reasoning route and the right autonomy tier.
    agent_mode: Optional[Any] = None
    plan_mode: bool = False


class InMemoryStreamHub:
    def __init__(self) -> None:
        # Map of task_id -> set of subscriber queues
        self._subs: Dict[int, Set[asyncio.Queue]] = {}
        # Track last known sequence per task for reconnect handling
        self._last_sequences: Dict[int, int] = {}
        self._sequence_seeded: Set[int] = set()
        # Global lock for subscription map
        self._lock = asyncio.Lock()
        # Track streaming state per task
        self._streaming_tasks: Set[int] = set()
        # Queue for messages waiting to be sent
        self._message_queues: Dict[int, List[QueuedMessage]] = {}
        # Track running queue processors so we do not double-dispatch
        self._queue_processors: Dict[int, asyncio.Task] = {}
        # Track task readiness inputs for chat readiness events
        self._task_running: Dict[int, bool] = {}
        self._chat_metadata: Dict[int, Dict[str, Any]] = {}
        # Delay knobs to smooth out queue handoffs (configurable)
        try:
            from backend.config.container_config import get_container_config
            get_container_config()
            # Use environment variables if present; fall back to defaults
            import os
            self._queue_start_delay = float(os.getenv("INTERACTIVE_QUEUE_START_DELAY", "0.75"))
            self._queue_between_messages_delay = float(os.getenv("INTERACTIVE_QUEUE_BETWEEN_DELAY", "0.4"))
            self._max_queue_attempts = int(os.getenv("INTERACTIVE_QUEUE_MAX_ATTEMPTS", "3"))
            self._queue_retry_backoff = float(os.getenv("INTERACTIVE_QUEUE_RETRY_BACKOFF", "1.0"))
        except Exception:
            self._queue_start_delay = 0.75
            self._queue_between_messages_delay = 0.4
            self._max_queue_attempts = 3
            self._queue_retry_backoff = 1.0
        self._subscriber_queue_maxsize = max(1, int(REASONING_SSE_MAX_QUEUE))
        self._persistence_disabled_tasks: Set[int] = set()

    def _build_status_event(self, task_id: int) -> Dict[str, Any]:
        """Build a task-level streaming status event for subscribers."""
        return {
            "type": "status",
            "content": "streaming_state",
            "metadata": {
                "task_id": task_id,
                "is_streaming": self.is_task_streaming(task_id),
                "queued_count": self.get_queued_count(task_id),
            },
        }

    def _build_chat_ready_event(self, task_id: int) -> Dict[str, Any]:
        meta = self._chat_metadata.get(task_id, {})
        conversation_id = meta.get("conversation_id")
        checkpointer_ready = bool(meta.get("checkpointer_ready", False))
        task_running = bool(self._task_running.get(task_id, False))
        sse_connected = task_id in self._subs and len(self._subs.get(task_id, set())) > 0
        # Chat readiness should reflect backend ability to accept input, not stream state.
        # Stream connection status is reported separately via sse_connected.
        chat_ready = task_running and bool(conversation_id)
        return {
            "type": "status",
            "content": "chat_ready",
            "metadata": {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "checkpointer_ready": checkpointer_ready,
                "task_running": task_running,
                "sse_connected": sse_connected,
                "chat_ready": chat_ready,
            },
        }

    def _publish_status(self, task_id: int) -> None:
        """Publish streaming/queue status without blocking caller."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop when trying to publish status for task %s", task_id)
            return
        event = self._build_status_event(task_id)
        loop.create_task(self.publish(task_id, event))

    def _publish_chat_ready(self, task_id: int) -> None:
        """Publish chat readiness without blocking caller."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop when trying to publish chat readiness for task %s", task_id)
            return
        event = self._build_chat_ready_event(task_id)
        meta = event.get("metadata", {})
        logger.debug(
            "Chat readiness publish for task %s: running=%s checkpointer=%s sse=%s conv=%s ready=%s",
            task_id,
            meta.get("task_running"),
            meta.get("checkpointer_ready"),
            meta.get("sse_connected"),
            "set" if meta.get("conversation_id") else "missing",
            meta.get("chat_ready"),
        )
        loop.create_task(self.publish(task_id, event))

    def get_chat_ready_payload(self, task_id: int) -> Dict[str, Any]:
        """Return the chat readiness metadata for a task."""
        return self._build_chat_ready_event(task_id)["metadata"]

    async def _ensure_sequence_seeded(self, task_id: int) -> None:
        """Seed in-memory sequence counter from persistent store (once per task)."""
        if task_id in self._sequence_seeded:
            return
        latest = 0
        db = SessionLocal()
        try:
            latest = StreamEventStore(db).get_latest_sequence(task_id)
        except Exception:
            logger.debug("Failed to seed stream sequence for task %s", task_id, exc_info=True)
        finally:
            try:
                db.close()
            except Exception:
                pass
        async with self._lock:
            if task_id in self._sequence_seeded:
                return
            previous = self._last_sequences.get(task_id, 0)
            self._last_sequences[task_id] = max(previous, latest)
            self._sequence_seeded.add(task_id)

    def _persist_stream_packet(self, task_id: int, packet: Dict[str, Any]) -> None:
        if task_id in self._persistence_disabled_tasks:
            return
        db = SessionLocal()
        try:
            persisted = StreamEventStore(db).append_packet(
                task_id,
                _mask_replayable_stream_packet(packet),
            )
            if persisted is None:
                logger.warning(
                    "Stream event persistence returned no row for task_id=%s sequence=%s",
                    task_id,
                    packet.get("sequence"),
                )
        except StreamEventTaskMissingError:
            self._persistence_disabled_tasks.add(task_id)
            logger.info(
                "Disabling stream event persistence for missing task_id=%s",
                task_id,
            )
        except Exception:
            logger.exception(
                "Stream event persistence failed for task_id=%s sequence=%s",
                task_id,
                packet.get("sequence"),
            )
            raise
        finally:
            try:
                db.close()
            except Exception:
                pass

    async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
        """Publish an event to all subscribers of the given task.

        On queue overflow, disconnect the subscriber so it can reconnect and
        replay missing packets from persisted history.
        """
        normalized = normalize_stream_packet(event, task_id=task_id)
        if normalized is None:
            logger.warning("Dropping invalid stream event for task %s", task_id)
            return
        event = normalized
        await self._ensure_sequence_seeded(task_id)
        queues: Set[asyncio.Queue]
        async with self._lock:
            queues = set(self._subs.get(task_id, set()))
            previous = self._last_sequences.get(task_id, 0)
            seq_hint = previous + 1
            event["sequence"] = seq_hint
            self._last_sequences[task_id] = seq_hint
        if not event.get("task_id"):
            event["task_id"] = task_id
        self._persist_stream_packet(task_id, event)
        if not queues:
            safe_inc("interactive_stream_no_subscriber_events")
            logger.debug(
                "InMemoryStreamHub publish without subscribers task=%s sequence=%s",
                task_id,
                event.get("sequence"),
            )
            return
        subscriber_count = len(queues)
        overflowed_queues: list[asyncio.Queue] = []
        for q in queues:
            try:
                # Keep small buffer to avoid slow consumers blocking publishers
                q.put_nowait(event)
            except asyncio.QueueFull:
                overflowed_queues.append(q)
                safe_inc("interactive_stream_queue_drops")
                safe_inc("interactive_stream_overflow_disconnects")
                logger.warning(
                    "InMemoryStreamHub queue overflow task=%s sequence=%s subscribers=%s; disconnecting subscriber for replay recovery",
                    task_id,
                    event.get("sequence"),
                    subscriber_count,
                )
        if overflowed_queues:
            async with self._lock:
                subs_for_task = self._subs.get(task_id)
                if subs_for_task:
                    for overflowed_queue in overflowed_queues:
                        subs_for_task.discard(overflowed_queue)
                    if not subs_for_task:
                        self._subs.pop(task_id, None)

            for overflowed_queue in overflowed_queues:
                try:
                    # Ensure the subscriber loop exits so the client reconnect path can replay.
                    overflowed_queue.put_nowait(_SUBSCRIPTION_CLOSE_SENTINEL)
                except asyncio.QueueFull:
                    try:
                        overflowed_queue.get_nowait()
                        overflowed_queue.put_nowait(_SUBSCRIPTION_CLOSE_SENTINEL)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        logger.debug(
                            "Failed to enqueue close sentinel for overflowed subscriber task=%s sequence=%s",
                            task_id,
                            event.get("sequence"),
                        )

    async def subscribe(self, task_id: int) -> AsyncGenerator[Dict[str, Any], None]:
        """Subscribe to events for a task. Yields events as they arrive.

        Caller should cancel/exit iteration to unsubscribe.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_queue_maxsize)
        async with self._lock:
            subs_for_task = self._subs.setdefault(task_id, set())
            subs_for_task.add(queue)
            total_subs = sum(len(subs) for subs in self._subs.values())
        try:
            self._publish_chat_ready(task_id)
            safe_gauge("interactive_stream_active_subscriptions", total_subs)
            while True:
                event = await queue.get()
                if event is _SUBSCRIPTION_CLOSE_SENTINEL:
                    break
                yield event
        except asyncio.CancelledError:
            # Normal cancellation path on disconnect
            pass
        finally:
            async with self._lock:
                subs = self._subs.get(task_id)
                if subs and queue in subs:
                    subs.remove(queue)
                if subs is not None and not subs:
                    self._subs.pop(task_id, None)
                total_subs = sum(len(subs) for subs in self._subs.values())
            self._publish_chat_ready(task_id)
            safe_gauge("interactive_stream_active_subscriptions", total_subs)

    async def remove_task(self, task_id: int) -> None:
        """Cleanup hub state for a deleted task and close active subscribers."""
        self._persistence_disabled_tasks.add(task_id)
        self._streaming_tasks.discard(task_id)
        self._task_running.pop(task_id, None)
        self._chat_metadata.pop(task_id, None)
        self._last_sequences.pop(task_id, None)
        self._sequence_seeded.discard(task_id)
        self._message_queues.pop(task_id, None)

        processor = self._queue_processors.pop(task_id, None)
        if processor and not processor.done():
            processor.cancel()

        async with self._lock:
            subscribers = set(self._subs.pop(task_id, set()))
            total_subs = sum(len(subs) for subs in self._subs.values())

        for queue in subscribers:
            try:
                queue.put_nowait(_SUBSCRIPTION_CLOSE_SENTINEL)
            except asyncio.QueueFull:
                logger.debug("Subscriber queue full while closing deleted task stream task_id=%s", task_id)

        safe_gauge("interactive_stream_active_subscriptions", total_subs)

    def get_latest_sequence(self, task_id: int) -> Optional[int]:
        """Return the most recent sequence observed for a task, if any."""
        seq = self._last_sequences.get(task_id)
        if seq is not None:
            return seq
        db = SessionLocal()
        try:
            return StreamEventStore(db).get_latest_sequence(task_id)
        except Exception:
            logger.debug("Failed to read latest stream sequence for task %s", task_id, exc_info=True)
            return None
        finally:
            try:
                db.close()
            except Exception:
                pass

    def has_subscribers(self, task_id: int) -> bool:
        """Return True when there are active stream subscribers for a task."""
        return bool(self._subs.get(task_id))

    def is_task_streaming(self, task_id: int) -> bool:
        """Check if a task is currently streaming."""
        return task_id in self._streaming_tasks

    def set_streaming_state(self, task_id: int, is_streaming: bool) -> None:
        """Set the streaming state for a task."""
        logger.info(f"Setting streaming state for task {task_id}: {is_streaming}")
        if is_streaming:
            self._streaming_tasks.add(task_id)
        else:
            self._streaming_tasks.discard(task_id)
            logger.info(f"Streaming stopped for task {task_id}, scheduling queued message processing")
            self._schedule_queue_processing(task_id)
        # Publish status so subscribers (and UI) can stay in sync with the backend truth
        self._publish_status(task_id)

    def set_task_running(self, task_id: int, is_running: bool) -> None:
        """Update cached task running state and emit readiness event."""
        self._task_running[task_id] = bool(is_running)
        self._publish_chat_ready(task_id)

    def update_chat_metadata(
        self,
        task_id: int,
        conversation_id: Optional[str],
        checkpointer_ready: bool,
    ) -> None:
        """Update cached chat metadata and emit readiness event."""
        self._chat_metadata[task_id] = {
            "conversation_id": conversation_id,
            "checkpointer_ready": bool(checkpointer_ready),
        }
        self._publish_chat_ready(task_id)

    def queue_message(
        self,
        task_id: int,
        content: str,
        conversation_id: str,
        user_id: int,
        client_message_id: Optional[str] = None,
        *,
        user_message_id: Optional[int] = None,
        assistant_message_id: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_number: Optional[int] = None,
        user_sequence: Optional[int] = None,
        anchor_sequence: Optional[int] = None,
        user_event_published: bool = False,
        requested_mode: Optional[Any] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        credential_ref: Optional[Dict[str, Any]] = None,
        runtime_selection: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        deterministic_mode: bool = False,
        agent_mode: Optional[Any] = None,
        plan_mode: bool = False,
    ) -> None:
        """Queue a user message for later sending when streaming stops."""
        if task_id not in self._message_queues:
            self._message_queues[task_id] = []

        queued_msg = QueuedMessage(
            content=content,
            conversation_id=conversation_id,
            user_id=user_id,
            created_at=utc_now(),
            task_id=task_id,
            retry_count=0,
            client_message_id=client_message_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            turn_id=turn_id,
            turn_number=turn_number,
            user_sequence=user_sequence,
            anchor_sequence=anchor_sequence,
            user_event_published=user_event_published,
            requested_mode=requested_mode,
            provider=provider,
            model=model,
            credential_ref=dict(credential_ref) if isinstance(credential_ref, dict) else None,
            runtime_selection=(
                dict(runtime_selection)
                if isinstance(runtime_selection, dict)
                else None
            ),
            reasoning_effort=reasoning_effort,
            deterministic_mode=bool(deterministic_mode),
            agent_mode=agent_mode,
            plan_mode=bool(plan_mode),
        )
        self._message_queues[task_id].append(queued_msg)
        logger.info(f"Queued message for task {task_id}: {content[:50]}...")
        # Status update after enqueue
        self._publish_status(task_id)
        if not self.is_task_streaming(task_id):
            logger.debug(f"Task {task_id} not streaming when message queued; scheduling processor")
            self._schedule_queue_processing(task_id)

    def _schedule_queue_processing(self, task_id: int) -> None:
        """Ensure a queue processor is scheduled for a task."""
        queue = self._message_queues.get(task_id)
        if not queue:
            logger.debug(f"No queued messages for task {task_id}; nothing to schedule")
            return

        existing = self._queue_processors.get(task_id)
        if existing and not existing.done():
            logger.debug(f"Queue processor already active for task {task_id}")
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"No running event loop available to process queue for task {task_id}")
            return

        task = loop.create_task(self._process_queue(task_id))
        self._queue_processors[task_id] = task

        def _cleanup(fut: asyncio.Task, *, tid: int = task_id) -> None:
            self._queue_processors.pop(tid, None)
            if fut.cancelled():
                logger.info(f"Queue processor for task {tid} cancelled")
            else:
                exc = fut.exception()
                if exc:
                    logger.error(f"Queue processor for task {tid} failed: {exc}", exc_info=True)

        task.add_done_callback(_cleanup)

    async def _process_queue(self, task_id: int) -> None:
        """Drain queued messages with gentle pacing to avoid race conditions."""
        logger.info(f"Starting queue processor for task {task_id}")
        try:
            await asyncio.sleep(self._queue_start_delay)
            while True:
                queue = self._message_queues.get(task_id)
                if not queue:
                    logger.debug(f"Queue exhausted for task {task_id}")
                    return

                if self.is_task_streaming(task_id):
                    logger.info(f"Task {task_id} resumed streaming; leaving {len(queue)} queued message(s)")
                    return

                queued_msg = queue.pop(0)
                processed = await self._dispatch_queued_message(queued_msg)

                if not processed:
                    continue

                if not self._message_queues.get(task_id):
                    return

                await asyncio.sleep(self._queue_between_messages_delay)
        finally:
            if not self._message_queues.get(task_id):
                self._message_queues.pop(task_id, None)
                self._publish_status(task_id)
            logger.info(f"Queue processor finished for task {task_id}")

    async def _dispatch_queued_message(self, queued_msg: QueuedMessage) -> bool:
        """Emit and process a single queued user message."""
        task_id = queued_msg.task_id
        if not queued_msg.user_event_published:
            extra_meta = {"queued": True}
            if queued_msg.client_message_id:
                extra_meta["client_message_id"] = queued_msg.client_message_id
            if queued_msg.turn_id:
                extra_meta["id"] = queued_msg.turn_id
            extra_meta["ind"] = -1
            if isinstance(queued_msg.turn_number, int):
                extra_meta["turn_sequence"] = queued_msg.turn_number
            elif isinstance(queued_msg.anchor_sequence, int):
                extra_meta["turn_sequence"] = queued_msg.anchor_sequence
            user_event = build_user_message_event(
                queued_msg.content,
                queued_msg.conversation_id,
                extra_meta,
            )
            if isinstance(queued_msg.user_sequence, int):
                user_event["sequence"] = queued_msg.user_sequence
                user_event["metadata"]["sequence"] = queued_msg.user_sequence
                if isinstance(queued_msg.turn_number, int):
                    user_event["metadata"]["turn_sequence"] = queued_msg.turn_number
                elif isinstance(queued_msg.anchor_sequence, int):
                    user_event["metadata"]["turn_sequence"] = queued_msg.anchor_sequence
            try:
                await self.publish(task_id, user_event)
            except Exception as exc:
                logger.warning(f"Failed to publish queued user message for task {task_id}: {exc}")
            queued_msg.user_event_published = True

        processed = await self._process_queued_message_with_llm(queued_msg)
        if not processed:
            queued_msg.retry_count += 1
            if queued_msg.retry_count > self._max_queue_attempts:
                logger.error(
                    f"Dropping queued message for task {task_id} after {queued_msg.retry_count} failed attempts"
                )
                return False

            logger.warning(
                f"Re-queueing message for task {task_id}; retry #{queued_msg.retry_count}"
            )
            queue = self._message_queues.setdefault(task_id, [])
            queue.insert(0, queued_msg)
            backoff = min(queued_msg.retry_count, 3) * self._queue_retry_backoff
            await asyncio.sleep(backoff)
            return False

        logger.info(f"Dispatched queued message for task {task_id}: {queued_msg.content[:50]}...")
        # Status update after successful dispatch
        self._publish_status(task_id)
        return True

    async def _process_queued_message_with_llm(self, queued_msg: QueuedMessage) -> bool:
        """Process a queued message with LLM generation."""
        try:
            # Import here to avoid circular imports
            from backend.database import SessionLocal
            from backend.models.core import User
            from backend.services.chat.conversation_history_reader import ConversationHistoryReader
            from backend.services.langgraph_chat.contracts import ExecutionMode
            from backend.services.langgraph_chat.execution.turn_service import (
                get_turn_execution_service,
            )
            from backend.services.llm_provider import LLMRuntimeConfigService

            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == queued_msg.user_id).first()
                if not user:
                    logger.error(f"User {queued_msg.user_id} not found for queued message")
                    return False

                runtime_config_service = LLMRuntimeConfigService(db)
                if queued_msg.runtime_selection is not None:
                    runtime_selection_payload = dict(queued_msg.runtime_selection)
                    resolved_provider = queued_msg.provider
                    resolved_model = queued_msg.model
                else:
                    runtime_selection = runtime_config_service.build_conversation_runtime_selection(
                        user_id=user.id,
                        provider=queued_msg.provider,
                        model=queued_msg.model,
                        reasoning_effort=queued_msg.reasoning_effort,
                        require_enabled_credential=not (
                            queued_msg.deterministic_mode or E2E_DETERMINISTIC_MODE
                        ),
                    )
                    runtime_selection_payload = runtime_selection.to_dict()
                    resolved_provider = (
                        getattr(runtime_selection, "provider", None)
                        or getattr(runtime_selection, "legacy_provider", None)
                    )
                    resolved_model = (
                        getattr(runtime_selection, "model", None)
                        or getattr(runtime_selection, "legacy_model", None)
                    )
                exclude_ids: set[int] = set()
                if isinstance(queued_msg.user_message_id, int):
                    exclude_ids.add(queued_msg.user_message_id)
                if isinstance(queued_msg.assistant_message_id, int):
                    exclude_ids.add(queued_msg.assistant_message_id)
                aligned_history = ConversationHistoryReader(
                    db
                ).build_aligned_openai_conversation_history(
                    task_id=queued_msg.task_id,
                    conversation_id=queued_msg.conversation_id,
                    exclude_message_ids=exclude_ids or None,
                )
                history = list(aligned_history.messages)

                self.set_streaming_state(queued_msg.task_id, True)
                anchor_sequence = queued_msg.anchor_sequence
                if anchor_sequence is None and isinstance(queued_msg.assistant_message_id, int):
                    anchor_sequence = queued_msg.assistant_message_id

                # Keep queued dispatch on the same execution entrypoint as interactive turns.
                # This guarantees queue processing inherits pre-turn context/compression behavior.
                turn_execution_service = get_turn_execution_service()
                await turn_execution_service.start_turn_generation(
                    task_id=queued_msg.task_id,
                    user_id=queued_msg.user_id,
                    provider=resolved_provider,
                    model=resolved_model,
                    runtime_selection=runtime_selection_payload,
                    reasoning_effort=queued_msg.reasoning_effort,
                    message=queued_msg.content,
                    conversation_id=queued_msg.conversation_id,
                    history=history,
                    history_source_message_ids=list(
                        aligned_history.source_message_ids
                    ),
                    anchor_sequence=anchor_sequence,
                    requested_mode=(
                        queued_msg.requested_mode
                        if queued_msg.requested_mode is not None
                        else (ExecutionMode.SIMPLE_TOOL if E2E_DETERMINISTIC_MODE else None)
                    ),
                    agent_mode=queued_msg.agent_mode,
                    plan_mode=bool(queued_msg.plan_mode),
                    turn_id=queued_msg.turn_id,
                    turn_number=queued_msg.turn_number,
                    reserved_message_id=queued_msg.assistant_message_id,
                    deterministic_mode=queued_msg.deterministic_mode or E2E_DETERMINISTIC_MODE,
                )
                return True
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Failed to process queued message: {e}", exc_info=True)
            self.set_streaming_state(queued_msg.task_id, False)
            return False
    def get_queued_count(self, task_id: int) -> int:
        """Get the number of queued messages for a task."""
        return len(self._message_queues.get(task_id, []))

    def clear_queue(self, task_id: int) -> None:
        """Clear all queued messages for a task."""
        if task_id in self._message_queues:
            self._message_queues.pop(task_id, None)
        processor = self._queue_processors.get(task_id)
        if processor and not processor.done():
            processor.cancel()
        self._publish_status(task_id)


_hub: InMemoryStreamHub | None = None


def get_in_memory_stream_hub() -> InMemoryStreamHub:
    global _hub
    if _hub is None:
        _hub = InMemoryStreamHub()
    return _hub
