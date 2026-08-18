"""Tests for persistent checkpoint helper utilities."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
