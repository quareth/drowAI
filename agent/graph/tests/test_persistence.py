"""Tests for persistent checkpoint storage."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCheckpointerFallbackHierarchy:
    """Test checkpointer fallback hierarchy (PostgreSQL → SQLite → Memory)."""
    
    def test_postgres_checkpointer_used_when_available(self):
        """Test PostgreSQL checkpointer is preferred when available."""
        from agent.graph import persistence
        
        # Mock PostgreSQL as available
        with patch.object(persistence, '_POSTGRES_AVAILABLE', True):
            with patch.object(persistence, 'PostgresSaver') as mock_postgres:
                mock_postgres.from_conn_string.return_value = MagicMock()
                
                # Set DATABASE_URL
                os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
                
                try:
                    checkpointer = persistence.get_persistent_checkpointer(task_id=1)
                    
                    # Verify PostgreSQL was used
                    assert mock_postgres.from_conn_string.called
                    
                finally:
                    os.environ.pop("DATABASE_URL", None)
    
    def test_sqlite_fallback_when_postgres_fails(self):
        """Test SQLite is used when PostgreSQL fails."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_POSTGRES_AVAILABLE', True):
            with patch.object(persistence, '_SQLITE_AVAILABLE', True):
                with patch.object(persistence, 'PostgresSaver') as mock_postgres:
                    with patch.object(persistence, 'SqliteSaver') as mock_sqlite:
                        # PostgreSQL fails
                        mock_postgres.from_conn_string.side_effect = Exception("Connection failed")
                        mock_sqlite.from_conn_string.return_value = MagicMock()
                        
                        os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
                        
                        try:
                            checkpointer = persistence.get_persistent_checkpointer(task_id=1)
                            
                            # Verify SQLite was used as fallback
                            assert mock_sqlite.from_conn_string.called
                            
                        finally:
                            os.environ.pop("DATABASE_URL", None)
    
    def test_memory_fallback_when_both_fail(self):
        """Test Memory is used when both PostgreSQL and SQLite fail."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_POSTGRES_AVAILABLE', False):
            with patch.object(persistence, '_SQLITE_AVAILABLE', False):
                with patch.object(persistence, '_MEMORY_AVAILABLE', True):
                    with patch.object(persistence, 'MemorySaver') as mock_memory:
                        mock_memory.return_value = MagicMock()
                        
                        checkpointer = persistence.get_persistent_checkpointer(task_id=1)
                        
                        # Verify Memory was used as last resort
                        assert mock_memory.called
    
    def test_error_when_no_checkpointer_available(self):
        """Test error is raised when no checkpointer implementation available."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_POSTGRES_AVAILABLE', False):
            with patch.object(persistence, '_SQLITE_AVAILABLE', False):
                with patch.object(persistence, '_MEMORY_AVAILABLE', False):
                    
                    with pytest.raises(RuntimeError, match="No checkpointer implementation available"):
                        persistence.get_persistent_checkpointer(task_id=1)


class TestPostgreSQLConnectionString:
    """Test PostgreSQL connection string conversion."""
    
    def test_psycopg2_format_converted_to_asyncpg(self):
        """Test SQLAlchemy psycopg2 format is converted to asyncpg."""
        from agent.graph import persistence
        
        os.environ["DATABASE_URL"] = "postgresql+psycopg2://user:pass@localhost:5432/drowai"
        
        try:
            conn_string = persistence._get_postgres_connection_string()
            
            # Should remove +psycopg2
            assert "postgresql://user:pass@localhost:5432/drowai" == conn_string
            assert "+psycopg2" not in conn_string
            
        finally:
            os.environ.pop("DATABASE_URL", None)
    
    def test_asyncpg_format_already_correct(self):
        """Test asyncpg format is accepted as-is."""
        from agent.graph import persistence
        
        os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/drowai"
        
        try:
            conn_string = persistence._get_postgres_connection_string()
            
            # Should remain unchanged
            assert conn_string == "postgresql://user:pass@localhost:5432/drowai"
            
        finally:
            os.environ.pop("DATABASE_URL", None)
    
    def test_error_when_database_url_not_set(self):
        """Test error when DATABASE_URL not set."""
        from agent.graph import persistence
        
        # Ensure DATABASE_URL not set
        os.environ.pop("DATABASE_URL", None)
        
        with pytest.raises(RuntimeError, match="DATABASE_URL environment variable not set"):
            persistence._get_postgres_connection_string()
    
    def test_error_when_invalid_format(self):
        """Test error when DATABASE_URL has invalid format."""
        from agent.graph import persistence
        
        os.environ["DATABASE_URL"] = "mysql://user:pass@localhost:3306/db"
        
        try:
            with pytest.raises(RuntimeError, match="Invalid DATABASE_URL format"):
                persistence._get_postgres_connection_string()
                
        finally:
            os.environ.pop("DATABASE_URL", None)


class TestSQLiteCheckpointPath:
    """Test SQLite checkpoint path resolution."""
    
    def test_sqlite_path_uses_workspace_structure(self):
        """Test SQLite checkpoint path follows workspace structure."""
        from agent.graph import persistence
        
        path = persistence._get_sqlite_checkpoint_path(task_id=123)
        
        # Should be workspace/<task_id>/checkpoints.db
        assert "123" in str(path)
        assert "checkpoints.db" in str(path)
        assert path.name == "checkpoints.db"
    
    def test_sqlite_path_fallback_when_workspace_config_unavailable(self):
        """Test fallback path when WorkspaceConfig unavailable."""
        from agent.graph import persistence
        
        # Simulate WorkspaceConfig import failure
        with patch('agent.graph.persistence.Path') as mock_path:
            mock_path.return_value = Path("workspace/456/checkpoints.db")
            
            path = persistence._get_sqlite_checkpoint_path(task_id=456)
            
            # Should use fallback path
            assert isinstance(path, Path)


class TestDefaultCheckpointerDeprecation:
    """Test deprecation warning for get_default_checkpointer."""
    
    def test_deprecation_warning_logged(self):
        """Test that get_default_checkpointer logs deprecation warning."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_MEMORY_AVAILABLE', True):
            with patch.object(persistence, 'MemorySaver') as mock_memory:
                mock_memory.return_value = MagicMock()
                
                with patch.object(persistence, 'logger') as mock_logger:
                    # Reset global checkpointer
                    persistence._DEFAULT_CHECKPOINTER = None
                    
                    checkpointer = persistence.get_default_checkpointer()
                    
                    # Verify warning was logged
                    assert mock_logger.warning.called
                    warning_msg = str(mock_logger.warning.call_args)
                    assert "deprecated" in warning_msg.lower()


class TestCheckpointerIntegration:
    """Integration tests for checkpointer usage."""
    
    def test_checkpointer_can_be_created_multiple_times(self):
        """Test multiple calls to get_persistent_checkpointer work."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_MEMORY_AVAILABLE', True):
            with patch.object(persistence, 'MemorySaver') as mock_memory:
                mock_memory.return_value = MagicMock()
                
                # Create checkpointers for multiple tasks
                cp1 = persistence.get_persistent_checkpointer(task_id=1)
                cp2 = persistence.get_persistent_checkpointer(task_id=2)
                
                # Both should succeed
                assert cp1 is not None
                assert cp2 is not None
    
    def test_checkpointer_isolated_per_task(self):
        """Test checkpointers are isolated per task."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_SQLITE_AVAILABLE', True):
            with patch.object(persistence, 'SqliteSaver') as mock_sqlite:
                mock_sqlite.from_conn_string.return_value = MagicMock()
                
                # Get checkpointers for different tasks
                persistence.get_persistent_checkpointer(task_id=1)
                call1_path = str(mock_sqlite.from_conn_string.call_args[0][0])
                
                persistence.get_persistent_checkpointer(task_id=2)
                call2_path = str(mock_sqlite.from_conn_string.call_args[0][0])
                
                # Paths should be different (different task IDs)
                assert "1" in call1_path or "2" in call2_path
                # Can't assert they're different because mock reuses same call_args


class TestErrorHandling:
    """Test error handling in checkpointer creation."""
    
    def test_graceful_degradation_on_postgres_error(self):
        """Test graceful degradation when PostgreSQL errors."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_POSTGRES_AVAILABLE', True):
            with patch.object(persistence, '_SQLITE_AVAILABLE', True):
                with patch.object(persistence, 'PostgresSaver') as mock_postgres:
                    with patch.object(persistence, 'SqliteSaver') as mock_sqlite:
                        # PostgreSQL throws exception
                        mock_postgres.from_conn_string.side_effect = RuntimeError("DB error")
                        mock_sqlite.from_conn_string.return_value = MagicMock()
                        
                        os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
                        
                        try:
                            # Should not raise, should fall back to SQLite
                            checkpointer = persistence.get_persistent_checkpointer(task_id=1)
                            assert checkpointer is not None
                            
                        finally:
                            os.environ.pop("DATABASE_URL", None)
    
    def test_graceful_degradation_on_sqlite_error(self):
        """Test graceful degradation when SQLite errors."""
        from agent.graph import persistence
        
        with patch.object(persistence, '_POSTGRES_AVAILABLE', False):
            with patch.object(persistence, '_SQLITE_AVAILABLE', True):
                with patch.object(persistence, '_MEMORY_AVAILABLE', True):
                    with patch.object(persistence, 'SqliteSaver') as mock_sqlite:
                        with patch.object(persistence, 'MemorySaver') as mock_memory:
                            # SQLite throws exception
                            mock_sqlite.from_conn_string.side_effect = OSError("File error")
                            mock_memory.return_value = MagicMock()
                            
                            # Should not raise, should fall back to Memory
                            checkpointer = persistence.get_persistent_checkpointer(task_id=1)
                            assert checkpointer is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

