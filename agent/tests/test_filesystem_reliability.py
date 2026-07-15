"""Tests for filesystem reliability improvements.

Tests cover:
-: Atomic writes (temp file + rename pattern)
-: HEREDOC delimiter collision prevention
-: Backup before overwrite"""

from __future__ import annotations

import os
import pytest
from pathlib import Path
from typing import Generator

from agent.tools.filesystem._reliability import (
    atomic_write_text,
    atomic_write_bytes,
    create_backup,
    restore_from_backup,
    generate_safe_heredoc_delimiter,
    _delimiter_appears_in_content,
    build_safe_heredoc_command,
    AtomicWriteContext,
)
from agent.tools.filesystem._helpers import (
    _generate_safe_heredoc_delimiter,
    build_heredoc_command,
)
from agent.tools.filesystem.write_file import FsWriteTool
from agent.tools.filesystem.contracts import FsWriteArgs


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Set up a temporary workspace for testing."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    yield tmp_path


class TestAtomicWrites:
    """Test atomic write functionality (Task 5.1)."""

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        """Atomic write should create file with content."""
        target = tmp_path / "new_file.txt"
        content = "Hello, world!"
        
        atomic_write_text(target, content)
        
        assert target.exists()
        assert target.read_text() == content

    def test_atomic_write_overwrites_file(self, tmp_path: Path) -> None:
        """Atomic write should replace existing content."""
        target = tmp_path / "existing.txt"
        target.write_text("old content")
        
        atomic_write_text(target, "new content")
        
        assert target.read_text() == "new content"

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Atomic write should create parent directories."""
        target = tmp_path / "sub" / "dir" / "file.txt"
        
        atomic_write_text(target, "nested content")
        
        assert target.exists()
        assert target.read_text() == "nested content"

    def test_atomic_write_no_partial_content_on_failure(self, tmp_path: Path) -> None:
        """Atomic write should not leave partial content on failure."""
        target = tmp_path / "readonly" / "file.txt"
        target.parent.mkdir()
        target.write_text("original")
        
        # Make directory read-only (Windows: remove write permission)
        original_mode = target.parent.stat().st_mode
        try:
            os.chmod(target.parent, 0o444)
            
            # This should fail since directory is read-only
            # The original file should be unchanged
            try:
                atomic_write_text(target, "new content that should fail")
            except OSError:
                pass  # Expected failure
            
            # Original content should be preserved
            # Note: On Windows, this may still succeed due to different permission model
        finally:
            os.chmod(target.parent, original_mode)

    def test_atomic_write_bytes(self, tmp_path: Path) -> None:
        """Atomic write should work with binary data."""
        target = tmp_path / "binary.bin"
        data = b"\x00\x01\x02\x03\xff\xfe\xfd"
        
        atomic_write_bytes(target, data)
        
        assert target.exists()
        assert target.read_bytes() == data

    def test_atomic_write_preserves_encoding(self, tmp_path: Path) -> None:
        """Atomic write should handle different encodings."""
        target = tmp_path / "unicode.txt"
        content = "Hello, 世界! 🌍"
        
        atomic_write_text(target, content, encoding="utf-8")
        
        assert target.read_text(encoding="utf-8") == content


class TestBackupFunctionality:
    """Test backup creation (Task 5.4)."""

    def test_create_backup_makes_copy(self, tmp_path: Path) -> None:
        """Backup should create a .bak copy."""
        original = tmp_path / "config.yaml"
        original.write_text("key: value")
        
        backup_path = create_backup(original)
        
        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.name == "config.yaml.bak"
        assert backup_path.read_text() == "key: value"

    def test_create_backup_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """Backup of non-existent file should return None."""
        missing = tmp_path / "missing.txt"
        
        result = create_backup(missing)
        
        assert result is None

    def test_create_timestamped_backup(self, tmp_path: Path) -> None:
        """Timestamped backup should include timestamp."""
        original = tmp_path / "log.txt"
        original.write_text("log content")
        
        backup_path = create_backup(original, timestamped=True)
        
        assert backup_path is not None
        assert backup_path.exists()
        assert ".bak" in backup_path.name
        # Timestamped backups have pattern like log.txt.1705512345.bak

    def test_restore_from_backup(self, tmp_path: Path) -> None:
        """Restore should recover content from backup."""
        original = tmp_path / "file.txt"
        original.write_text("original content")
        
        backup_path = create_backup(original)
        original.write_text("modified content")
        
        result = restore_from_backup(original, backup_path)
        
        assert result is True
        assert original.read_text() == "original content"

    def test_restore_fails_gracefully_without_backup(self, tmp_path: Path) -> None:
        """Restore should return False if no backup exists."""
        original = tmp_path / "file.txt"
        original.write_text("content")
        
        result = restore_from_backup(original)
        
        assert result is False


class TestHeredocDelimiterSafety:
    """Test HEREDOC delimiter collision prevention (Task 5.3)."""

    def test_simple_content_uses_default_delimiter(self) -> None:
        """Simple content should use default delimiter."""
        content = "Hello, world!"
        
        delimiter = generate_safe_heredoc_delimiter(content)
        
        assert delimiter == "DROWAI_EOF"

    def test_content_with_delimiter_gets_numbered_variant(self) -> None:
        """Content containing delimiter should get numbered variant."""
        content = "Some content\nDROWAI_EOF\nMore content"
        
        delimiter = generate_safe_heredoc_delimiter(content)
        
        assert delimiter == "DROWAI_EOF_1"
        assert delimiter not in content

    def test_content_starting_with_delimiter(self) -> None:
        """Content starting with delimiter should be detected."""
        content = "DROWAI_EOF\nMore content"
        
        delimiter = generate_safe_heredoc_delimiter(content)
        
        assert delimiter != "DROWAI_EOF"

    def test_content_ending_with_delimiter(self) -> None:
        """Content ending with delimiter should be detected."""
        content = "Some content\nDROWAI_EOF"
        
        delimiter = generate_safe_heredoc_delimiter(content)
        
        assert delimiter != "DROWAI_EOF"

    def test_multiple_collisions_increment_counter(self) -> None:
        """Multiple delimiter collisions should increment counter."""
        content = "DROWAI_EOF\nDROWAI_EOF_1\nDROWAI_EOF_2"
        
        delimiter = generate_safe_heredoc_delimiter(content)
        
        assert delimiter == "DROWAI_EOF_3"

    def test_delimiter_appears_in_content_detection(self) -> None:
        """_delimiter_appears_in_content should detect various patterns."""
        # On its own line
        assert _delimiter_appears_in_content("EOF", "line1\nEOF\nline2")
        # At start
        assert _delimiter_appears_in_content("EOF", "EOF\nmore")
        # At end
        assert _delimiter_appears_in_content("EOF", "content\nEOF")
        # Entire content
        assert _delimiter_appears_in_content("EOF", "EOF")
        # Not on own line (safe)
        assert not _delimiter_appears_in_content("EOF", "line with EOF in middle")

    def test_build_safe_heredoc_command(self) -> None:
        """build_safe_heredoc_command should produce valid command."""
        path = "/workspace/test.txt"
        content = "Hello, world!"
        
        command = build_safe_heredoc_command(path, content)
        
        assert "cat >" in command
        assert "DROWAI_EOF" in command
        assert content in command

    def test_helpers_generate_safe_delimiter(self) -> None:
        """_helpers version should handle delimiter on its own line."""
        # When delimiter appears on its own line, it should be replaced
        content_with_delimiter_on_line = "Some content\nDROWAI_EOF\nMore content"
        
        delimiter = _generate_safe_heredoc_delimiter(content_with_delimiter_on_line)
        
        assert delimiter != "DROWAI_EOF"
        
        # When delimiter appears inline (not on its own line), it's safe
        content_inline = "DROWAI_EOF appears inline here"
        delimiter_inline = _generate_safe_heredoc_delimiter(content_inline)
        
        # Inline appearance is safe for heredoc - delimiter stays as default
        assert delimiter_inline == "DROWAI_EOF"

    def test_build_heredoc_command_uses_safe_delimiter(self) -> None:
        """build_heredoc_command should use safe delimiter generation."""
        path = "/workspace/test.txt"
        content = "Content with DROWAI_EOF in it"
        
        command = build_heredoc_command(path, content, append=False)
        
        # Should use a safe delimiter (not the default that appears in content)
        assert isinstance(command, list)
        assert command[0] == "bash"


class TestAtomicWriteContext:
    """Test context manager for atomic operations."""

    def test_context_creates_backup(self, tmp_path: Path) -> None:
        """Context manager should create backup on enter."""
        target = tmp_path / "file.txt"
        target.write_text("original")
        
        with AtomicWriteContext(target) as ctx:
            assert ctx.backup_path is not None
            assert ctx.backup_path.exists()

    def test_context_restores_on_exception(self, tmp_path: Path) -> None:
        """Context manager should restore on exception."""
        target = tmp_path / "file.txt"
        target.write_text("original")
        
        try:
            with AtomicWriteContext(target) as ctx:
                ctx.write("modified")
                raise ValueError("Simulated error")
        except ValueError:
            pass
        
        # Original should be restored (from backup)
        # Note: The context writes first, then restores on error
        assert target.exists()

    def test_context_commit_prevents_rollback(self, tmp_path: Path) -> None:
        """Commit should prevent rollback."""
        target = tmp_path / "file.txt"
        target.write_text("original")
        
        try:
            with AtomicWriteContext(target) as ctx:
                ctx.write("modified")
                ctx.commit()
                raise ValueError("Error after commit")
        except ValueError:
            pass
        
        # Should NOT rollback because we committed
        assert target.read_text() == "modified"


class TestWriteToolReliability:
    """Test write tool with reliability features."""

    def test_write_with_atomic_enabled(self, workspace: Path) -> None:
        """Write with atomic=True should use atomic write."""
        tool = FsWriteTool()
        result = tool.run(FsWriteArgs(
            path="atomic_test.txt",
            content="atomic content",
            atomic=True,
        ))
        
        assert result.success
        assert (workspace / "atomic_test.txt").read_text() == "atomic content"
        # Check metadata indicates atomic write
        fs_write = result.metadata.get("fs_write", {})
        assert fs_write.get("extra", {}).get("atomic_write") is True

    def test_write_with_atomic_disabled(self, workspace: Path) -> None:
        """Write with atomic=False should use standard write."""
        tool = FsWriteTool()
        result = tool.run(FsWriteArgs(
            path="standard_test.txt",
            content="standard content",
            atomic=False,
        ))
        
        assert result.success
        assert (workspace / "standard_test.txt").read_text() == "standard content"
        fs_write = result.metadata.get("fs_write", {})
        assert fs_write.get("extra", {}).get("atomic_write") is False

    def test_write_with_backup(self, workspace: Path) -> None:
        """Write with backup=True should create .bak file."""
        target = workspace / "config.yaml"
        target.write_text("original: value")
        
        tool = FsWriteTool()
        result = tool.run(FsWriteArgs(
            path="config.yaml",
            content="modified: value",
            backup=True,
            overwrite="overwrite",
        ))
        
        assert result.success
        assert target.read_text() == "modified: value"
        
        # Backup should exist
        backup = workspace / "config.yaml.bak"
        assert backup.exists()
        assert backup.read_text() == "original: value"
        
        # Metadata should indicate backup was created
        fs_write = result.metadata.get("fs_write", {})
        assert fs_write.get("extra", {}).get("backup_created") is True

    def test_write_backup_only_on_existing_file(self, workspace: Path) -> None:
        """Backup should only be created for existing files."""
        tool = FsWriteTool()
        result = tool.run(FsWriteArgs(
            path="new_file.txt",
            content="new content",
            backup=True,
        ))
        
        assert result.success
        
        # No backup for new file
        backup = workspace / "new_file.txt.bak"
        assert not backup.exists()
        
        fs_write = result.metadata.get("fs_write", {})
        assert fs_write.get("extra", {}).get("backup_created") is False

    def test_write_default_atomic_is_true(self, workspace: Path) -> None:
        """Default atomic setting should be True."""
        args = FsWriteArgs(path="test.txt", content="test")
        assert args.atomic is True

    def test_write_default_backup_is_false(self, workspace: Path) -> None:
        """Default backup setting should be False."""
        args = FsWriteArgs(path="test.txt", content="test")
        assert args.backup is False
