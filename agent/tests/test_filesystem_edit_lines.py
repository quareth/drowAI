"""Tests for filesystem.edit_lines surgical editing tool.

This test suite verifies the line-level editing capability that allows
targeted file modifications without full file rewrites.
"""

from __future__ import annotations

import os
import pytest
from pathlib import Path
from typing import Generator

from agent.tools.filesystem.edit_lines import FsEditLinesTool
from agent.tools.filesystem.contracts import FsEditLinesArgs


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Set up a temporary workspace for testing."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    yield tmp_path


class TestFsEditLinesTool:
    """Test the FsEditLinesTool implementation."""

    # ========================================================================
    # Replace Mode Tests
    # ========================================================================

    def test_replace_single_line(self, workspace: Path) -> None:
        """Replace a single line in a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="modified",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nmodified\nline3\n"

    def test_replace_line_range(self, workspace: Path) -> None:
        """Replace a range of lines with new content."""
        test_file = workspace / "test.txt"
        test_file.write_text("a\nb\nc\nd\ne\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            end_line=4,
            new_content="X\nY",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "a\nX\nY\ne\n"

    def test_replace_with_more_lines(self, workspace: Path) -> None:
        """Replace fewer lines with more lines (expansion)."""
        test_file = workspace / "test.txt"
        test_file.write_text("before\noriginal\nafter\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="line1\nline2\nline3",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "before\nline1\nline2\nline3\nafter\n"

    def test_replace_with_fewer_lines(self, workspace: Path) -> None:
        """Replace more lines with fewer lines (contraction)."""
        test_file = workspace / "test.txt"
        test_file.write_text("keep\ndelete1\ndelete2\ndelete3\nkeep\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            end_line=4,
            new_content="single",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "keep\nsingle\nkeep\n"

    def test_replace_with_empty_content(self, workspace: Path) -> None:
        """Replace lines with empty content (effectively delete)."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nline3\n"

    def test_replace_first_line(self, workspace: Path) -> None:
        """Replace the first line of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("old_first\nsecond\nthird\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=1,
            new_content="new_first",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "new_first\nsecond\nthird\n"

    def test_replace_last_line(self, workspace: Path) -> None:
        """Replace the last line of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("first\nsecond\nold_last\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=3,
            new_content="new_last",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "first\nsecond\nnew_last\n"

    # ========================================================================
    # Insert Mode Tests
    # ========================================================================

    def test_insert_before_line(self, workspace: Path) -> None:
        """Insert content before a specific line."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="line2",
            mode="insert",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nline2\nline3\n"

    def test_insert_at_beginning(self, workspace: Path) -> None:
        """Insert content at the beginning of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("original_first\nsecond\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=1,
            new_content="new_first",
            mode="insert",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "new_first\noriginal_first\nsecond\n"

    def test_insert_multiple_lines(self, workspace: Path) -> None:
        """Insert multiple lines at once."""
        test_file = workspace / "test.txt"
        test_file.write_text("header\nfooter\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="body1\nbody2\nbody3",
            mode="insert",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "header\nbody1\nbody2\nbody3\nfooter\n"

    def test_insert_at_end(self, workspace: Path) -> None:
        """Insert at a line beyond file length (append at end)."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=3,
            new_content="line3",
            mode="insert",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nline2\nline3\n"

    # ========================================================================
    # Delete Mode Tests
    # ========================================================================

    def test_delete_single_line(self, workspace: Path) -> None:
        """Delete a single line from a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\ndelete_me\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            mode="delete",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nline3\n"

    def test_delete_line_range(self, workspace: Path) -> None:
        """Delete a range of lines."""
        test_file = workspace / "test.txt"
        test_file.write_text("keep\ndelete1\ndelete2\nkeep\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            end_line=3,
            mode="delete",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "keep\nkeep\n"

    def test_delete_first_line(self, workspace: Path) -> None:
        """Delete the first line of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("delete_me\nkeep1\nkeep2\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=1,
            mode="delete",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "keep1\nkeep2\n"

    def test_delete_last_line(self, workspace: Path) -> None:
        """Delete the last line of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("keep1\nkeep2\ndelete_me\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=3,
            mode="delete",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "keep1\nkeep2\n"

    def test_delete_ignores_new_content(self, workspace: Path) -> None:
        """Delete mode should ignore new_content parameter."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\ndelete\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="this should be ignored",
            mode="delete",
        ))

        assert result.success
        content = test_file.read_text()
        assert content == "line1\nline3\n"
        assert "this should be ignored" not in content

    # ========================================================================
    # Backup Tests
    # ========================================================================

    def test_backup_created_when_requested(self, workspace: Path) -> None:
        """Backup file should be created when backup=True."""
        test_file = workspace / "test.txt"
        test_file.write_text("original content")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=1,
            new_content="modified",
            backup=True,
        ))

        assert result.success
        backup_file = workspace / "test.txt.bak"
        assert backup_file.exists()
        assert backup_file.read_text() == "original content"

    def test_no_backup_by_default(self, workspace: Path) -> None:
        """No backup file should be created when backup=False (default)."""
        test_file = workspace / "test.txt"
        test_file.write_text("original content")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=1,
            new_content="modified",
        ))

        assert result.success
        backup_file = workspace / "test.txt.bak"
        assert not backup_file.exists()

    # ========================================================================
    # Error Handling Tests
    # ========================================================================

    def test_file_not_found_error(self, workspace: Path) -> None:
        """Should return LLM-friendly error when file doesn't exist."""
        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="nonexistent.txt",
            start_line=1,
            new_content="test",
        ))

        assert not result.success
        assert "not found" in result.stderr.lower() or "does not exist" in result.stderr.lower()
        assert "filesystem.list_dir" in result.stderr

    def test_directory_error(self, workspace: Path) -> None:
        """Should return error when path is a directory."""
        subdir = workspace / "subdir"
        subdir.mkdir()

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="subdir",
            start_line=1,
            new_content="test",
        ))

        assert not result.success
        assert "directory" in result.stderr.lower()
        assert "filesystem.list_dir" in result.stderr

    def test_path_traversal_blocked(self, workspace: Path) -> None:
        """Should block path traversal attempts."""
        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="../../../etc/passwd",
            start_line=1,
            new_content="root:x:0:0:",
        ))

        assert not result.success
        assert "workspace" in result.stderr.lower()

    def test_line_out_of_range_error(self, workspace: Path) -> None:
        """Should return error when start_line exceeds file length."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=100,
            new_content="test",
        ))

        assert not result.success
        assert "100" in result.stderr
        assert "2" in result.stderr  # Total lines

    def test_invalid_range_error(self, workspace: Path) -> None:
        """Should return error when end_line < start_line."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=3,
            end_line=1,
            new_content="test",
        ))

        assert not result.success
        assert "end_line" in result.stderr.lower() or "invalid" in result.stderr.lower()

    # ========================================================================
    # Edge Cases
    # ========================================================================

    def test_empty_file(self, workspace: Path) -> None:
        """Handle editing an empty file."""
        test_file = workspace / "empty.txt"
        test_file.write_text("")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="empty.txt",
            start_line=1,
            new_content="content",
            mode="insert",
        ))

        assert result.success
        content = test_file.read_text()
        assert "content" in content

    def test_file_without_trailing_newline(self, workspace: Path) -> None:
        """Handle files without trailing newline."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2")  # No trailing newline

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="modified",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert "line1\n" in content
        assert "modified" in content

    def test_multiline_new_content_without_trailing_newline(self, workspace: Path) -> None:
        """New content without trailing newline should be handled."""
        test_file = workspace / "test.txt"
        test_file.write_text("before\noriginal\nafter\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="new1\nnew2",  # No trailing newline
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert "before\n" in content
        assert "new1\nnew2\n" in content
        assert "after\n" in content

    def test_preserves_file_structure(self, workspace: Path) -> None:
        """Editing should preserve overall file structure."""
        original = "# Header\n\ndef function():\n    pass\n\n# Footer\n"
        test_file = workspace / "code.py"
        test_file.write_text(original)

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="code.py",
            start_line=4,
            new_content="    return True",
            mode="replace",
        ))

        assert result.success
        content = test_file.read_text()
        assert "# Header\n" in content
        assert "def function():\n" in content
        assert "    return True\n" in content
        assert "# Footer\n" in content

    # ========================================================================
    # Metadata Tests
    # ========================================================================

    def test_result_contains_edit_metadata(self, workspace: Path) -> None:
        """Result should contain structured edit metadata."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="modified",
            mode="replace",
        ))

        assert result.success
        assert "fs_edit" in result.metadata
        edit_meta = result.metadata["fs_edit"]
        assert edit_meta["path"] == "test.txt"
        assert edit_meta["mode"] == "replace"
        assert edit_meta["start_line"] == 2
        assert edit_meta["new_line_count"] == 3

    def test_result_contains_diff_preview(self, workspace: Path) -> None:
        """Result stdout should contain diff preview."""
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        tool = FsEditLinesTool()
        result = tool.run(FsEditLinesArgs(
            path="test.txt",
            start_line=2,
            new_content="modified",
            mode="replace",
        ))

        assert result.success
        assert "Changes:" in result.stdout
        assert "modified" in result.stdout


class TestBuildCommand:
    """Test PTY command building for edit_lines."""

    def test_build_command_delete_mode(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Delete mode should produce sed command."""
        monkeypatch.setenv("WORKSPACE", str(workspace))
        (workspace / "test.txt").touch()

        tool = FsEditLinesTool()
        args = FsEditLinesArgs(path="test.txt", start_line=5, end_line=10, mode="delete")
        cmd = tool.build_command(args)

        assert "sed" in cmd
        assert "-i" in cmd
        assert "5,10d" in cmd[2]

    def test_build_command_replace_raises(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace mode should raise ValueError for PTY."""
        monkeypatch.setenv("WORKSPACE", str(workspace))
        (workspace / "test.txt").touch()

        tool = FsEditLinesTool()
        args = FsEditLinesArgs(path="test.txt", start_line=1, new_content="test", mode="replace")

        with pytest.raises(ValueError, match="direct execution"):
            tool.build_command(args)

    def test_build_command_insert_raises(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Insert mode should raise ValueError for PTY."""
        monkeypatch.setenv("WORKSPACE", str(workspace))
        (workspace / "test.txt").touch()

        tool = FsEditLinesTool()
        args = FsEditLinesArgs(path="test.txt", start_line=1, new_content="test", mode="insert")

        with pytest.raises(ValueError, match="direct execution"):
            tool.build_command(args)


class TestSchemaValidation:
    """Test FsEditLinesArgs schema validation."""

    def test_start_line_must_be_positive(self) -> None:
        """start_line must be >= 1."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FsEditLinesArgs(path="test.txt", start_line=0, new_content="test")

    def test_end_line_must_be_positive(self) -> None:
        """end_line must be >= 1 when provided."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            FsEditLinesArgs(path="test.txt", start_line=1, end_line=0, new_content="test")

    def test_default_mode_is_replace(self) -> None:
        """Default mode should be 'replace'."""
        args = FsEditLinesArgs(path="test.txt", start_line=1, new_content="test")
        assert args.mode == "replace"

    def test_default_backup_is_false(self) -> None:
        """Default backup should be False."""
        args = FsEditLinesArgs(path="test.txt", start_line=1, new_content="test")
        assert args.backup is False

    def test_end_line_defaults_to_none(self) -> None:
        """end_line should default to None (tool interprets as start_line)."""
        args = FsEditLinesArgs(path="test.txt", start_line=5, new_content="test")
        assert args.end_line is None
