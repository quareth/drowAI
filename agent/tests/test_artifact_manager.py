"""Tests for artifact_manager utility."""

import os
import tempfile
from pathlib import Path

from agent.utils.artifact_manager import save_tool_output_artifact


def _artifact_file(workspace: str, artifact_path: str) -> Path:
    """Resolve the workspace-relative path returned by artifact manager."""
    path = Path(artifact_path)
    if path.is_absolute():
        return path
    return Path(workspace) / path


class MockLogger:
    """Mock logger for testing."""
    
    def __init__(self):
        self.logs = []
    
    def log_operation(self, level: str, message: str):
        """Record log messages."""
        self.logs.append((level, message))


def test_save_tool_output_artifact_basic():
    """Test basic artifact saving functionality."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        stdout = "Port 80 is open\nPort 443 is open\n"
        stderr = ""
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout=stdout,
            stderr=stderr,
            logger=None
        )
        
        # Verify artifact was created
        assert artifact_path != ""
        artifact_file = _artifact_file(workspace, artifact_path)
        assert artifact_file.exists()
        assert artifact_path.startswith("artifacts/")
        
        # Verify content
        with open(artifact_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert content == stdout


def test_save_tool_output_artifact_with_stderr():
    """Test artifact saving with stderr included."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        stdout = "Scanning completed\n"
        stderr = "Warning: some hosts were unreachable\n"
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout=stdout,
            stderr=stderr,
            logger=None
        )
        
        # Verify artifact was created
        assert artifact_path != ""
        artifact_file = _artifact_file(workspace, artifact_path)
        assert artifact_file.exists()
        
        # Verify content includes both stdout and stderr
        with open(artifact_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Scanning completed" in content
        assert "=== STDERR ===" in content
        assert "Warning: some hosts were unreachable" in content


def test_save_tool_output_artifact_with_logger():
    """Test artifact saving logs debug messages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        logger = MockLogger()
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout="test output",
            stderr="",
            logger=logger
        )
        
        # Verify logging occurred
        assert len(logger.logs) > 0
        assert any("Stored tool output to:" in msg for level, msg in logger.logs)


def test_save_tool_output_artifact_requires_workspace_directory():
    """Test that artifact writes fail instead of using fallback storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_file = Path(tmpdir) / "not-a-directory"
        workspace_file.write_text("blocking file", encoding="utf-8")
        logger = MockLogger()

        artifact_path = save_tool_output_artifact(
            workspace_path=str(workspace_file),
            stdout="workspace-only test",
            stderr="",
            logger=logger,
        )

        assert artifact_path == ""
        assert not (Path(tmpdir) / "not-a-directory" / "artifacts").exists()


def test_save_tool_output_artifact_empty_stdout():
    """Test artifact saving with empty stdout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout="",
            stderr="Error occurred",
            logger=None
        )
        
        # Verify artifact was created even with empty stdout
        assert artifact_path != ""
        artifact_file = _artifact_file(workspace, artifact_path)
        assert artifact_file.exists()
        
        # Verify content
        with open(artifact_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "=== STDERR ===" in content
        assert "Error occurred" in content


def test_save_tool_output_artifact_creates_directory():
    """Test that artifacts directory is created if it doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        artifacts_dir = os.path.join(workspace, "artifacts")
        
        # Ensure artifacts directory doesn't exist
        assert not os.path.exists(artifacts_dir)
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout="test",
            stderr="",
            logger=None
        )
        
        # Verify directory was created
        assert os.path.exists(artifacts_dir)
        assert os.path.isdir(artifacts_dir)


def test_save_tool_output_artifact_filename_format():
    """Test that artifact filename follows expected timestamp format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = tmpdir
        
        artifact_path = save_tool_output_artifact(
            workspace_path=workspace,
            stdout="test",
            stderr="",
            logger=None
        )
        
        # Verify filename format: YYYYMMDDHHMMSSffffff_tool.txt
        filename = os.path.basename(artifact_path)
        assert filename.endswith("_tool.txt")
        
        timestamp_part = filename.replace("_tool.txt", "")
        assert len(timestamp_part) == 20  # YYYYMMDDHHMMSSffffff
        assert timestamp_part.isdigit()
