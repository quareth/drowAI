"""
LangGraph checkpointer lifecycle management and fallback orchestration.

Responsibilities:
- Provide async context manager for checkpointer instances
- Handle PostgreSQL → SQLite → Memory fallback chain
- Per-task checkpointer reuse to avoid open/close churn on sequential
  interrupt operations (e.g. resume, interrupt, resume)
- LRU eviction with safe cleanup on error paths

Out of scope:
- Graph compilation (handled by handlers)
- Thread configuration (handled by facade/handlers)
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, Optional, Union

from backend.services.langgraph_chat.diagnostic_logger import log_checkpointer_operation

logger = logging.getLogger("backend.services.langgraph_chat.checkpointer_service")

# Default max cached checkpointers; 0 disables pooling (always close on exit)
_DEFAULT_POOL_MAX_SIZE = 64


@dataclass
class _CachedCheckpointerEntry:
    """Cached checkpointer entry with borrower tracking for safe concurrent reuse."""

    checkpointer: Any
    context_manager: Any
    last_used: float
    active_borrows: int = 0
    invalidated: bool = False


# Import checkpointer implementations with availability checks
try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    _ASYNC_POSTGRES_AVAILABLE = True
except ImportError:
    AsyncPostgresSaver = None  # type: ignore[assignment]
    _ASYNC_POSTGRES_AVAILABLE = False

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    _ASYNC_SQLITE_AVAILABLE = True
except ImportError:
    AsyncSqliteSaver = None  # type: ignore[assignment]
    _ASYNC_SQLITE_AVAILABLE = False

try:
    from langgraph.checkpoint.memory import MemorySaver

    _MEMORY_AVAILABLE = True
except ImportError:
    MemorySaver = None  # type: ignore[assignment]
    _MEMORY_AVAILABLE = False

try:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    _JSON_PLUS_AVAILABLE = True
except ImportError:
    JsonPlusSerializer = None  # type: ignore[assignment]
    _JSON_PLUS_AVAILABLE = False

from agent.graph.persistence import (  # noqa: E402
    get_checkpointer_connection_string,
    get_sqlite_checkpoint_path,
)

# Custom graph-state types that appear in checkpoints and must be explicitly
# allowlisted so the msgpack deserializer doesn't emit warnings (langgraph-
# checkpoint >=4.0).  Each entry is (module_path, class_name).
_CHECKPOINT_ALLOWED_MODULES: list[tuple[str, str]] = [
    ("agent.graph.state", "TodoStatus"),
    ("agent.graph.state", "CompletionType"),
]


def _build_serde():
    """Build a JsonPlusSerializer with project-specific types allowlisted.

    Returns None when JsonPlusSerializer is unavailable (older langgraph),
    letting checkpointers fall back to their default serializer.
    """
    if not _JSON_PLUS_AVAILABLE:
        return None
    try:
        return JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_ALLOWED_MODULES)
    except TypeError:
        # Older/newer langgraph-checkpoint variants can expose JsonPlusSerializer
        # without the allowlist argument. Fall back to default construction.
        return JsonPlusSerializer()


def _wrap_checkpointer_with_logging(checkpointer, task_id: int):
    """Wrap checkpointer to log all aput/aget calls."""
    original_aput = checkpointer.aput
    original_aget_tuple = checkpointer.aget_tuple

    async def logged_aput(*args, **kwargs):
        log_checkpointer_operation(
            task_id, "aput_start", True, f"args_count={len(args)}"
        )
        result = await original_aput(*args, **kwargs)
        log_checkpointer_operation(task_id, "aput_complete", True)
        return result

    async def logged_aget_tuple(*args, **kwargs):
        log_checkpointer_operation(task_id, "aget_tuple_start", True)
        result = await original_aget_tuple(*args, **kwargs)
        log_checkpointer_operation(
            task_id, "aget_tuple_complete", True, f"has_result={result is not None}"
        )
        return result

    checkpointer.aput = logged_aput
    checkpointer.aget_tuple = logged_aget_tuple
    return checkpointer


class CheckpointerService:
    """Manages LangGraph checkpointer lifecycle with per-task reuse."""

    def __init__(self, pool_max_size: int = _DEFAULT_POOL_MAX_SIZE) -> None:
        """Initialize service.

        Args:
            pool_max_size: Max cached checkpointers per task. 0 disables pooling
                (always close on exit). Default 64.
        """
        self._pool_max_size = pool_max_size
        self._cache: Dict[int, _CachedCheckpointerEntry] = {}
        self._cache_lock = asyncio.Lock()

    async def _evict_lru(self) -> None:
        """Evict least recently used entry if at capacity. Caller must hold lock."""
        if self._pool_max_size <= 0 or len(self._cache) < self._pool_max_size:
            return
        evictable = [
            (task_id, entry)
            for task_id, entry in self._cache.items()
            if entry.active_borrows == 0
        ]
        if not evictable:
            # All cached entries are currently borrowed; allow temporary overflow
            # rather than force-closing an in-use checkpointer.
            return
        oldest_task, oldest_entry = min(evictable, key=lambda item: item[1].last_used)
        self._cache.pop(oldest_task, None)
        await self._close_cached_entry(oldest_task, oldest_entry)
        logger.debug(f"[CHECKPOINT] Evicted task {oldest_task} from pool (LRU)")

    async def _close_cached_entry(
        self, task_id: int, entry: _CachedCheckpointerEntry
    ) -> None:
        """Safely close a cached entry. Caller must hold lock."""
        if entry.context_manager is not None:
            try:
                await entry.context_manager.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning(
                    f"[CHECKPOINT] Error closing checkpointer for task {task_id}: {exc}"
                )

    async def invalidate_task(self, task_id: int) -> None:
        """Close and remove any pooled checkpointer for a task id."""
        entry_to_close: Optional[_CachedCheckpointerEntry] = None
        async with self._cache_lock:
            entry = self._cache.get(int(task_id))
            if entry is not None:
                entry.invalidated = True
                if entry.active_borrows == 0:
                    entry_to_close = self._cache.pop(int(task_id), None)
        if entry_to_close is not None:
            await self._close_cached_entry(int(task_id), entry_to_close)

    @asynccontextmanager
    async def get_checkpointer(
        self,
        task_id: int,
    ) -> AsyncGenerator[
        Union["AsyncPostgresSaver", "AsyncSqliteSaver", "MemorySaver"], None
    ]:
        """Get checkpointer context manager for the task.

        Reuses cached checkpointer for same task when pool_max_size > 0,
        avoiding full reconnect churn on sequential interrupt operations.

        Tries backends in order of preference:
        1. PostgreSQL (preferred, shared database)
        2. SQLite (fallback, per-task file)
        3. MemorySaver (last resort, state lost on restart)

        Usage:
            async with service.get_checkpointer(task_id) as checkpointer:
                compiled = build_graph(checkpointer=checkpointer)
                # ... use compiled graph ...
                # Connection returned to pool on exit (or closed if pool disabled)

        Args:
            task_id: Task ID for checkpoint isolation

        Yields:
            Checkpointer instance (connection opened or reused)
        """
        # Memory path: no pooling (cheap to create)
        if not _ASYNC_POSTGRES_AVAILABLE and not _ASYNC_SQLITE_AVAILABLE:
            if _MEMORY_AVAILABLE:
                logger.warning(
                    f"[CHECKPOINT] Using in-memory checkpointer for task {task_id}. "
                    "State will be LOST on restart! "
                    "Install langgraph-checkpoint-postgres or langgraph-checkpoint-sqlite "
                    "for persistent checkpointing."
                )
                async with self._memory_checkpointer_context() as checkpointer:
                    yield checkpointer
                return
            raise RuntimeError(
                "No checkpointer implementation available! "
                "Install langgraph>=0.4.8 for basic MemorySaver, or "
                "langgraph-checkpoint-postgres/sqlite for persistent storage."
            )

        # Check cache first (when pooling enabled).
        # Important: never hold _cache_lock across caller execution (yield), otherwise
        # cache-hit paths serialize unrelated tasks and can deadlock on nested cleanup.
        if self._pool_max_size > 0:
            borrowed_entry: Optional[_CachedCheckpointerEntry] = None
            async with self._cache_lock:
                candidate = self._cache.get(task_id)
                if candidate is not None and not candidate.invalidated:
                    candidate.active_borrows += 1
                    candidate.last_used = time.monotonic()
                    borrowed_entry = candidate

            if borrowed_entry is not None:
                log_checkpointer_operation(task_id, "pool_hit", True)
                try:
                    yield borrowed_entry.checkpointer
                except Exception:
                    entry_to_close: Optional[_CachedCheckpointerEntry] = None
                    async with self._cache_lock:
                        current = self._cache.get(task_id)
                        if current is borrowed_entry:
                            current.active_borrows = max(0, current.active_borrows - 1)
                            current.invalidated = True
                            current.last_used = time.monotonic()
                            if current.active_borrows == 0:
                                entry_to_close = self._cache.pop(task_id, None)
                    if entry_to_close is not None:
                        await self._close_cached_entry(task_id, entry_to_close)
                    raise
                else:
                    entry_to_close: Optional[_CachedCheckpointerEntry] = None
                    async with self._cache_lock:
                        current = self._cache.get(task_id)
                        if current is borrowed_entry:
                            current.active_borrows = max(0, current.active_borrows - 1)
                            current.last_used = time.monotonic()
                            if current.invalidated and current.active_borrows == 0:
                                entry_to_close = self._cache.pop(task_id, None)
                    if entry_to_close is not None:
                        await self._close_cached_entry(task_id, entry_to_close)
                return

        # Create new checkpointer
        checkpointer_acquired = False
        checkpointer_ctx: Optional[Any] = None
        checkpointer: Optional[Any] = None

        serde = _build_serde()

        if _ASYNC_POSTGRES_AVAILABLE:
            try:
                conn_string = get_checkpointer_connection_string()
                kwargs = {"serde": serde} if serde else {}
                try:
                    checkpointer_ctx = AsyncPostgresSaver.from_conn_string(
                        conn_string, **kwargs
                    )
                except TypeError:
                    checkpointer_ctx = AsyncPostgresSaver.from_conn_string(conn_string)
                checkpointer = await checkpointer_ctx.__aenter__()
                checkpointer = _wrap_checkpointer_with_logging(checkpointer, task_id)
                checkpointer_acquired = True
            except Exception as exc:
                logger.error(
                    f"[CHECKPOINT] PostgreSQL connection FAILED for task {task_id}: {exc}, falling back",
                    exc_info=True,
                )

        if _ASYNC_SQLITE_AVAILABLE and not checkpointer_acquired:
            try:
                checkpoint_path = get_sqlite_checkpoint_path(task_id)
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                kwargs = {"serde": serde} if serde else {}
                sqlite_conn = f"file:{checkpoint_path}"
                try:
                    checkpointer_ctx = AsyncSqliteSaver.from_conn_string(
                        sqlite_conn, **kwargs
                    )
                except TypeError:
                    checkpointer_ctx = AsyncSqliteSaver.from_conn_string(sqlite_conn)
                checkpointer = await checkpointer_ctx.__aenter__()
                checkpointer_acquired = True
                log_checkpointer_operation(task_id, "sqlite_connect", True)
            except Exception as exc:
                logger.error(
                    f"[CHECKPOINT] SQLite connection FAILED for task {task_id}: {exc}, falling back",
                    exc_info=True,
                )

        if not checkpointer_acquired:
            if _MEMORY_AVAILABLE:
                logger.warning(
                    f"[CHECKPOINT] Using in-memory checkpointer for task {task_id}. "
                    "State will be LOST on restart!"
                )
                async with self._memory_checkpointer_context() as cp:
                    yield cp
                return
            raise RuntimeError(
                "No checkpointer implementation available! "
                "Install langgraph>=0.4.8 for basic MemorySaver, or "
                "langgraph-checkpoint-postgres/sqlite for persistent storage."
            )

        # Yield and handle cleanup
        exc_type, exc_value, exc_tb = None, None, None
        try:
            log_checkpointer_operation(task_id, "yield_to_handler", True)
            yield checkpointer
            log_checkpointer_operation(task_id, "handler_complete", True)
        except Exception:
            import sys

            exc_type, exc_value, exc_tb = sys.exc_info()
            logger.warning(
                f"[CHECKPOINT] Handler raised for task {task_id}, closing connection"
            )
            raise
        finally:
            if exc_type is not None or self._pool_max_size <= 0:
                # On error or pooling disabled: close connection (no leak)
                if checkpointer_ctx is not None:
                    await checkpointer_ctx.__aexit__(exc_type, exc_value, exc_tb)
            else:
                # Success with pooling: return to cache
                async with self._cache_lock:
                    await self._evict_lru()
                    self._cache[task_id] = _CachedCheckpointerEntry(
                        checkpointer=checkpointer,
                        context_manager=checkpointer_ctx,
                        last_used=time.monotonic(),
                    )
                    log_checkpointer_operation(task_id, "pool_store", True)

    @staticmethod
    @asynccontextmanager
    async def _memory_checkpointer_context() -> AsyncGenerator["MemorySaver", None]:
        """Wrap MemorySaver in async context manager for consistent interface.

        MemorySaver doesn't natively support async with, so we wrap it
        to provide a consistent interface with PostgreSQL/SQLite checkpointers.

        Yields:
            MemorySaver instance
        """
        if not _MEMORY_AVAILABLE:
            raise RuntimeError("MemorySaver not available. Install langgraph>=0.4.8")

        checkpointer = MemorySaver()
        logger.debug("[CHECKPOINT] MemorySaver instance created")
        try:
            yield checkpointer
        finally:
            logger.debug("[CHECKPOINT] MemorySaver context exited")


_SHARED_CHECKPOINTER_SERVICE: Optional[CheckpointerService] = None


def get_shared_checkpointer_service() -> CheckpointerService:
    """Return process-level shared CheckpointerService for facade defaults."""
    global _SHARED_CHECKPOINTER_SERVICE
    if _SHARED_CHECKPOINTER_SERVICE is None:
        _SHARED_CHECKPOINTER_SERVICE = CheckpointerService()
    return _SHARED_CHECKPOINTER_SERVICE


__all__ = ["CheckpointerService", "get_shared_checkpointer_service"]
