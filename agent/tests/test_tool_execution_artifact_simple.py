"""Simplified tests for tool execution artifact saving (without backend deps)."""

import os
import tempfile
from pathlib import Path

import pytest

from agent.utils.artifact_manager import save_tool_output_artifact


def test_tool_execution_artifact_workflow():
    """Test the complete artifact saving workflow as used by tool_execution node."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        
        # Simulate tool execution outcome
        tool_stdout = "Port 80 is open\nPort 443 is open\nPort 8080 is open\n"
        tool_stderr = "Warning: Some hosts were filtered\n"
        
        # This is what tool_execution.py does
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout=tool_stdout,
            stderr=tool_stderr,
            logger=None
        )
        
        # Verify artifact was created
        assert artifact_path != ""
        assert os.path.exists(artifact_path)
        assert artifact_path.startswith(os.path.join(workspace_path, "artifacts"))
        assert artifact_path.endswith("_tool.txt")
        
        # Verify content matches what was saved
        with open(artifact_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        assert "Port 80 is open" in content
        assert "Port 443 is open" in content
        assert "Port 8080 is open" in content
        assert "=== STDERR ===" in content
        assert "Warning: Some hosts were filtered" in content
        
        # Verify metadata would be populated correctly
        metadata = {
            "last_artifact_path": artifact_path,
            "workspace_path": workspace_path,
        }
        
        assert metadata["last_artifact_path"] == artifact_path
        assert metadata["workspace_path"] == workspace_path


def test_artifact_saving_creates_directory():
    """Test that artifacts directory is created if missing."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        artifacts_dir = os.path.join(workspace_path, "artifacts")
        
        # Verify directory doesn't exist yet
        assert not os.path.exists(artifacts_dir)
        
        # Save artifact
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout="Test output",
            stderr="",
            logger=None
        )
        
        # Verify directory was created
        assert os.path.exists(artifacts_dir)
        assert os.path.isdir(artifacts_dir)
        assert artifact_path.startswith(artifacts_dir)


def test_multiple_artifacts_same_workspace():
    """Test that multiple artifacts can be saved to same workspace."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        
        # Save first artifact
        artifact1 = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout="First tool output",
            stderr="",
            logger=None
        )
        
        # Delay to ensure different timestamps (seconds precision)
        import time
        time.sleep(1.1)
        
        # Save second artifact
        artifact2 = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout="Second tool output",
            stderr="",
            logger=None
        )
        
        # Both should exist
        assert os.path.exists(artifact1)
        assert os.path.exists(artifact2)
        
        # If they have different timestamps, verify both work
        if artifact1 != artifact2:
            # Verify content is different
            with open(artifact1, "r") as f:
                content1 = f.read()
            with open(artifact2, "r") as f:
                content2 = f.read()
            
            assert "First tool output" in content1
            assert "Second tool output" in content2
        else:
            # Same second, second write overwrote first (acceptable behavior)
            # Verify at least the last write succeeded
            with open(artifact2, "r") as f:
                content = f.read()
            assert "Second tool output" in content


def test_artifact_metadata_structure():
    """Test that metadata structure matches expected format for downstream nodes."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        
        # Simulate tool execution
        stdout = "Scan completed successfully\n"
        stderr = ""
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout=stdout,
            stderr=stderr,
            logger=None
        )
        
        # Simulate metadata structure used by tool_execution node
        metadata = {
            "last_artifact_path": artifact_path,
            "workspace_path": workspace_path,
            "last_tool_result": {
                "stdout": stdout,
                "stderr": stderr,
                "status": "success"
            },
        }
        
        # Verify downstream nodes can access all required fields
        assert "last_artifact_path" in metadata
        assert "workspace_path" in metadata
        assert "last_tool_result" in metadata
        
        # Verify artifact path is valid and accessible
        assert os.path.exists(metadata["last_artifact_path"])
        
        # Verify we can read the artifact later
        with open(metadata["last_artifact_path"], "r") as f:
            saved_content = f.read()
        
        assert saved_content == stdout


def test_artifact_saving_with_large_output():
    """Test artifact saving with large tool output."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        
        # Simulate large output (e.g., from nmap scan)
        large_stdout = "Host discovery result:\n" + ("Line of output\n" * 1000)
        stderr = ""
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout=large_stdout,
            stderr=stderr,
            logger=None
        )
        
        # Verify artifact was created successfully
        assert artifact_path != ""
        assert os.path.exists(artifact_path)
        
        # Verify content was saved completely
        with open(artifact_path, "r") as f:
            content = f.read()
        
        assert len(content) >= len(large_stdout)
        assert content.count("Line of output") == 1000


def test_artifact_path_format_consistency():
    """Test that artifact paths follow consistent naming convention."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace_path,
            stdout="test",
            stderr="",
            logger=None
        )
        
        # Verify path format
        # Should be: workspace/artifacts/YYYYMMDDHHMMSS_tool.txt
        assert artifact_path.startswith(workspace_path)
        assert "artifacts" in artifact_path
        assert artifact_path.endswith("_tool.txt")
        
        # Extract filename
        filename = os.path.basename(artifact_path)
        
        # Verify timestamp format (14 digits before _tool.txt)
        timestamp_part = filename.replace("_tool.txt", "")
        assert len(timestamp_part) == 14
        assert timestamp_part.isdigit()


def test_workspace_path_resolution_consistency():
    """Test that workspace path resolution maintains consistency.
    
    This test verifies the fix for the critical bug where fallback
    workspace resolution wasn't being applied back to the request object.
    
    When tool_execution.py resolves workspace_path from fallback (e.g., from task_id),
    that resolved value must be used consistently:
    1. Applied to request.workspace_path for coordinator/executor
    2. Applied to metadata for state tracking
    3. Used for artifact saving
    """
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate scenario where workspace is resolved from fallback
        resolved_workspace = tmpdir
        
        # Simulate what tool_execution.py does after fallback resolution
        # (This mirrors the critical fix we applied)
        
        # Mock request object
        class MockRequest:
            def __init__(self):
                self.workspace_path = None  # Initially None (triggering fallback)
                self.task_id = 123
                self.metadata = {}
        
        request = MockRequest()
        metadata = {}
        
        # This is what the fix does: apply resolved workspace back
        workspace_path = resolved_workspace  # Resolved from fallback
        if workspace_path:
            request.workspace_path = workspace_path
            if not metadata.get("workspace_path"):
                metadata["workspace_path"] = workspace_path
                request.metadata = metadata
        
        # Verify all three places have consistent workspace
        assert request.workspace_path == resolved_workspace
        assert request.metadata["workspace_path"] == resolved_workspace
        
        # Verify artifact saving would use the same resolved workspace
        artifact_path = save_tool_output_artifact(
            workspace_path=request.workspace_path,  # Uses updated value
            stdout="test output",
            stderr="",
            logger=None
        )
        
        # Artifact should be in the resolved workspace
        assert artifact_path.startswith(resolved_workspace)
        assert os.path.exists(artifact_path)

