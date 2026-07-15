"""Tests for startup-owned LangGraph checkpointer schema initialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_postgres_bootstrap_serializes_setup_with_advisory_lock() -> None:
    """Concurrent backend starts must serialize idempotent schema setup."""
    from backend.services.langgraph_chat.checkpoint.schema_bootstrap import (
        initialize_checkpointer_schema,
    )

    cursor = AsyncMock()
    cursor.fetchone.return_value = {"locked": True}
    cursor_context = AsyncMock()
    cursor_context.__aenter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    checkpointer = MagicMock()
    checkpointer.conn = connection
    checkpointer.setup = AsyncMock()
    saver_context = AsyncMock()
    saver_context.__aenter__.return_value = checkpointer

    with patch.dict(
        "os.environ",
        {"DATABASE_URL": "postgresql://test:test@postgres/test"},
    ), patch(
        "backend.services.langgraph_chat.checkpoint.schema_bootstrap.get_checkpointer_connection_string",
        return_value="postgresql://test:test@postgres/test",
    ), patch(
        "backend.services.langgraph_chat.checkpoint.schema_bootstrap.AsyncPostgresSaver.from_conn_string",
        return_value=saver_context,
    ):
        await initialize_checkpointer_schema()

    statements = [str(call.args[0]) for call in cursor.execute.await_args_list]
    assert any("pg_advisory_lock" in statement for statement in statements)
    assert any("pg_advisory_unlock" in statement for statement in statements)
    checkpointer.setup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_non_postgres_bootstrap_is_explicitly_skipped() -> None:
    """SQLite development databases do not run PostgreSQL checkpointer DDL."""
    from backend.services.langgraph_chat.checkpoint.schema_bootstrap import (
        initialize_checkpointer_schema,
    )

    with patch.dict("os.environ", {"DATABASE_URL": "sqlite:///test.db"}), patch(
        "backend.services.langgraph_chat.checkpoint.schema_bootstrap.AsyncPostgresSaver"
    ) as saver:
        initialized = await initialize_checkpointer_schema()

    assert initialized is False
    saver.from_conn_string.assert_not_called()


def test_request_runtime_modules_do_not_own_checkpointer_schema_setup() -> None:
    """Only startup bootstrap may invoke LangGraph checkpointer schema DDL."""
    request_runtime_paths = (
        "services/langgraph_chat/runtime/warmup_service.py",
        "services/langgraph_chat/handlers/normal_chat_handler.py",
        "services/langgraph_chat/handlers/simple_tool_handler.py",
        "services/langgraph_chat/handlers/deep_reasoning_handler.py",
        "services/langgraph_chat/handlers/turn_runtime.py",
    )

    for relative_path in request_runtime_paths:
        source = (_BACKEND_ROOT / relative_path).read_text(encoding="utf-8")
        assert ".setup(" not in source, relative_path
