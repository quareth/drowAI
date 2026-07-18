"""Usage tracking middleware for LangGraph handlers.

This module provides utilities for capturing and recording token usage
from LLM calls made during LangGraph execution. It bridges the gap between
the LLM providers (which now return usage data) and the handlers
(which have access to DB sessions).

Key components:
- UsageCollector: Accumulates usage from multiple LLM calls during a turn
- record_turn_usage: Helper to persist collected usage to database

Usage in handlers:
    async def handle(self, runtime_config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        collector = UsageCollector()
        
        # Pass collector to graph execution context
        # ... execute graph ...
        
        # After execution, persist collected usage
        record_turn_usage(
            db=db,
            task_id=chat_inputs.task_id,
            user_id=chat_inputs.user_id,
            collector=collector,
            source="langgraph_normal",
            conversation_id=conversation_id,
        )
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from backend.services.usage_tracking.models import UsageData
    from backend.services.usage_tracking.insights_models import UsageRecordMetadata

logger = logging.getLogger(__name__)


class UsageCollector:
    """Collects token usage from multiple LLM calls during a single turn.
    
    This is a lightweight accumulator that doesn't require database access.
    It can be passed through the graph execution context and used by nodes
    to report usage. After the turn completes, a handler can persist all
    collected usage to the database.
    
    Thread-safety: Not thread-safe. Create one instance per turn/request.
    
    Usage:
        collector = UsageCollector()
        
        # In LLM-calling code:
        response = await client.chat_messages_with_usage(messages)
        collector.add(response.usage, source="llm_call_1")
        
        # After turn:
        for entry in collector.entries:
            service.record_usage(task_id, user_id, entry.usage, entry.source)
    """
    
    def __init__(self) -> None:
        """Initialize empty collector."""
        self._entries: List[Dict[str, Any]] = []
    
    def add(
        self,
        usage: Optional["UsageData"],
        source: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add usage data to the collector.
        
        Args:
            usage: UsageData from an LLM call (can be None if unavailable)
            source: Identifier for the call (e.g., "simple_chat", "tool_select")
            metadata: Optional additional metadata to store with the usage
        """
        if usage is None or usage.is_empty():
            logger.debug(f"Skipping empty usage from source={source}")
            return
        
        self._entries.append({
            "usage": usage,
            "source": source,
            "metadata": metadata or {},
        })
        
        logger.debug(
            f"Collected usage: source={source}, "
            f"tokens={usage.total_tokens} ({usage.prompt_tokens}+{usage.completion_tokens})"
        )
    
    @property
    def entries(self) -> List[Dict[str, Any]]:
        """Get all collected usage entries."""
        return self._entries
    
    @property
    def total_tokens(self) -> int:
        """Get total tokens across all entries."""
        return sum(
            entry["usage"].total_tokens 
            for entry in self._entries 
            if entry.get("usage")
        )
    
    @property
    def is_empty(self) -> bool:
        """Check if no usage has been collected."""
        return len(self._entries) == 0
    
    def clear(self) -> None:
        """Clear all collected entries."""
        self._entries.clear()
    
    def __len__(self) -> int:
        """Return number of collected entries."""
        return len(self._entries)


def record_turn_usage(
    db: "Session",
    task_id: int,
    user_id: int,
    collector: UsageCollector,
    source: str,
    conversation_id: Optional[str] = None,
) -> int:
    """Persist all collected usage from a turn to the database.
    
    This is a convenience function for handlers to persist all usage
    collected during a turn in a single call.
    
    Args:
        db: SQLAlchemy database session
        task_id: ID of the task
        user_id: ID of the user
        collector: UsageCollector with accumulated usage
        source: Base source identifier (will be combined with entry source)
        conversation_id: Optional conversation ID for tracking
        
    Returns:
        Number of usage records successfully persisted
    """
    if collector.is_empty:
        logger.debug(f"No usage to record for task {task_id}")
        return 0
    
    from backend.services.usage_tracking.service import UsageTrackingService
    
    service = UsageTrackingService(db)
    recorded_count = 0
    
    for entry in collector.entries:
        usage = entry["usage"]
        entry_source = entry.get("source", "unknown")
        entry_metadata = entry.get("metadata", {})
        usage_metadata = _metadata_from_collector_entry(
            usage=usage,
            source=entry_source,
            metadata=entry_metadata,
        )
        
        # Combine base source with entry source
        full_source = f"{source}:{entry_source}" if entry_source != "unknown" else source
        
        result = service.record_usage(
            task_id=task_id,
            user_id=user_id,
            usage=usage,
            source=full_source,
            conversation_id=conversation_id,
            metadata=entry_metadata,
            usage_metadata=usage_metadata,
        )
        
        if result is not None:
            recorded_count += 1
    
    logger.info(
        f"Recorded {recorded_count}/{len(collector)} usage entries for task {task_id}"
    )
    return recorded_count


def record_usage_list_best_effort(
    *,
    task_id: int,
    user_id: int,
    usage_list: Optional[List[Any]],
    source: str,
    conversation_id: Optional[str],
    model: Optional[str] = None,
    runtime_selection: Optional[Mapping[str, Any]] = None,
    session_factory: Optional[Callable[[], "Session"]] = None,
) -> None:
    """Best-effort helper to persist a list of usage records.

    Each item in ``usage_list`` may be either:

    * a plain ``UsageData`` (legacy / lightweight callers), or
    * a ``UsageRecordWithMetadata`` envelope produced by LangGraph handlers
      so canonical per-call metadata (role, node_name, execution_branch,
      provider, api_surface, request_mode, cache_reporting, turn_index)
      survives into ``LLMUsageRecord.request_metadata`` without a second
      persistence path.

    The coarse ``source`` argument is preserved on the row for routing /
    debug compatibility with existing ``/usage`` / ``/usage/breakdown``
    consumers, but the insights read layer groups on
    ``request_metadata.role`` / ``node_name`` instead of re-parsing it.
    """
    _ = model
    if not usage_list:
        return
    usage_identity = _usage_identity_from_runtime_selection(runtime_selection)
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        from backend.services.usage_tracking.insights_models import (
            UsageRecordWithMetadata,
        )
        from backend.services.usage_tracking.service import UsageTrackingService

        service = UsageTrackingService(db)
        for item in usage_list:
            if item is None:
                continue
            if isinstance(item, UsageRecordWithMetadata):
                usage = item.usage
                usage_metadata = item.metadata
            else:
                usage = item
                usage_metadata = None
            service.record_usage(
                task_id=task_id,
                user_id=user_id,
                usage=usage,
                source=source,
                conversation_id=conversation_id,
                usage_metadata=usage_metadata,
                **usage_identity,
            )
    except Exception:
        logger.warning("Failed to record usage for task %s", task_id, exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _usage_identity_from_runtime_selection(
    runtime_selection: Optional[Mapping[str, Any]],
) -> dict[str, str]:
    """Return deployment refs safe to pass to usage persistence."""

    if not isinstance(runtime_selection, Mapping):
        return {}
    deployment_ref = runtime_selection.get("deployment_ref")
    if not isinstance(deployment_ref, Mapping):
        return {}
    deployment_id = deployment_ref.get("deployment_id")
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        return {}
    identity = {"deployment_id": deployment_id.strip()}
    route_id = runtime_selection.get("preferred_route_id")
    if isinstance(route_id, str) and route_id.strip():
        identity["route_id"] = route_id.strip()
    return identity


def _known_request_mode(value: Any) -> str:
    """Return a canonical request mode or ``"unknown"`` for legacy callers."""
    return value if value in {"streaming", "non_streaming"} else "unknown"


def _metadata_from_collector_entry(
    *,
    usage: "UsageData",
    source: str,
    metadata: Any,
) -> Optional["UsageRecordMetadata"]:
    """Build canonical metadata for UsageCollector-backed writes when possible."""
    if not isinstance(metadata, dict):
        return None

    has_canonical_signal = any(
        key in metadata
        for key in (
            "role",
            "node_name",
            "execution_branch",
            "provider",
            "api_surface",
            "request_mode",
            "cache_reporting",
            "turn_index",
        )
    )
    if not has_canonical_signal:
        return None

    from backend.services.usage_tracking.insights_models import (
        UNKNOWN,
        UsageRecordMetadata,
        role_and_node_from_source,
    )

    role, node_name = role_and_node_from_source(source)
    turn_index = metadata.get("turn_index")
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        turn_index = None
    return UsageRecordMetadata(
        role=str(metadata.get("role") or role or UNKNOWN),
        node_name=str(metadata.get("node_name") or node_name or UNKNOWN),
        execution_branch=str(metadata.get("execution_branch") or UNKNOWN),
        provider=str(
            metadata.get("provider") or getattr(usage, "provider", "") or UNKNOWN
        ),
        api_surface=str(
            metadata.get("api_surface") or getattr(usage, "api_surface", "") or UNKNOWN
        ),
        request_mode=_known_request_mode(metadata.get("request_mode")),
        cache_reporting=str(
            metadata.get("cache_reporting")
            or getattr(usage, "cache_reporting", "")
            or UNKNOWN
        ),
        turn_index=turn_index,
    )


def create_usage_aware_llm_wrapper(
    llm_client: Any,
    collector: UsageCollector,
    source: str = "llm_call",
) -> Any:
    """Create a wrapper around an LLMClient that automatically collects usage.
    
    This is a convenience wrapper that intercepts calls to *_with_usage methods
    and automatically adds usage to the collector.
    
    Note: This returns a wrapper object, not the original client. The wrapper
    forwards all attribute access to the original client but intercepts
    usage-returning methods.
    
    Args:
        llm_client: The LLMClient to wrap
        collector: UsageCollector to add usage to
        source: Source identifier for usage entries
        
    Returns:
        Wrapped client that auto-collects usage
    """
    class UsageAwareLLMWrapper:
        """Wrapper that auto-collects usage from LLM calls."""
        
        def __init__(self, client: Any, collector: UsageCollector, source: str):
            self._client = client
            self._collector = collector
            self._source = source
        
        def __getattr__(self, name: str) -> Any:
            return getattr(self._client, name)
        
        async def chat_messages_with_usage(self, *args: Any, **kwargs: Any) -> Any:
            """Forward to client and collect usage."""
            response = await self._client.chat_messages_with_usage(*args, **kwargs)
            if hasattr(response, "usage"):
                self._collector.add(
                    response.usage,
                    source=self._source,
                    metadata={"request_mode": "non_streaming"},
                )
            return response
        
    if hasattr(llm_client, "stream_chat_messages_with_usage"):
        async def stream_chat_messages_with_usage(
            self: UsageAwareLLMWrapper,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Forward to client and collect usage from get_final_usage()."""
            response = await self._client.stream_chat_messages_with_usage(*args, **kwargs)

            original_get_usage = response.get_final_usage

            def get_usage_and_collect() -> Optional["UsageData"]:
                usage = original_get_usage()
                if usage:
                    self._collector.add(
                        usage,
                        source=self._source,
                        metadata={"request_mode": "streaming"},
                    )
                return usage

            response.get_final_usage = get_usage_and_collect
            return response

        UsageAwareLLMWrapper.stream_chat_messages_with_usage = (  # type: ignore[attr-defined]
            stream_chat_messages_with_usage
        )

    return UsageAwareLLMWrapper(llm_client, collector, source)


__all__ = [
    "UsageCollector",
    "record_turn_usage",
    "record_usage_list_best_effort",
    "create_usage_aware_llm_wrapper",
]
