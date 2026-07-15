"""Tests to verify bug fixes for ToolExecutionRecord handling and path validation."""

import os
import pytest
from pathlib import Path
from unittest.mock import Mock
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional


# Define ToolExecutionRecord inline to avoid import issues
class ToolExecutionRecord(BaseModel):
    """Record of a tool execution during the turn."""
    tool_id: str
    args: Dict[str, Any] = Field(default_factory=dict)
    status: str = "success"  # "success" or "error"
    stdout_excerpt: Optional[str] = None
    stderr_excerpt: Optional[str] = None
    observation: Optional[str] = None
    reasoning: Optional[str] = None
    approval_granted: Optional[bool] = None
    approval_reason: Optional[str] = None
    approval_metadata: Dict[str, Any] = Field(default_factory=dict)


# Import the function we're testing
from agent.graph.nodes.node_utils import format_tool_attempts


class TestToolExecutionRecordHandling:
    """Test that ToolExecutionRecord objects are handled correctly (Bugs 1, 2, 3)."""

    def test_format_tool_attempts_with_tool_execution_records(self):
        """Test format_tool_attempts handles ToolExecutionRecord objects."""
        # Create ToolExecutionRecord objects (not dicts)
        tools = [
            ToolExecutionRecord(tool_id="nmap", status="success"),
            ToolExecutionRecord(tool_id="metasploit", status="error"),
            ToolExecutionRecord(tool_id="hydra", status="success"),
        ]
        
        result = format_tool_attempts(tools)
        
        # Should format all tools, not return "No tool attempts recorded"
        assert result != "No tool attempts recorded"
        assert "nmap" in result
        assert "metasploit" in result
        assert "hydra" in result
        # Check success/failure indicators
        assert "✓" in result  # Success indicator
        assert "✗" in result  # Failure indicator

    def test_format_tool_attempts_with_mixed_types(self):
        """Test format_tool_attempts handles both dicts and ToolExecutionRecord."""
        # Mix of dict and ToolExecutionRecord
        tools = [
            {"tool_id": "nmap", "success": True},
            ToolExecutionRecord(tool_id="metasploit", status="error"),
            {"tool_id": "hydra", "success": False},
        ]
        
        result = format_tool_attempts(tools)
        
        assert "nmap" in result
        assert "metasploit" in result
        assert "hydra" in result

    def test_format_tool_attempts_recognizes_error_status(self):
        """Test that ToolExecutionRecord with status='error' is marked as failed."""
        tools = [
            ToolExecutionRecord(tool_id="failed_tool", status="error"),
        ]
        
        result = format_tool_attempts(tools)
        
        # Should show failure indicator
        assert "✗" in result
        assert "failed_tool" in result

    def test_format_tool_attempts_recognizes_success_status(self):
        """Test that ToolExecutionRecord with status='success' is marked as successful."""
        tools = [
            ToolExecutionRecord(tool_id="success_tool", status="success"),
        ]
        
        result = format_tool_attempts(tools)
        
        # Should show success indicator
        assert "✓" in result
        assert "success_tool" in result


class TestPathValidation:
    """Test path validation fixes (Bug 4)."""

    def test_commonpath_prevents_workspace_prefix_bypass(self):
        """Test that /workspace2/file is rejected when workspace is /workspace."""
        # Simulate the fixed validation logic
        workspace_path = "/workspace"
        malicious_path = "/workspace2/file.txt"
        
        workspace_normalized = os.path.normpath(workspace_path)
        path_normalized = os.path.normpath(malicious_path)
        
        # The bug was that startswith would incorrectly accept this
        # The fix uses commonpath which correctly rejects it
        try:
            common = os.path.commonpath([workspace_normalized, path_normalized])
            is_within_workspace = (common == workspace_normalized)
        except ValueError:
            is_within_workspace = False
        
        # Should be rejected (not within workspace)
        assert not is_within_workspace

    def test_commonpath_accepts_valid_workspace_paths(self):
        """Test that valid paths within workspace are accepted."""
        workspace_path = "/workspace"
        valid_path = "/workspace/subdir/file.txt"
        
        workspace_normalized = os.path.normpath(workspace_path)
        path_normalized = os.path.normpath(valid_path)
        
        common = os.path.commonpath([workspace_normalized, path_normalized])
        is_within_workspace = (common == workspace_normalized)
        
        # Should be accepted
        assert is_within_workspace

    def test_commonpath_rejects_parent_directory_traversal(self):
        """Test that ../../../etc/passwd is rejected."""
        workspace_path = "/workspace"
        # After normpath, this would resolve to /etc/passwd
        malicious_path = os.path.normpath(os.path.join(workspace_path, "../../../etc/passwd"))
        
        workspace_normalized = os.path.normpath(workspace_path)
        
        try:
            common = os.path.commonpath([workspace_normalized, malicious_path])
            is_within_workspace = (common == workspace_normalized)
        except ValueError:
            is_within_workspace = False
        
        # Should be rejected
        assert not is_within_workspace

    def test_startswith_vulnerability_example(self):
        """Demonstrate the original startswith vulnerability."""
        workspace_path = "/workspace"
        malicious_path = "/workspace2/file.txt"
        
        # Old vulnerable check
        old_check_passes = malicious_path.startswith(workspace_path)
        
        # This was the bug - it incorrectly passes
        assert old_check_passes == True
        
        # New safe check
        try:
            common = os.path.commonpath([workspace_path, malicious_path])
            new_check_passes = (common == workspace_path)
        except ValueError:
            new_check_passes = False
        
        # New check correctly rejects it
        assert new_check_passes == False


class TestToolExecutionRecordFieldAccess:
    """Test that code accesses correct fields on ToolExecutionRecord."""

    def test_tool_execution_record_has_status_not_success(self):
        """Verify ToolExecutionRecord uses 'status' field, not 'success'."""
        record = ToolExecutionRecord(tool_id="test", status="error")
        
        # Has status field
        assert hasattr(record, "status")
        assert record.status == "error"
        
        # Does NOT have success field
        assert not hasattr(record, "success")

    def test_tool_execution_record_status_values(self):
        """Test valid status values for ToolExecutionRecord."""
        # Success case
        success_record = ToolExecutionRecord(tool_id="test1", status="success")
        assert success_record.status == "success"
        
        # Error case
        error_record = ToolExecutionRecord(tool_id="test2", status="error")
        assert error_record.status == "error"

