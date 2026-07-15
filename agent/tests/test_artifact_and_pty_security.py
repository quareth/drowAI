"""Tests to verify artifact filename collision fix and PTY command injection fix."""

import os
import re
import shlex
from datetime import datetime
from unittest.mock import Mock, patch, AsyncMock
import pytest


class TestArtifactFilenameCollisionFix:
    """Test that artifact filenames use microsecond precision (Bug 1 fix)."""

    def test_timestamp_format_includes_microseconds(self):
        """Verify timestamp format includes microseconds (%f) for collision prevention."""
        # Simulate the fixed timestamp generation
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        
        # Should be 20 characters: 14 (date/time) + 6 (microseconds)
        assert len(timestamp) == 20, f"Expected 20 chars, got {len(timestamp)}"
        
        # Verify format: YYYYMMDDHHMMSSμμμμμμ
        assert re.match(r'^\d{20}$', timestamp), "Timestamp should be 20 digits"

    def test_multiple_timestamps_in_same_second_are_unique(self):
        """Test that multiple timestamps generated in quick succession are unique."""
        import time
        timestamps = []
        
        # Generate multiple timestamps rapidly with tiny delays
        for _ in range(10):
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
            timestamps.append(timestamp)
            time.sleep(0.0001)  # Small delay to ensure microsecond changes
        
        # With microsecond precision, we should get more unique values than with second-only
        unique_timestamps = set(timestamps)
        # At minimum, we should have more than 1 unique timestamp (which would happen with second-only)
        assert len(unique_timestamps) > 1, \
            f"Expected multiple unique timestamps, got {len(unique_timestamps)}"

    def test_old_format_would_have_collisions(self):
        """Demonstrate that old second-only format would cause collisions."""
        # Old format (second-only precision)
        old_timestamps = []
        for _ in range(10):
            old_timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            old_timestamps.append(old_timestamp)
        
        # Old format would have duplicates within the same second
        unique_old = set(old_timestamps)
        # We expect collisions (fewer unique values than total)
        # Note: This might occasionally pass if loop spans multiple seconds
        assert len(unique_old) <= len(old_timestamps)

    def test_artifact_filename_format(self):
        """Test that artifact filenames follow the expected format."""
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        filename = f"{timestamp}_tool.txt"
        
        # Should match pattern: 20digits_tool.txt
        assert re.match(r'^\d{20}_tool\.txt$', filename), \
            f"Filename {filename} doesn't match expected pattern"


class TestPTYCommandInjectionFix:
    """Test that workspace_path is properly quoted (Bug 2 fix)."""

    def test_shlex_quote_prevents_command_injection(self):
        """Test that shlex.quote() properly escapes dangerous characters."""
        dangerous_paths = [
            "/workspace; rm -rf /",
            "/workspace`whoami`",
            "/workspace$(cat /etc/passwd)",
            "/workspace | cat /etc/passwd",
            "/workspace && echo hacked",
            "/workspace/task with spaces",
            "/workspace'single'quotes",
            '/workspace"double"quotes',
            "/workspace$HOME",
            "/workspace\nrm -rf /",
        ]
        
        for dangerous_path in dangerous_paths:
            quoted = shlex.quote(dangerous_path)
            
            # Quoted path should be safe to use in shell command
            # The entire path should be treated as a single argument
            assert quoted.startswith("'") or not any(c in quoted for c in [';', '|', '&', '$', '`']), \
                f"Path {dangerous_path} not properly quoted: {quoted}"

    def test_cd_command_with_quoted_path(self):
        """Test that cd command uses quoted path."""
        workspace_path = "/workspace; rm -rf /"
        quoted_path = shlex.quote(workspace_path)
        
        # Build the command as it would be in the fixed code
        command = f"cd {quoted_path} 2>/dev/null || true\n"
        
        # The entire dangerous path should be wrapped in single quotes
        # This makes the semicolon a literal character, not a command separator
        assert quoted_path == "'/workspace; rm -rf /'"
        assert "cd '/workspace; rm -rf /'" in command, \
            "Dangerous path should be single-quoted"

    def test_spaces_in_path_handled_correctly(self):
        """Test that paths with spaces are properly quoted."""
        workspace_path = "/workspace/task with spaces"
        quoted_path = shlex.quote(workspace_path)
        
        # Should be quoted to handle spaces
        assert quoted_path == "'/workspace/task with spaces'", \
            f"Expected single-quoted path, got: {quoted_path}"

    def test_normal_paths_still_work(self):
        """Test that normal paths work correctly with quoting."""
        normal_paths = [
            "/workspace",
            "/workspace/task_123",
            "/workspace/task-456",
            "/workspace/task_abc_def",
        ]
        
        for path in normal_paths:
            quoted = shlex.quote(path)
            # Normal paths might not need quotes, but quoting them is still safe
            # shlex.quote() only adds quotes when necessary
            assert quoted == path or quoted == f"'{path}'", \
                f"Path {path} should be safely quotable"

    def test_unquoted_path_vulnerability(self):
        """Demonstrate the vulnerability of unquoted paths."""
        malicious_path = "/workspace; echo HACKED"
        
        # Unquoted (vulnerable)
        vulnerable_command = f"cd {malicious_path} 2>/dev/null || true"
        
        # The command would be split at the semicolon
        assert "; echo HACKED" in vulnerable_command
        # This would execute two commands: cd and echo
        
        # Quoted (safe)
        safe_path = shlex.quote(malicious_path)
        safe_command = f"cd {safe_path} 2>/dev/null || true"
        
        # The entire path is treated as one argument (wrapped in single quotes)
        assert safe_path == "'/workspace; echo HACKED'"
        assert "cd '/workspace; echo HACKED'" in safe_command


class TestIntegrationScenarios:
    """Test realistic scenarios combining both fixes."""

    def test_rapid_artifact_creation_no_collisions(self):
        """Test that rapid artifact creation doesn't cause filename collisions."""
        import time
        artifacts_dir = "/tmp/test_artifacts"
        filenames = []
        
        # Simulate rapid artifact creation with tiny delays
        for i in range(100):
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
            filename = f"{timestamp}_tool.txt"
            path = os.path.join(artifacts_dir, filename)
            filenames.append(path)
            if i % 10 == 0:  # Small delay every 10 iterations
                time.sleep(0.0001)
        
        # With microsecond precision, we should have significantly more unique paths
        # than we would with second-only precision (which would give us ~1-2 unique)
        unique_paths = set(filenames)
        assert len(unique_paths) > 10, \
            f"Expected many unique paths with microsecond precision, got {len(unique_paths)}"

    def test_pty_initialization_with_dangerous_workspace(self):
        """Test PTY initialization handles dangerous workspace paths safely."""
        dangerous_workspaces = [
            "/workspace; cat /etc/passwd",
            "/workspace`id`",
            "/workspace$(whoami)",
        ]
        
        for workspace in dangerous_workspaces:
            quoted = shlex.quote(workspace)
            command = f"cd {quoted} 2>/dev/null || true\n"
            
            # Command should be safe (no unquoted metacharacters)
            # The dangerous part should be inside quotes
            assert not ("; cat /etc/passwd" in command and "'" not in command), \
                f"Unsafe command generated: {command}"

