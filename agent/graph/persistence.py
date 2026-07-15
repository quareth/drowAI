"""Checkpointer utilities for LangGraph integration with persistent storage.

This module provides a hierarchy of checkpoint storage backends:
1. PostgreSQL (preferred) - Centralized, queryable, survives restarts
2. SQLite (fallback) - Persistent, isolated per-task
3. Memory (last resort) - In-memory only, lost on restart

The checkpointer choice is made per-task and handles failures gracefully.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Try importing checkpoint implementations
try:
    from langgraph.checkpoint.memory import MemorySaver
    _MEMORY_AVAILABLE = True
except ImportError:
    MemorySaver = None  # type: ignore[assignment]
    _MEMORY_AVAILABLE = False

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


_DEFAULT_CHECKPOINTER: Optional["MemorySaver"] = None


def get_default_checkpointer() -> "MemorySaver":
    """Return a shared in-memory checkpointer for LangGraph runs.
    
    .. deprecated:: 1.0
        Use `get_persistent_checkpointer(task_id)` instead for persistent storage.
        This function will be removed in v2.0.
    
    Returns:
        MemorySaver instance (in-memory only, lost on restart)
        
    Raises:
        RuntimeError: If MemorySaver is not available
    """
    global _DEFAULT_CHECKPOINTER
    
    if not _MEMORY_AVAILABLE:
        raise RuntimeError(
            "LangGraph MemorySaver unavailable; did you install langgraph>=0.4.8?"
        )
    
    if _DEFAULT_CHECKPOINTER is None:
        _DEFAULT_CHECKPOINTER = MemorySaver()
        logger.warning(
            "[CHECKPOINT] Using deprecated get_default_checkpointer(). "
            "Use get_persistent_checkpointer(task_id) for persistent storage."
        )
    
    return _DEFAULT_CHECKPOINTER


def get_checkpointer_connection_string() -> str:
    """Get PostgreSQL connection string for checkpointer.
    
    This is a simple helper that returns the connection string.
    The caller is responsible for creating and managing the context manager.
    
    Returns:
        PostgreSQL connection string in asyncpg format
    
    Raises:
        RuntimeError: If DATABASE_URL not set or PostgreSQL unavailable
    """
    if not _ASYNC_POSTGRES_AVAILABLE:
        raise RuntimeError(
            "AsyncPostgresSaver not available. "
            "Install langgraph-checkpoint-postgres for persistent checkpointing."
        )
    
    return _get_postgres_connection_string()


def get_sqlite_checkpoint_path(task_id: int) -> Path:
    """Get SQLite checkpoint file path for a task.
    
    Public wrapper for _get_sqlite_checkpoint_path.
    
    Args:
        task_id: Task ID for path resolution
        
    Returns:
        Path to checkpoint database file
    """
    return _get_sqlite_checkpoint_path(task_id)


def _get_postgres_connection_string() -> str:
    """Return PostgreSQL connection string for LangGraph.
    
    LangGraph checkpoint library requires asyncpg format:
    postgresql://user:pass@host:port/db
    
    SQLAlchemy typically uses:
    postgresql+psycopg2://user:pass@host:port/db
    
    This function converts between formats if needed.
    
    Returns:
        Connection string in asyncpg format
        
    Raises:
        RuntimeError: If DATABASE_URL not set or invalid
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL environment variable not set. "
            "Required for PostgreSQL checkpointing."
        )
    
    # Convert SQLAlchemy format to asyncpg format
    # Remove the +psycopg2 or +asyncpg suffix if present
    if "+psycopg2" in db_url:
        db_url = db_url.replace("+psycopg2", "")
        logger.debug("[CHECKPOINT] Converted psycopg2 DSN to asyncpg format")
    elif "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "")
    
    # Validate basic format
    if not db_url.startswith("postgresql://"):
        raise RuntimeError(
            f"Invalid DATABASE_URL format: {db_url[:30]}... "
            "Expected: postgresql://user:pass@host:port/db"
        )
    
    logger.debug(f"[CHECKPOINT] Using PostgreSQL connection: {db_url[:30]}...")
    
    return db_url


def _get_sqlite_checkpoint_path(task_id: int) -> Path:
    """Get SQLite checkpoint file path for a task.
    
    Uses workspace structure: workspace/<task_id>/checkpoints.db
    
    Args:
        task_id: Task ID for path resolution
        
    Returns:
        Path to checkpoint database file
    """
    # Try to import workspace config (may not be available in all contexts)
    try:
        from backend.config.workspace import WorkspaceConfig
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
    except Exception:
        # Fallback to simple workspace structure
        workspace_root = os.getenv("WORKSPACE_ROOT", "workspace")
        workspace_path = Path(workspace_root) / str(task_id)
    
    return workspace_path / "checkpoints.db"


__all__ = [
    "get_default_checkpointer",
    "get_checkpointer_connection_string",
    "get_sqlite_checkpoint_path",
]
