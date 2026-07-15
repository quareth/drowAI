"""Optional in-memory cache for usage summaries.

This module provides a simple TTL-based cache for task usage summaries
to reduce database load from frequent polling. The cache is designed to
be lightweight and non-critical - cache misses simply query the database.

Key features:
- Thread-safe via threading.Lock
- Automatic TTL expiration
- Cache invalidation on new records
- Zero dependencies beyond stdlib

Usage:
    cache = UsageCache(ttl_seconds=30)
    
    # Check cache before DB query
    summary = cache.get(task_id)
    if summary is None:
        summary = service.get_task_usage(task_id)
        cache.set(task_id, summary)
    
    # Invalidate after recording new usage
    service.record_usage(...)
    cache.invalidate(task_id)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TaskUsageSummary

logger = logging.getLogger(__name__)


class UsageCache:
    """Simple TTL cache for usage summaries.
    
    This cache stores task usage summaries with automatic expiration.
    It is designed for high-read, low-write scenarios where the same
    task's usage is queried frequently (e.g., frontend polling).
    
    Thread Safety:
        All operations are thread-safe via a reentrant lock.
    
    Memory:
        Expired entries are lazily removed on access. For long-running
        processes, call cleanup() periodically to remove all expired entries.
    
    Attributes:
        ttl_seconds: Time-to-live for cached entries (default: 30)
    
    Example:
        cache = UsageCache(ttl_seconds=30)
        
        # Try cache first
        summary = cache.get(task_id)
        if summary is None:
            summary = db_service.get_task_usage(task_id)
            cache.set(task_id, summary)
        
        # After recording new usage
        cache.invalidate(task_id)
    """
    
    def __init__(self, ttl_seconds: int = 30) -> None:
        """Initialize cache with specified TTL.
        
        Args:
            ttl_seconds: How long entries remain valid (default: 30 seconds)
        """
        self._cache: Dict[int, Tuple["TaskUsageSummary", float]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        
        logger.debug(f"UsageCache initialized with TTL={ttl_seconds}s")
    
    def get(self, task_id: int) -> Optional["TaskUsageSummary"]:
        """Get cached summary if present and not expired.
        
        Args:
            task_id: Task ID to look up
            
        Returns:
            TaskUsageSummary if cached and valid, None otherwise
        """
        with self._lock:
            entry = self._cache.get(task_id)
            if entry is None:
                return None
            
            summary, timestamp = entry
            if time.time() - timestamp > self._ttl:
                # Entry expired - remove it
                del self._cache[task_id]
                logger.debug(f"Cache EXPIRED for task {task_id}")
                return None
            
            logger.debug(f"Cache HIT for task {task_id}")
            return summary
    
    def set(self, task_id: int, summary: "TaskUsageSummary") -> None:
        """Store summary in cache.
        
        Args:
            task_id: Task ID to cache
            summary: Usage summary to store
        """
        with self._lock:
            self._cache[task_id] = (summary, time.time())
            logger.debug(f"Cache SET for task {task_id}")
    
    def invalidate(self, task_id: int) -> bool:
        """Remove entry from cache.
        
        Call this after recording new usage to ensure fresh data.
        
        Args:
            task_id: Task ID to invalidate
            
        Returns:
            True if entry was present and removed, False otherwise
        """
        with self._lock:
            if task_id in self._cache:
                del self._cache[task_id]
                logger.debug(f"Cache INVALIDATED for task {task_id}")
                return True
            return False
    
    def cleanup(self) -> int:
        """Remove all expired entries.
        
        Call periodically to prevent memory growth in long-running processes.
        
        Returns:
            Number of expired entries removed
        """
        with self._lock:
            now = time.time()
            expired_keys = [
                task_id 
                for task_id, (_, timestamp) in self._cache.items()
                if now - timestamp > self._ttl
            ]
            
            for task_id in expired_keys:
                del self._cache[task_id]
            
            if expired_keys:
                logger.debug(f"Cache CLEANUP removed {len(expired_keys)} expired entries")
            
            return len(expired_keys)
    
    def clear(self) -> int:
        """Remove all entries from cache.
        
        Returns:
            Number of entries removed
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            logger.debug(f"Cache CLEARED {count} entries")
            return count
    
    def size(self) -> int:
        """Get number of cached entries (including expired).
        
        Returns:
            Number of entries in cache
        """
        with self._lock:
            return len(self._cache)
    
    def stats(self) -> Dict[str, int]:
        """Get cache statistics.
        
        Returns:
            Dict with 'total', 'valid', and 'expired' counts
        """
        with self._lock:
            now = time.time()
            total = len(self._cache)
            expired = sum(
                1 for _, (_, timestamp) in self._cache.items()
                if now - timestamp > self._ttl
            )
            return {
                "total": total,
                "valid": total - expired,
                "expired": expired,
                "ttl_seconds": self._ttl,
            }


# Module-level singleton for convenience (optional usage)
_default_cache: Optional[UsageCache] = None


def get_default_cache(ttl_seconds: int = 30) -> UsageCache:
    """Get or create default module-level cache.
    
    This provides a convenient singleton for cases where dependency
    injection is not used. The cache is lazily initialized.
    
    Args:
        ttl_seconds: TTL for the cache (only used on first call)
        
    Returns:
        Module-level UsageCache instance
    """
    global _default_cache
    if _default_cache is None:
        _default_cache = UsageCache(ttl_seconds=ttl_seconds)
    return _default_cache


__all__ = [
    "UsageCache",
    "get_default_cache",
]
