""": Checkpointer lifecycle and reuse tests.

Verifies checkpointer pooling/reuse behavior under repeated calls
and error-path cleanup ( acceptance)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.langgraph_chat.checkpoint.checkpointer_service import CheckpointerService


class TestCheckpointerLifecycle:
    """Phase 4 lifecycle tests: reuse and error-path behavior."""

    @pytest.mark.asyncio
    async def test_checkpointer_reuse_under_repeated_calls(self):
        """Repeated get_checkpointer for same task yields same instance (pool hit)."""
        service = CheckpointerService(pool_max_size=64)
        create_count = 0
        shared_cp = AsyncMock()

        @asynccontextmanager
        async def mock_pg_context():
            nonlocal create_count
            create_count += 1
            yield shared_cp

        with patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE",
            True,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string",
            return_value="postgresql://test:test@localhost/test",
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver"
        ) as mock_saver:
            mock_saver.from_conn_string.return_value = mock_pg_context()

            async with service.get_checkpointer(task_id=42) as cp1:
                pass
            assert create_count == 1

            async with service.get_checkpointer(task_id=42) as cp2:
                pass
            assert create_count == 1, "Second call should reuse (pool hit)"
            assert cp1 is cp2

    @pytest.mark.asyncio
    async def test_error_path_closes_connection_no_leak(self):
        """On handler error, connection is closed (not cached); no leak."""
        service = CheckpointerService(pool_max_size=64)
        exit_called = False
        shared_cp = AsyncMock()

        @asynccontextmanager
        async def mock_pg_context():
            nonlocal exit_called
            try:
                yield shared_cp
            finally:
                exit_called = True

        with patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE",
            True,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string",
            return_value="postgresql://test:test@localhost/test",
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver"
        ) as mock_saver:
            mock_saver.from_conn_string.return_value = mock_pg_context()

            with pytest.raises(ValueError, match="handler failed"):
                async with service.get_checkpointer(task_id=99) as _:
                    raise ValueError("handler failed")

            assert exit_called, "Connection must be closed on error (no leak)"

    @pytest.mark.asyncio
    async def test_cache_hit_error_path_does_not_deadlock_and_recreates_entry(self):
        """Cache-hit handler errors should close cached entry without lock deadlock."""
        service = CheckpointerService(pool_max_size=64)
        create_count = 0
        exit_called = 0
        shared_cp = AsyncMock()

        class _TrackedContext:
            async def __aenter__(self):
                return shared_cp

            async def __aexit__(self, exc_type, exc, tb):
                nonlocal exit_called
                exit_called += 1
                return False

        def _new_context(*_args, **_kwargs):
            nonlocal create_count
            create_count += 1
            return _TrackedContext()

        with patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE",
            True,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE",
            False,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string",
            return_value="postgresql://test:test@localhost/test",
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver"
        ) as mock_saver:
            mock_saver.from_conn_string.side_effect = _new_context

            # Initial acquisition stores entry in pool.
            async with service.get_checkpointer(task_id=77):
                pass
            assert create_count == 1
            assert exit_called == 0

            # Cache-hit acquisition raises in handler. This used to deadlock because
            # lock was held across yield and cleanup re-acquired the same lock.
            async def _cache_hit_failure() -> None:
                async with service.get_checkpointer(task_id=77):
                    raise ValueError("cache-hit failure")

            with pytest.raises(ValueError, match="cache-hit failure"):
                await asyncio.wait_for(_cache_hit_failure(), timeout=1.0)

            # Failed cached entry must be closed and removed; next call recreates it.
            assert exit_called == 1
            async with service.get_checkpointer(task_id=77):
                pass
            assert create_count == 2

    @pytest.mark.asyncio
    async def test_cache_hit_failure_does_not_close_entry_while_other_borrower_active(self):
        """One borrower failure must not close shared cached entry before others release it."""
        service = CheckpointerService(pool_max_size=64)
        create_count = 0
        exit_called = 0
        shared_cp = AsyncMock()

        class _TrackedContext:
            async def __aenter__(self):
                return shared_cp

            async def __aexit__(self, exc_type, exc, tb):
                nonlocal exit_called
                exit_called += 1
                return False

        def _new_context(*_args, **_kwargs):
            nonlocal create_count
            create_count += 1
            return _TrackedContext()

        with patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE",
            True,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE",
            False,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string",
            return_value="postgresql://test:test@localhost/test",
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver"
        ) as mock_saver:
            mock_saver.from_conn_string.side_effect = _new_context

            async with service.get_checkpointer(task_id=88):
                pass
            assert create_count == 1
            assert exit_called == 0

            fail_entered = asyncio.Event()
            ok_entered = asyncio.Event()
            release_fail = asyncio.Event()
            release_ok = asyncio.Event()

            async def _failing_borrower() -> None:
                with pytest.raises(ValueError, match="cache-hit boom"):
                    async with service.get_checkpointer(task_id=88) as cp:
                        assert cp is shared_cp
                        fail_entered.set()
                        await release_fail.wait()
                        raise ValueError("cache-hit boom")

            async def _successful_borrower() -> None:
                async with service.get_checkpointer(task_id=88) as cp:
                    assert cp is shared_cp
                    ok_entered.set()
                    await release_ok.wait()

            fail_task = asyncio.create_task(_failing_borrower())
            ok_task = asyncio.create_task(_successful_borrower())

            await asyncio.wait_for(fail_entered.wait(), timeout=1.0)
            await asyncio.wait_for(ok_entered.wait(), timeout=1.0)

            # Fail one borrower first; cached context must remain open because another
            # borrower is still active.
            release_fail.set()
            await asyncio.wait_for(fail_task, timeout=1.0)
            assert exit_called == 0

            release_ok.set()
            await asyncio.wait_for(ok_task, timeout=1.0)
            assert exit_called == 1

            # Entry should be invalidated and recreated on next borrow.
            async with service.get_checkpointer(task_id=88):
                pass
            assert create_count == 2
