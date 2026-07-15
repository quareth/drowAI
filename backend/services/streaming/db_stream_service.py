"""
Database Stream Service for agent reasoning logs.

This service provides centralized DB-backed streaming operations for agent reasoning logs,
enabling efficient replay and real-time streaming with proper backpressure handling.

Design Patterns:
- Singleton pattern: Single instance per application lifecycle
- Observer pattern: Subscription-based event distribution
- Factory pattern: Create appropriate stream types based on configuration
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import AsyncGenerator, Dict, Any, Callable, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime


from backend.config import (
    DB_STREAM_POLL_INTERVAL_MS,
    DB_STREAM_REPLAY_BATCH_SIZE,
    DB_STREAM_MAX_CONNECTIONS_PER_TASK,
)
from .reasoning_store import AgentReasoningStore
from backend.database import SessionLocal
from backend.services.metrics import metrics

logger = logging.getLogger("backend.services.db_stream_service")


@dataclass
class StreamSubscription:
    """Represents an active subscription to a task's reasoning stream."""

    subscription_id: str
    task_id: int
    callback: Callable[[Dict[str, Any]], None]
    last_sequence: int
    created_at: datetime = field(default_factory=datetime.now)
    is_active: bool = True


class DatabaseStreamService:
    """
    Centralized service for DB-backed streaming operations.

    Provides replay of historical events and real-time streaming of new events
    with proper backpressure handling and connection management.
    """

    def __init__(self):
        self._subscriptions: Dict[str, StreamSubscription] = {}
        self._task_subscriptions: Dict[int, Set[str]] = {}
        self._polling_tasks: Dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._metrics = {
            "active_subscriptions": 0,
            "total_events_sent": 0,
            "total_replay_events": 0,
            "total_poll_operations": 0,
        }
        # Track per-task connections to enforce limits
        self._connections_per_task: Dict[int, int] = {}

    async def replay_events(
        self,
        task_id: int,
        after: int,
        limit: int = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Replay historical events from database.

        Args:
            task_id: The task ID to replay events for
            after: Replay events with sequence > after
            limit: Maximum number of events to replay (defaults to config)

        Yields:
            Event dictionaries with sequence, content, metadata, etc.
        """
        if limit is None:
            limit = DB_STREAM_REPLAY_BATCH_SIZE

        logger.debug("Starting replay for task %s after sequence %s (limit: %s)",
                    task_id, after, limit)

        try:
            db = SessionLocal()
            store = AgentReasoningStore(db)

            # Use a cursor based on the last emitted sequence to avoid gaps
            cursor = after
            while True:
                batch = store.list_after(task_id, cursor, limit)
                if not batch:
                    break

                for event in batch:
                    event_dict = self._convert_to_event_dict(event)
                    yield event_dict
                    cursor = event.sequence  # advance cursor to last emitted sequence
                    self._metrics["total_replay_events"] += 1
                    metrics.inc("db_stream_replay_events")

                if len(batch) < limit:
                    break

        except Exception as e:
            logger.error("Error during replay for task %s: %s", task_id, e, exc_info=True)
            raise
        finally:
            try:
                db.close()
            except Exception:
                pass

    async def stream_new_events(
        self,
        task_id: int,
        last_sequence: int
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream new events as they are added to the database.

        Args:
            task_id: The task ID to stream events for
            last_sequence: Only stream events with sequence > last_sequence

        Yields:
            New event dictionaries as they become available
        """
        logger.debug("Starting new event stream for task %s from sequence %s",
                    task_id, last_sequence)

        poll_interval = DB_STREAM_POLL_INTERVAL_MS / 1000.0
        current_sequence = last_sequence

        try:
            while True:
                # Check for new events
                db = SessionLocal()
                store = AgentReasoningStore(db)

                try:
                    new_events = store.poll_new_events(task_id, current_sequence, 100)

                    for event in new_events:
                        event_dict = self._convert_to_event_dict(event)
                        yield event_dict
                        current_sequence = event.sequence
                        self._metrics["total_events_sent"] += 1

                finally:
                    db.close()

                self._metrics["total_poll_operations"] += 1
                metrics.inc("db_stream_poll_operations")

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.debug("Stream cancelled for task %s", task_id)
            raise
        except Exception as e:
            logger.error("Error in new event stream for task %s: %s", task_id, e, exc_info=True)
            raise

    async def get_latest_sequence(self, task_id: int) -> int:
        """
        Get the latest sequence number for a task.

        Args:
            task_id: The task ID

        Returns:
            The latest sequence number, or 0 if no events exist
        """
        try:
            db = SessionLocal()
            store = AgentReasoningStore(db)
            latest = store.get_latest_sequence(task_id)
            return latest
        except Exception as e:
            logger.error("Error getting latest sequence for task %s: %s", task_id, e)
            return 0
        finally:
            try:
                db.close()
            except Exception:
                pass

    def create_subscription(
        self,
        task_id: int,
        callback: Callable[[Dict[str, Any]], None],
        last_sequence: int = 0
    ) -> str:
        """
        Create a subscription to receive events for a task.

        Args:
            task_id: The task ID to subscribe to
            callback: Function to call with new events
            last_sequence: Start from events after this sequence

        Returns:
            Subscription ID for later management
        """
        subscription_id = str(uuid.uuid4())

        subscription = StreamSubscription(
            subscription_id=subscription_id,
            task_id=task_id,
            callback=callback,
            last_sequence=last_sequence
        )

        async def _create_subscription():
            async with self._lock:
                # Enforce per-task connection limits
                current = self._connections_per_task.get(task_id, 0)
                if current >= DB_STREAM_MAX_CONNECTIONS_PER_TASK:
                    logger.warning("Per-task connection limit reached for task %s", task_id)
                    return  # Silent drop; caller may handle via metrics/health
                self._connections_per_task[task_id] = current + 1
                self._subscriptions[subscription_id] = subscription

                if task_id not in self._task_subscriptions:
                    self._task_subscriptions[task_id] = set()
                self._task_subscriptions[task_id].add(subscription_id)

                # Start polling task if not already running
                if task_id not in self._polling_tasks:
                    self._polling_tasks[task_id] = asyncio.create_task(
                        self._poll_task_events(task_id)
                    )

                self._metrics["active_subscriptions"] = len(self._subscriptions)
                logger.debug("Created subscription %s for task %s", subscription_id, task_id)

        # Schedule the creation in the event loop
        asyncio.create_task(_create_subscription())

        return subscription_id

    def remove_subscription(self, subscription_id: str) -> None:
        """
        Remove a subscription and clean up resources.

        Args:
            subscription_id: The subscription ID to remove
        """
        async def _remove_subscription():
            async with self._lock:
                if subscription_id not in self._subscriptions:
                    return

                subscription = self._subscriptions[subscription_id]
                task_id = subscription.task_id

                # Remove from subscriptions
                del self._subscriptions[subscription_id]

                # Remove from task subscriptions
                if task_id in self._task_subscriptions:
                    self._task_subscriptions[task_id].discard(subscription_id)

                    # If no more subscriptions for this task, stop polling
                    if not self._task_subscriptions[task_id]:
                        del self._task_subscriptions[task_id]

                        # Cancel polling task
                        if task_id in self._polling_tasks:
                            self._polling_tasks[task_id].cancel()
                            del self._polling_tasks[task_id]
                # Decrement connection count
                if task_id in self._connections_per_task:
                    self._connections_per_task[task_id] = max(0, self._connections_per_task[task_id] - 1)
                    if self._connections_per_task[task_id] == 0:
                        del self._connections_per_task[task_id]

                self._metrics["active_subscriptions"] = len(self._subscriptions)
                logger.debug("Removed subscription %s for task %s", subscription_id, task_id)

        # Schedule the removal in the event loop
        asyncio.create_task(_remove_subscription())

    async def _poll_task_events(self, task_id: int) -> None:
        """
        Poll for new events for a specific task and distribute to subscribers.

        Args:
            task_id: The task ID to poll for
        """
        poll_interval = DB_STREAM_POLL_INTERVAL_MS / 1000.0

        try:
            while True:
                async with self._lock:
                    if task_id not in self._task_subscriptions:
                        break

                    subscriptions = [
                        self._subscriptions[sub_id]
                        for sub_id in self._task_subscriptions[task_id]
                        if sub_id in self._subscriptions and self._subscriptions[sub_id].is_active
                    ]

                if not subscriptions:
                    break

                # Get new events for all subscribers
                try:
                    db = SessionLocal()
                    store = AgentReasoningStore(db)

                    # Find the minimum last_sequence to optimize query
                    min_sequence = min(sub.last_sequence for sub in subscriptions)
                    new_events = store.poll_new_events(task_id, min_sequence, 100)

                    # Distribute events to relevant subscribers
                    for event in new_events:
                        event_dict = self._convert_to_event_dict(event)

                        for subscription in subscriptions:
                            if event.sequence > subscription.last_sequence:
                                try:
                                    subscription.callback(event_dict)
                                    subscription.last_sequence = event.sequence
                                    self._metrics["total_events_sent"] += 1
                                    metrics.inc("db_stream_events_sent")
                                except Exception as e:
                                    logger.error("Error in subscription callback %s: %s",
                                               subscription.subscription_id, e)
                                    subscription.is_active = False

                finally:
                    db.close()

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.debug("Polling cancelled for task %s", task_id)
        except Exception as e:
            logger.error("Error polling events for task %s: %s", task_id, e, exc_info=True)

    def _convert_to_event_dict(self, agent_log) -> Dict[str, Any]:
        """
        Convert SystemLog model to event dictionary format.

        Args:
            agent_log: SystemLog model instance

        Returns:
            Event dictionary with standardized format
        """
        return {
            "sequence": agent_log.sequence,
            "task_id": agent_log.task_id,
            "type": agent_log.type,
            "content": agent_log.content,
            "metadata": agent_log.log_metadata or {},
            "timestamp": agent_log.timestamp.isoformat() if agent_log.timestamp else None,
        }

    def get_metrics(self) -> Dict[str, Any]:
        """
        Get current service metrics.

        Returns:
            Dictionary of current metrics
        """
        return {
            **self._metrics,
            "active_tasks": len(self._task_subscriptions),
            "polling_tasks": len(self._polling_tasks),
        }

    async def shutdown(self) -> None:
        """
        Gracefully shutdown the service and clean up resources.
        """
        logger.info("Shutting down DatabaseStreamService")

        # Cancel all polling tasks
        for task in self._polling_tasks.values():
            task.cancel()

        # Wait for tasks to complete
        if self._polling_tasks:
            await asyncio.gather(*self._polling_tasks.values(), return_exceptions=True)

        # Clear all subscriptions
        self._subscriptions.clear()
        self._task_subscriptions.clear()
        self._polling_tasks.clear()

        logger.info("DatabaseStreamService shutdown complete")


# Global instance following singleton pattern
_db_stream_service: Optional[DatabaseStreamService] = None


def get_db_stream_service() -> DatabaseStreamService:
    """
    Get the global DatabaseStreamService instance.

    Returns:
        The singleton DatabaseStreamService instance
    """
    global _db_stream_service
    if _db_stream_service is None:
        _db_stream_service = DatabaseStreamService()
    return _db_stream_service


async def shutdown_db_stream_service() -> None:
    """
    Shutdown the global DatabaseStreamService instance.
    """
    global _db_stream_service
    if _db_stream_service is not None:
        await _db_stream_service.shutdown()
        _db_stream_service = None
