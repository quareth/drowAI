"""Tests for workspace_helpers utility."""

import os
import tempfile
from pathlib import Path

import pytest

from agent.utils.workspace_helpers import (
    get_index_directory,
    get_run_id_from_workspace,
    ensure_workspace_directories,
    resolve_workspace_path,
    resolve_host_workspace_path,
    resolve_container_path,
    resolve_workspace_path_for_executor,
    temporary_cwd,
)


def test_get_index_directory_default():
    """Test default index directory resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        index_dir = get_index_directory(workspace)
        
        # Should return workspace/index
        assert index_dir == os.path.join(workspace, "index")


def test_get_index_directory_with_context():
    """Test index directory resolution with existing context dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        # Create context directory
        context_dir = Path(workspace) / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        
        index_dir = get_index_directory(workspace)
        
        # Should return workspace/index (context.parent/index)
        assert index_dir == os.path.join(workspace, "index")


def test_get_index_directory_env_override():
    """Test index directory resolution with environment override."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        custom_index = "/custom/index/path"
        
        # Set environment variable
        original = os.environ.get("CONTEXT_INDEX_DIR")
        try:
            os.environ["CONTEXT_INDEX_DIR"] = custom_index
            
            index_dir = get_index_directory(workspace, respect_env_override=True)
            
            # Should return custom path
            assert index_dir == custom_index
        finally:
            # Restore original
            if original is None:
                os.environ.pop("CONTEXT_INDEX_DIR", None)
            else:
                os.environ["CONTEXT_INDEX_DIR"] = original


def test_get_index_directory_ignore_env_override():
    """Test index directory resolution ignoring env override."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        custom_index = "/custom/index/path"
        
        # Set environment variable
        original = os.environ.get("CONTEXT_INDEX_DIR")
        try:
            os.environ["CONTEXT_INDEX_DIR"] = custom_index
            
            index_dir = get_index_directory(workspace, respect_env_override=False)
            
            # Should ignore env and return workspace/index
            assert index_dir == os.path.join(workspace, "index")
        finally:
            # Restore original
            if original is None:
                os.environ.pop("CONTEXT_INDEX_DIR", None)
            else:
                os.environ["CONTEXT_INDEX_DIR"] = original


def test_get_run_id_from_workspace_numeric():
    """Test run ID extraction from numeric workspace path."""
    workspace = "/workspace/1423"
    run_id = get_run_id_from_workspace(workspace)
    assert run_id == "1423"


def test_get_run_id_from_workspace_task_prefix():
    """Test run ID extraction from task-prefixed workspace path."""
    workspace = "/workspace/task-1423"
    run_id = get_run_id_from_workspace(workspace)
    assert run_id == "task-1423"


def test_get_run_id_from_workspace_complex_path():
    """Test run ID extraction from complex workspace path."""
    workspace = "/var/workspaces/project-xyz/task-456"
    run_id = get_run_id_from_workspace(workspace)
    assert run_id == "task-456"


def test_get_run_id_from_workspace_empty():
    """Test run ID extraction from empty path."""
    run_id = get_run_id_from_workspace("")
    assert run_id == "default"


def test_get_run_id_from_workspace_root():
    """Test run ID extraction from root path."""
    run_id = get_run_id_from_workspace("/")
    # Root path has no name, should return default
    assert run_id == "default"


def test_ensure_workspace_directories():
    """Test workspace directory creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        artifacts_dir = os.path.join(workspace, "artifacts")
        index_dir = os.path.join(workspace, "index")
        
        # Ensure directories don't exist
        assert not os.path.exists(artifacts_dir)
        assert not os.path.exists(index_dir)
        
        # Create directories
        ensure_workspace_directories(workspace)
        
        # Verify creation
        assert os.path.exists(artifacts_dir)
        assert os.path.isdir(artifacts_dir)
        assert os.path.exists(index_dir)
        assert os.path.isdir(index_dir)


def test_ensure_workspace_directories_idempotent():
    """Test that ensure_workspace_directories is idempotent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        # Create directories first time
        ensure_workspace_directories(workspace)
        
        # Get timestamps
        artifacts_dir = Path(workspace) / "artifacts"
        index_dir = Path(workspace) / "index"
        artifacts_mtime1 = artifacts_dir.stat().st_mtime
        index_mtime1 = index_dir.stat().st_mtime
        
        # Call again (should not fail)
        ensure_workspace_directories(workspace)
        
        # Directories should still exist
        assert artifacts_dir.exists()
        assert index_dir.exists()


def test_ensure_workspace_directories_with_nested_path():
    """Test workspace directory creation with nested path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create deeply nested workspace path
        workspace = os.path.join(tmpdir, "deep", "nested", "workspace")
        
        # Create directories (should create parents too)
        ensure_workspace_directories(workspace)
        
        # Verify creation
        artifacts_dir = os.path.join(workspace, "artifacts")
        index_dir = os.path.join(workspace, "index")
        assert os.path.exists(artifacts_dir)
        assert os.path.exists(index_dir)


def test_resolve_workspace_path_explicit():
    """Test workspace path resolution with explicit override."""
    custom_workspace = "/custom/workspace/path"
    
    resolved = resolve_workspace_path(workspace_override=custom_workspace)
    
    assert resolved == custom_workspace


def test_resolve_workspace_path_from_env():
    """Test workspace path resolution from environment."""
    custom_workspace = "/env/workspace/path"
    
    original = os.environ.get("WORKSPACE")
    try:
        os.environ["WORKSPACE"] = custom_workspace
        
        resolved = resolve_workspace_path()
        
        assert resolved == custom_workspace
    finally:
        # Restore original
        if original is None:
            os.environ.pop("WORKSPACE", None)
        else:
            os.environ["WORKSPACE"] = original


def test_resolve_workspace_path_default():
    """Test workspace path resolution with default fallback."""
    original = os.environ.get("WORKSPACE")
    try:
        # Clear environment variable
        os.environ.pop("WORKSPACE", None)
        
        resolved = resolve_workspace_path()
        
        # Should return default
        assert resolved == "/workspace"
    finally:
        # Restore original
        if original:
            os.environ["WORKSPACE"] = original


def test_resolve_workspace_path_override_priority():
    """Test that explicit override takes priority over environment."""
    custom_workspace = "/override/workspace"
    env_workspace = "/env/workspace"
    
    original = os.environ.get("WORKSPACE")
    try:
        os.environ["WORKSPACE"] = env_workspace
        
        resolved = resolve_workspace_path(workspace_override=custom_workspace)
        
        # Should use override, not env
        assert resolved == custom_workspace
    finally:
        # Restore original
        if original is None:
            os.environ.pop("WORKSPACE", None)
        else:
            os.environ["WORKSPACE"] = original


def test_resolve_workspace_path_no_env_fallback():
    """Test workspace path resolution ignoring environment."""
    env_workspace = "/env/workspace"
    
    original = os.environ.get("WORKSPACE")
    try:
        os.environ["WORKSPACE"] = env_workspace
        
        resolved = resolve_workspace_path(fallback_to_env=False)
        
        # Should ignore env and return default
        assert resolved == "/workspace"
    finally:
        # Restore original
        if original is None:
            os.environ.pop("WORKSPACE", None)
        else:
            os.environ["WORKSPACE"] = original


def test_resolve_workspace_path_for_executor_relative():
    """Test executor path resolution stays within workspace."""
    assert resolve_workspace_path_for_executor(
        "artifacts/test.txt",
        workspace_path="/workspace/task-1",
    ) == "/workspace/task-1/artifacts/test.txt"


def test_resolve_workspace_path_for_executor_rejects_traversal():
    """Test executor helper rejects path traversal outside workspace."""
    with pytest.raises(ValueError, match="outside workspace"):
        resolve_workspace_path_for_executor(
            "../../../etc/passwd",
            workspace_path="/workspace/task-1",
        )


def test_resolve_container_path_relative():
    """Test container helper handles relative paths."""
    assert resolve_container_path(
        "artifacts",
        host_workspace="/workspace/task-1",
    ) == "/workspace/artifacts"


def test_resolve_container_path_host_workspace():
    """Test container helper translates host workspace absolute paths."""
    assert resolve_container_path(
        "/workspace/task-1/artifacts/sample.txt",
        host_workspace="/workspace/task-1",
    ) == "/workspace/artifacts/sample.txt"


def test_resolve_container_path_rejects_tmp():
    """Absolute /tmp paths should be rejected for container execution."""
    with pytest.raises(ValueError, match="cannot be resolved for container"):
        resolve_container_path(
            "/tmp/file.txt",
            host_workspace="/workspace/task-1",
        )


def test_temporary_cwd_restores_previous_directory(tmp_path):
    """Test temporary_cwd restores working directory."""
    original = os.getcwd()
    nested = tmp_path / "nested"
    nested.mkdir()
    with temporary_cwd(str(nested)):
        assert os.getcwd() == str(nested.resolve())
    assert os.getcwd() == original


def test_resolve_host_workspace_path_prefers_workspace_hint(tmp_path):
    """Test host workspace helper uses existing workspace hint."""
    workspace = tmp_path / "task-1"
    workspace.mkdir()
    assert resolve_host_workspace_path(task_id=1, workspace_hint=str(workspace)) == str(workspace)


def test_resolve_host_workspace_path_rejects_missing_runtime_metadata():
    """Host workspace helper fails closed when provider/runtime metadata is missing."""
    with pytest.raises(ValueError, match="requires provider/runtime metadata"):
        resolve_host_workspace_path(task_id=1, workspace_hint=None)


def test_resolve_host_workspace_path_rejects_nonexistent_workspace_hint(tmp_path):
    """Host workspace helper fails closed when workspace hint does not exist."""
    missing_workspace = tmp_path / "missing-task-workspace"
    with pytest.raises(ValueError, match="requires provider/runtime metadata"):
        resolve_host_workspace_path(task_id=1, workspace_hint=str(missing_workspace))


def test_integration_full_workflow():
    """Test complete workflow: resolve workspace, ensure dirs, get paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate task workspace
        task_id = "1423"
        workspace = os.path.join(tmpdir, task_id)
        
        # Ensure directories
        ensure_workspace_directories(workspace)
        
        # Get run ID
        run_id = get_run_id_from_workspace(workspace)
        assert run_id == task_id
        
        # Get index directory
        index_dir = get_index_directory(workspace)
        expected_index = os.path.join(workspace, "index")
        assert index_dir == expected_index
        
        # Verify all directories exist
        assert os.path.exists(os.path.join(workspace, "artifacts"))
        assert os.path.exists(os.path.join(workspace, "index"))

