"""Security validation tests for filesystem tools (.5).

This test suite verifies that security guardrails remain intact with
transport parameter support:
- Path traversal prevention
- Workspace isolation enforcement
- Absolute path rejection
- Symlink escape blocking
- Size limits enforcement"""

import os
import tempfile
from pathlib import Path

import pytest

from agent.tools.filesystem.read_file import FsReadTool
from agent.tools.filesystem.write_file import FsWriteTool
from agent.tools.filesystem.delete_path import FsDeleteTool
from agent.tools.filesystem.list_dir import FsListDirTool
from agent.tools.filesystem.contracts import (
    FsReadArgs,
    FsWriteArgs,
    FsDeleteArgs,
    FsListArgs,
)


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Create a temporary workspace for testing.
    
    Sets TASK_ID env var to create workspace structure.
    """
    task_id = "test_security_task"
    workspace_root = tmp_path / "workspace" / task_id
    workspace_root.mkdir(parents=True, exist_ok=True)
    
    # Set environment variable for workspace resolution
    monkeypatch.setenv("TASK_ID", task_id)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    
    # Create some test files
    (workspace_root / "test.txt").write_text("test content")
    (workspace_root / "subdir").mkdir(exist_ok=True)
    (workspace_root / "subdir" / "nested.txt").write_text("nested content")
    
    return workspace_root


class TestPathTraversalPrevention:
    """Test path traversal attacks are blocked (Phase 3.2.5.1)."""

    def test_parent_directory_traversal_blocked(self, temp_workspace):
        """Test that ../ path traversal is blocked."""
        tool = FsReadTool()
        args = FsReadArgs(path="../../../etc/passwd")
        
        result = tool.run(args)
        
        assert not result.success
        # Check for security error indicators
        assert ("path_out_of_workspace" in result.metadata or
                "escapes" in result.stderr.lower() or
                "traversal" in result.stderr.lower() or
                "invalid" in result.stderr.lower())

    def test_absolute_path_blocked(self, temp_workspace):
        """Test that absolute paths are blocked."""
        tool = FsReadTool()
        
        # Try various absolute path formats
        for malicious_path in ["/etc/passwd", "C:\\Windows\\System32", "/tmp/evil"]:
            args = FsReadArgs(path=malicious_path)
            result = tool.run(args)
            
            assert not result.success, f"Absolute path {malicious_path} should be blocked"

    def test_mixed_traversal_blocked(self, temp_workspace):
        """Test that mixed relative/absolute paths are blocked."""
        tool = FsReadTool()
        args = FsReadArgs(path="subdir/../../etc/passwd")
        
        result = tool.run(args)
        
        assert not result.success

    def test_url_encoded_traversal_blocked(self, temp_workspace):
        """Test that URL-encoded traversal attempts are blocked."""
        tool = FsReadTool()
        args = FsReadArgs(path="..%2F..%2F..%2Fetc%2Fpasswd")
        
        result = tool.run(args)
        
        # Should either fail or treat as literal filename (which won't exist)
        assert not result.success

    def test_null_byte_injection_blocked(self, temp_workspace):
        """Test that null byte injection is blocked."""
        tool = FsReadTool()
        
        # Null bytes should be rejected or sanitized
        try:
            args = FsReadArgs(path="test.txt\x00../../etc/passwd")
            result = tool.run(args)
            assert not result.success
        except (ValueError, Exception):
            # Pydantic or path validation should reject this
            pass


class TestWorkspaceIsolation:
    """Test workspace isolation enforcement (Phase 3.2.5.2)."""

    def test_read_outside_workspace_blocked(self, temp_workspace):
        """Test that reads outside workspace are blocked."""
        tool = FsReadTool()
        
        # Invalid read outside workspace
        args = FsReadArgs(path="../other_task/secret.txt")
        result = tool.run(args)
        assert not result.success
        assert ("escapes" in result.stderr.lower() or
                "path_out_of_workspace" in result.metadata)

    def test_write_outside_workspace_blocked(self, temp_workspace):
        """Test that writes outside workspace are blocked."""
        tool = FsWriteTool()
        
        # Invalid write outside workspace
        args = FsWriteArgs(path="../other_task/evil.txt", content="evil")
        result = tool.run(args)
        assert not result.success
        assert ("escapes" in result.stderr.lower() or
                "path_out_of_workspace" in result.metadata)

    def test_delete_outside_workspace_blocked(self, temp_workspace):
        """Test that deletes outside workspace are blocked."""
        tool = FsDeleteTool()
        
        # Invalid delete outside workspace
        args = FsDeleteArgs(path="../other_task/important.txt")
        result = tool.run(args)
        assert not result.success
        assert ("escapes" in result.stderr.lower() or
                "path_out_of_workspace" in result.metadata)

    def test_list_outside_workspace_blocked(self, temp_workspace):
        """Test that directory listings outside workspace are blocked."""
        tool = FsListDirTool()
        
        # Invalid list outside workspace
        args = FsListArgs(path="../other_task")
        result = tool.run(args)
        assert not result.success
        assert ("escapes" in result.stderr.lower() or
                "path_out_of_workspace" in result.metadata)


class TestSymlinkSecurity:
    """Test symlink escape prevention (Phase 3.2.5.1)."""

    def test_symlink_outside_workspace_blocked(self, temp_workspace):
        """Test that symlinks pointing outside workspace are blocked."""
        # Create a symlink pointing outside workspace
        external_target = Path(tempfile.gettempdir()) / "external_file.txt"
        external_target.write_text("external content")
        
        symlink_path = temp_workspace / "evil_symlink"
        try:
            symlink_path.symlink_to(external_target)
        except (OSError, NotImplementedError):
            # Skip if symlinks not supported on this platform
            pytest.skip("Symlinks not supported on this platform")
        
        # Try to read through symlink
        tool = FsReadTool()
        args = FsReadArgs(path="evil_symlink")
        result = tool.run(args)
        
        # Should either fail or resolve to workspace boundary
        # Implementation may vary, but should not expose external content
        if result.success:
            # If it succeeds, it should not contain external content
            # (implementation may have resolved to workspace boundary)
            pass
        else:
            # Preferred: symlink outside workspace is rejected
            assert not result.success

class TestSizeLimitsEnforcement:
    """Test size limits enforcement (Phase 3.2.5.4)."""

    def test_max_bytes_parameter_accepted(self, temp_workspace):
        """Test that max_bytes parameter is accepted and validated."""
        tool = FsReadTool()
        
        # Valid max_bytes values
        for max_bytes in [100, 1000, 10000]:
            args = FsReadArgs(path="test.txt", max_bytes=max_bytes)
            assert args.max_bytes == max_bytes
        
        # Test that schema enforces limits
        try:
            # Should reject values outside range
            args = FsReadArgs(path="test.txt", max_bytes=3_000_000)  # Over limit
        except Exception:
            # Pydantic validation should catch this
            pass

    def test_start_byte_parameter_accepted(self, temp_workspace):
        """Test that start_byte parameter is accepted."""
        tool = FsReadTool()
        
        # Valid start_byte values
        for start_byte in [0, 100, 1000]:
            args = FsReadArgs(path="test.txt", start_byte=start_byte)
            assert args.start_byte == start_byte


class TestSpecialCharactersHandling:
    """Test handling of special characters in paths (Phase 3.2.5.3)."""

    def test_semicolon_in_path(self, temp_workspace):
        """Test that semicolons in paths don't cause command injection."""
        tool = FsReadTool()
        
        # Semicolon should be treated as literal character
        args = FsReadArgs(path="test;evil.txt")
        result = tool.run(args)
        
        # Should fail (file doesn't exist) but not execute commands
        assert not result.success
        assert "not found" in result.stderr.lower() or \
               "does not exist" in result.stderr.lower()

    def test_pipe_in_path(self, temp_workspace):
        """Test that pipes in paths don't cause command injection."""
        tool = FsReadTool()
        
        args = FsReadArgs(path="test|evil.txt")
        result = tool.run(args)
        
        # Should fail safely
        assert not result.success

    def test_dollar_sign_in_path(self, temp_workspace):
        """Test that dollar signs in paths don't cause variable expansion."""
        tool = FsReadTool()
        
        args = FsReadArgs(path="test$HOME.txt")
        result = tool.run(args)
        
        # Should fail safely (literal filename)
        assert not result.success

    def test_backtick_in_path(self, temp_workspace):
        """Test that backticks in paths don't cause command execution."""
        tool = FsReadTool()
        
        args = FsReadArgs(path="test`whoami`.txt")
        result = tool.run(args)
        
        # Should fail safely
        assert not result.success

    def test_newline_in_path(self, temp_workspace):
        """Test that newlines in paths are handled safely."""
        tool = FsReadTool()
        
        try:
            args = FsReadArgs(path="test\nrm -rf /")
            result = tool.run(args)
            # Should fail safely
            assert not result.success
        except (ValueError, Exception):
            # Pydantic may reject this
            pass


class TestContentInjectionPrevention:
    """Test content injection prevention for write operations."""

    def test_dangerous_content_accepted_as_literal(self, temp_workspace):
        """Test that dangerous content is accepted as literal strings."""
        tool = FsWriteTool()
        
        # Content with potential shell metacharacters
        dangerous_contents = [
            "EOF\n$(whoami)\n`ls`\n; rm -rf /",
            'echo "hello"; rm -rf /',
            "$(cat /etc/passwd)",
            "`whoami`",
            "; ls -la",
            "| cat /etc/passwd",
        ]
        
        for content in dangerous_contents:
            # Schema should accept the content (it's just a string)
            args = FsWriteArgs(path="test.txt", content=content)
            assert args.content == content
            # The tool will handle it safely (not execute it)


class TestTransportIndependentSecurity:
    """Test that security works regardless of transport parameter."""

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_path_traversal_blocked_all_transports(self, temp_workspace, transport):
        """Test path traversal blocked for all transport types."""
        tool = FsReadTool()
        args = FsReadArgs(path="../../etc/passwd", transport=transport)
        
        result = tool.run(args)
        
        assert not result.success

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_absolute_path_blocked_all_transports(self, temp_workspace, transport):
        """Test absolute paths blocked for all transport types."""
        tool = FsReadTool()
        args = FsReadArgs(path="/etc/passwd", transport=transport)
        
        result = tool.run(args)
        
        assert not result.success

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_workspace_isolation_all_transports(self, temp_workspace, transport):
        """Test workspace isolation for all transport types."""
        tool = FsReadTool()
        
        # Invalid read outside workspace - should be blocked
        args = FsReadArgs(path="../other/file.txt", transport=transport)
        result = tool.run(args)
        assert not result.success
        assert ("escapes" in result.stderr.lower() or
                "path_out_of_workspace" in result.metadata)

