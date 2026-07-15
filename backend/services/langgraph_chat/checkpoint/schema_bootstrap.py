"""Initialize the PostgreSQL LangGraph checkpointer schema at app startup.

This module is the sole owner of checkpointer schema DDL. Runtime requests may
acquire checkpointers, but must never run migrations or create indexes.
"""

from __future__ import annotations

import logging
import os

from agent.graph.persistence import get_checkpointer_connection_string

try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
except ImportError:  # pragma: no cover - validated explicitly for PostgreSQL
    AsyncPostgresSaver = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

# Stable, application-specific PostgreSQL advisory-lock key. Session advisory
# locks work with the autocommit connection required by CREATE INDEX CONCURRENTLY.
_CHECKPOINTER_SCHEMA_LOCK_KEY = 0x44524F5741494350


async def initialize_checkpointer_schema() -> bool:
    """Run idempotent checkpointer setup before the application serves traffic.

    PostgreSQL advisory locking serializes startup across backend replicas. The
    lock and LangGraph DDL use the same autocommit connection, so concurrent
    replicas cannot enter ``AsyncPostgresSaver.setup()`` together.

    Returns ``False`` only for non-PostgreSQL development configurations, where
    PostgreSQL checkpointer persistence is not active.
    """
    database_url = str(os.getenv("DATABASE_URL") or "").strip().lower()
    if not database_url.startswith(("postgresql://", "postgresql+", "postgres://")):
        logger.info(
            "Skipping PostgreSQL checkpointer schema bootstrap for non-PostgreSQL database."
        )
        return False
    if AsyncPostgresSaver is None:
        raise RuntimeError(
            "PostgreSQL checkpointer schema bootstrap requires "
            "langgraph-checkpoint-postgres."
        )

    connection_string = get_checkpointer_connection_string()
    async with AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        async with checkpointer.conn.cursor() as cursor:
            await cursor.execute(
                "SELECT pg_advisory_lock(%s)",
                (_CHECKPOINTER_SCHEMA_LOCK_KEY,),
            )
        try:
            await checkpointer.setup()
        finally:
            async with checkpointer.conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_CHECKPOINTER_SCHEMA_LOCK_KEY,),
                )

    logger.info("LangGraph checkpointer schema is ready.")
    return True


__all__ = ["initialize_checkpointer_schema"]
