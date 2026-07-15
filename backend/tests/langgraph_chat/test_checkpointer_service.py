"""Tests for CheckpointerService."""

import os

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest

from backend.services.langgraph_chat.checkpoint.checkpointer_service import CheckpointerService


class TestCheckpointerServicePostgreSQL:
    """Test PostgreSQL checkpointer integration."""
    
    @pytest.mark.asyncio
    async def test_checkpointer_service_postgres_success(self):
        """Test successful PostgreSQL checkpointer creation."""
        service = CheckpointerService()
        
        # Mock PostgreSQL to be available and succeed
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', True):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string') as mock_conn_str:
                mock_conn_str.return_value = "postgresql://test:test@localhost/test"
                
                mock_checkpointer = AsyncMock()
                mock_checkpointer.__class__.__name__ = "AsyncPostgresSaver"
                
                @asynccontextmanager
                async def mock_context_manager():
                    yield mock_checkpointer
                
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver') as mock_saver_class:
                    mock_saver_class.from_conn_string.return_value = mock_context_manager()
                    
                    # Test that we can use it as a context manager
                    async with service.get_checkpointer(task_id=1) as checkpointer:
                        assert checkpointer is mock_checkpointer
                        assert checkpointer.__class__.__name__ == "AsyncPostgresSaver"
    
    @pytest.mark.asyncio
    async def test_checkpointer_service_lifecycle_context_manager(self):
        """Test that checkpointer follows context manager lifecycle when pooling disabled."""
        service = CheckpointerService(pool_max_size=0)
        
        entry_called = False
        exit_called = False
        
        mock_checkpointer = AsyncMock()
        
        @asynccontextmanager
        async def mock_context_with_tracking():
            nonlocal entry_called, exit_called
            entry_called = True
            try:
                yield mock_checkpointer
            finally:
                exit_called = True
        
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', True):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string') as mock_conn_str:
                mock_conn_str.return_value = "postgresql://test:test@localhost/test"
                
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver') as mock_saver_class:
                    mock_saver_class.from_conn_string.return_value = mock_context_with_tracking()
                    
                    async with service.get_checkpointer(task_id=1) as checkpointer:
                        assert entry_called
                        assert not exit_called
                        assert checkpointer is mock_checkpointer
                    
                    # After exiting context, exit should be called
                    assert exit_called


class TestCheckpointerServiceSQLite:
    """Test SQLite checkpointer fallback."""
    
    @pytest.mark.asyncio
    async def test_checkpointer_service_postgres_fallback_to_sqlite(self):
        """Test that service falls back to SQLite when PostgreSQL fails."""
        service = CheckpointerService()
        
        # Mock PostgreSQL to fail
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', True):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE', True):
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string') as mock_conn_str:
                    mock_conn_str.side_effect = RuntimeError("PostgreSQL unavailable")
                    
                    # Mock SQLite to succeed
                    mock_sqlite_checkpointer = AsyncMock()
                    mock_sqlite_checkpointer.__class__.__name__ = "AsyncSqliteSaver"
                    
                    @asynccontextmanager
                    async def mock_sqlite_context():
                        yield mock_sqlite_checkpointer
                    
                    with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.get_sqlite_checkpoint_path') as mock_path:
                        mock_path.return_value = Path("/tmp/test.db")
                        
                        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncSqliteSaver') as mock_sqlite_class:
                            mock_sqlite_class.from_conn_string.return_value = mock_sqlite_context()
                            
                            async with service.get_checkpointer(task_id=1) as checkpointer:
                                # Should get SQLite checkpointer
                                assert checkpointer is mock_sqlite_checkpointer
                                assert checkpointer.__class__.__name__ == "AsyncSqliteSaver"
    
    @pytest.mark.asyncio
    async def test_sqlite_checkpointer_creates_parent_dirs(self):
        """Test that SQLite checkpointer creates parent directories."""
        service = CheckpointerService()
        
        # Mock PostgreSQL unavailable, SQLite available
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', False):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE', True):
                mock_sqlite_checkpointer = AsyncMock()
                
                @asynccontextmanager
                async def mock_sqlite_context():
                    yield mock_sqlite_checkpointer
                
                mock_path = MagicMock(spec=Path)
                mock_parent = MagicMock()
                mock_path.parent = mock_parent
                
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.get_sqlite_checkpoint_path') as mock_get_path:
                    mock_get_path.return_value = mock_path
                    
                    with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncSqliteSaver') as mock_sqlite_class:
                        mock_sqlite_class.from_conn_string.return_value = mock_sqlite_context()
                        
                        async with service.get_checkpointer(task_id=1) as checkpointer:
                            # Verify parent.mkdir was called
                            mock_parent.mkdir.assert_called_once_with(parents=True, exist_ok=True)
                            assert checkpointer is mock_sqlite_checkpointer


class TestCheckpointerServiceMemory:
    """Test Memory checkpointer fallback."""
    
    @pytest.mark.asyncio
    async def test_checkpointer_service_sqlite_fallback_to_memory(self):
        """Test that service falls back to Memory when SQLite fails."""
        service = CheckpointerService()
        
        # Mock both PostgreSQL and SQLite to fail
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', False):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE', False):
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._MEMORY_AVAILABLE', True):
                    with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.MemorySaver') as mock_memory_class:
                        mock_memory_instance = MagicMock()
                        mock_memory_class.return_value = mock_memory_instance
                        
                        # Should fall back to MemorySaver
                        async with service.get_checkpointer(task_id=1) as checkpointer:
                            assert checkpointer is mock_memory_instance
                            mock_memory_class.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_memory_checkpointer_logs_warning(self, caplog):
        """Test that using Memory checkpointer logs a warning."""
        service = CheckpointerService()
        
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', False):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE', False):
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._MEMORY_AVAILABLE', True):
                    with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service.MemorySaver'):
                        async with service.get_checkpointer(task_id=1):
                            # Verify warning was logged
                            assert any("in-memory checkpointer" in record.message.lower() 
                                      for record in caplog.records)


class TestCheckpointerServiceErrors:
    """Test error handling."""
    
    @pytest.mark.asyncio
    async def test_checkpointer_service_no_implementation_raises_error(self):
        """Test that service raises RuntimeError when no checkpointer available."""
        service = CheckpointerService()
        
        # Mock all checkpointers unavailable
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE', False):
            with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_SQLITE_AVAILABLE', False):
                with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._MEMORY_AVAILABLE', False):
                    with pytest.raises(RuntimeError, match="No checkpointer implementation available"):
                        async with service.get_checkpointer(task_id=1):
                            pass
    
    @pytest.mark.asyncio
    async def test_memory_checkpointer_unavailable_raises_error(self):
        """Test that memory checkpointer context raises error when MemorySaver unavailable."""
        service = CheckpointerService()
        
        with patch('backend.services.langgraph_chat.checkpoint.checkpointer_service._MEMORY_AVAILABLE', False):
            with pytest.raises(RuntimeError, match="MemorySaver not available"):
                async with service._memory_checkpointer_context():
                    pass


class TestCheckpointerServicePooling:
    """Test checkpointer reuse (Task 4.1)."""

    @pytest.mark.asyncio
    async def test_repeated_same_task_reuses_checkpointer(self):
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
            assert cp1 is shared_cp

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
    async def test_invalidate_task_closes_cached_entry_and_forces_recreate(self):
        """Hard-delete cleanup must not leave a reusable per-task checkpointer."""
        service = CheckpointerService(pool_max_size=64)
        create_count = 0
        exit_count = 0

        def _new_context():
            @asynccontextmanager
            async def mock_pg_context():
                nonlocal create_count, exit_count
                create_count += 1
                try:
                    yield AsyncMock()
                finally:
                    exit_count += 1

            return mock_pg_context()

        with patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service._ASYNC_POSTGRES_AVAILABLE",
            True,
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.get_checkpointer_connection_string",
            return_value="postgresql://test:test@localhost/test",
        ), patch(
            "backend.services.langgraph_chat.checkpoint.checkpointer_service.AsyncPostgresSaver"
        ) as mock_saver:
            mock_saver.from_conn_string.side_effect = lambda *_args, **_kwargs: _new_context()

            async with service.get_checkpointer(task_id=42):
                pass
            assert create_count == 1
            assert exit_count == 0

            await service.invalidate_task(42)
            assert exit_count == 1

            async with service.get_checkpointer(task_id=42):
                pass
            assert create_count == 2


class TestCheckpointerServiceIntegration:
    """Integration tests with facade."""
    
    @pytest.mark.asyncio
    async def test_service_can_be_injected_into_facade(self):
        """Test that CheckpointerService can be injected into facade."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade
        
        service = CheckpointerService()
        facade = LangGraphChatFacade(checkpointer_service=service)
        
        assert facade._checkpointer_service is service
    
    @pytest.mark.asyncio
    async def test_facade_uses_default_service_when_none_provided(self):
        """Test that facade creates default CheckpointerService when none provided."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade
        
        facade = LangGraphChatFacade()
        
        assert isinstance(facade._checkpointer_service, CheckpointerService)

    @pytest.mark.asyncio
    async def test_facades_share_default_checkpointer_service_instance(self):
        """Default facades should share one process-level checkpointer service."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade

        first = LangGraphChatFacade()
        second = LangGraphChatFacade()

        assert first._checkpointer_service is second._checkpointer_service
    
    @pytest.mark.asyncio
    async def test_facade_uses_checkpointer_service(self):
        """Test that facade uses injected checkpointer service for get_checkpointer."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade

        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service = CheckpointerService(pool_max_size=0)
        service.get_checkpointer = mock_get_checkpointer

        facade = LangGraphChatFacade(checkpointer_service=service)

        async with facade._checkpointer_service.get_checkpointer(task_id=1) as checkpointer:
            assert checkpointer is mock_checkpointer


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
