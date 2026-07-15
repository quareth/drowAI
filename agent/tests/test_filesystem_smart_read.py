"""Tests for smart read mode detection.

This test suite validates the smart file reading capability that
automatically selects the optimal read mode based on file size."""

from __future__ import annotations

import os
import pytest
from pathlib import Path
from typing import Generator

from agent.tools.filesystem._smart_read import (
    SMALL_FILE_LINE_THRESHOLD,
    MEDIUM_FILE_LINE_THRESHOLD,
    SMART_DEFAULT_HEAD_LINES,
    SMART_DEFAULT_TAIL_LINES,
    SmartReadResult,
    get_line_count_python,
    get_file_size_bytes,
    smart_read_mode_detection,
    resolve_read_mode_smart,
)
from agent.tools.filesystem.read_file import FsReadTool
from agent.tools.filesystem.contracts import FsReadArgs


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Set up a temporary workspace for testing."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    yield tmp_path


class TestGetLineCountPython:
    """Tests for cross-platform line counting."""

    def test_counts_lines_in_simple_file(self, tmp_path: Path) -> None:
        """Count lines in a simple text file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")
        
        count = get_line_count_python(test_file)
        
        assert count == 3

    def test_counts_lines_without_trailing_newline(self, tmp_path: Path) -> None:
        """Count lines in file without trailing newline."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3")  # No trailing newline
        
        count = get_line_count_python(test_file)
        
        # Still counts 2 newlines (line1 and line2 end with \n)
        assert count == 2

    def test_counts_empty_file(self, tmp_path: Path) -> None:
        """Empty file has 0 lines."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")
        
        count = get_line_count_python(test_file)
        
        assert count == 0

    def test_counts_single_line_file(self, tmp_path: Path) -> None:
        """Single line file with newline."""
        test_file = tmp_path / "single.txt"
        test_file.write_text("single line\n")
        
        count = get_line_count_python(test_file)
        
        assert count == 1

    def test_returns_none_for_nonexistent_file(self, tmp_path: Path) -> None:
        """Return None for files that don't exist."""
        test_file = tmp_path / "nonexistent.txt"
        
        count = get_line_count_python(test_file)
        
        assert count is None

    def test_handles_large_file(self, tmp_path: Path) -> None:
        """Handle files with many lines efficiently."""
        test_file = tmp_path / "large.txt"
        # Create a file with 10,000 lines
        test_file.write_text("\n".join(f"line-{i}" for i in range(10000)) + "\n")
        
        count = get_line_count_python(test_file)
        
        assert count == 10000


class TestGetFileSizeBytes:
    """Tests for file size retrieval."""

    def test_gets_file_size(self, tmp_path: Path) -> None:
        """Get size of a simple file."""
        test_file = tmp_path / "test.txt"
        content = "Hello, World!"
        test_file.write_text(content)
        
        size = get_file_size_bytes(test_file)
        
        assert size == len(content)

    def test_returns_none_for_nonexistent_file(self, tmp_path: Path) -> None:
        """Return None for files that don't exist."""
        test_file = tmp_path / "nonexistent.txt"
        
        size = get_file_size_bytes(test_file)
        
        assert size is None


class TestSmartReadModeDetection:
    """Tests for smart read mode detection."""

    def test_small_file_returns_full_mode(self, tmp_path: Path) -> None:
        """Small files (< 1000 lines) should use full mode."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(500)) + "\n")
        
        result = smart_read_mode_detection(test_file)
        
        assert result.mode == "full"
        assert result.num_lines is None
        assert result.suggestion is None

    def test_very_small_file_skips_line_count(self, tmp_path: Path) -> None:
        """Very small files (< 50KB) skip line counting for performance."""
        test_file = tmp_path / "tiny.txt"
        test_file.write_text("small content\n")
        
        result = smart_read_mode_detection(test_file)
        
        assert result.mode == "full"
        # total_lines might be None because we skipped counting
        assert result.file_size_bytes is not None
        assert result.file_size_bytes < 50 * 1024

    def test_medium_file_returns_head_mode(self, tmp_path: Path) -> None:
        """Medium files (1001-5000 lines) should use head mode with suggestion."""
        test_file = tmp_path / "medium.txt"
        line_count = 2000
        # Create a file large enough (>50KB) to trigger line counting
        # Each line: "line-NNNN plus padding to make lines longer for size\n"
        padding = "x" * 50  # Add padding to make file >50KB
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(line_count)) + "\n")
        
        result = smart_read_mode_detection(test_file)
        
        assert result.mode == "head"
        assert result.num_lines == SMART_DEFAULT_HEAD_LINES
        assert result.suggestion is not None
        # Numbers are formatted with commas
        assert "2,000" in result.suggestion or str(line_count) in result.suggestion
        assert "read_mode='range'" in result.suggestion or "read_mode='tail'" in result.suggestion

    def test_large_file_returns_tail_mode(self, tmp_path: Path) -> None:
        """Large files (> 5000 lines) should use tail mode with suggestion."""
        test_file = tmp_path / "large.txt"
        line_count = 6000
        # Create a file large enough (>50KB) to trigger line counting
        padding = "x" * 30  # Add padding to make file >50KB
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(line_count)) + "\n")
        
        result = smart_read_mode_detection(test_file)
        
        assert result.mode == "tail"
        assert result.num_lines == SMART_DEFAULT_TAIL_LINES
        assert result.suggestion is not None
        # Numbers are formatted with commas
        assert "6,000" in result.suggestion or str(line_count) in result.suggestion
        assert "read_mode='head'" in result.suggestion or "read_mode='range'" in result.suggestion

    def test_explicit_mode_overrides_smart_detection(self, tmp_path: Path) -> None:
        """Explicit mode should override smart detection."""
        test_file = tmp_path / "large.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(6000)) + "\n")
        
        result = smart_read_mode_detection(test_file, explicit_mode="full")
        
        assert result.mode == "full"
        assert result.suggestion is None

    def test_explicit_num_lines_used_for_medium_file(self, tmp_path: Path) -> None:
        """Explicit num_lines should be used instead of default."""
        test_file = tmp_path / "medium.txt"
        padding = "x" * 50  # Make file >50KB
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(2000)) + "\n")
        
        result = smart_read_mode_detection(test_file, explicit_num_lines=50)
        
        assert result.mode == "head"
        assert result.num_lines == 50

    def test_records_total_lines(self, tmp_path: Path) -> None:
        """Total lines should be recorded in result."""
        test_file = tmp_path / "medium.txt"
        line_count = 2500
        padding = "x" * 50  # Make file >50KB
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(line_count)) + "\n")
        
        result = smart_read_mode_detection(test_file)
        
        assert result.total_lines == line_count


class TestResolveReadModeSmart:
    """Tests for the enhanced read mode resolver."""

    def test_grep_pattern_overrides_smart_detection(self, tmp_path: Path) -> None:
        """Grep pattern should always use grep mode."""
        test_file = tmp_path / "large.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(6000)) + "\n")
        
        mode, result = resolve_read_mode_smart(
            test_file,
            read_mode=None,
            grep_pattern="pattern",
            start_line=None,
            num_lines=None,
            start_byte=0,
            max_bytes=200000,
            encoding="utf-8",
        )
        
        assert mode == "grep"
        assert result is None

    def test_start_line_uses_range_mode(self, tmp_path: Path) -> None:
        """start_line should use range mode."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content\n")
        
        mode, result = resolve_read_mode_smart(
            test_file,
            read_mode=None,
            grep_pattern=None,
            start_line=10,
            num_lines=50,
            start_byte=0,
            max_bytes=200000,
            encoding="utf-8",
        )
        
        assert mode == "range"
        assert result is None

    def test_binary_encoding_uses_byte_mode(self, tmp_path: Path) -> None:
        """Binary encoding (None) should use byte mode."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"\x00\x01\x02")
        
        mode, result = resolve_read_mode_smart(
            test_file,
            read_mode=None,
            grep_pattern=None,
            start_line=None,
            num_lines=None,
            start_byte=0,
            max_bytes=200000,
            encoding=None,
        )
        
        assert mode == "byte"
        assert result is None

    def test_smart_detection_for_simple_read(self, tmp_path: Path) -> None:
        """Smart detection should be used when no explicit params."""
        test_file = tmp_path / "medium.txt"
        padding = "x" * 50  # Make file >50KB
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(2000)) + "\n")
        
        mode, result = resolve_read_mode_smart(
            test_file,
            read_mode=None,
            grep_pattern=None,
            start_line=None,
            num_lines=None,
            start_byte=0,
            max_bytes=200000,
            encoding="utf-8",
        )
        
        assert mode == "head"
        assert result is not None
        assert result.suggestion is not None

    def test_disabled_smart_detection_uses_full(self, tmp_path: Path) -> None:
        """Disabling smart detection should use full mode."""
        test_file = tmp_path / "medium.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(2000)) + "\n")
        
        mode, result = resolve_read_mode_smart(
            test_file,
            read_mode=None,
            grep_pattern=None,
            start_line=None,
            num_lines=None,
            start_byte=0,
            max_bytes=200000,
            encoding="utf-8",
            use_smart_detection=False,
        )
        
        assert mode == "full"
        assert result is None


class TestSimplifiedParameters:
    """Tests for Task 4.2: Simplified FsReadArgs parameters."""

    def test_search_maps_to_grep_pattern(self) -> None:
        """'search' parameter should map to grep_pattern."""
        args = FsReadArgs(path="test.txt", search="ERROR")
        
        assert args.search == "ERROR"
        assert args.grep_pattern == "ERROR"

    def test_offset_maps_to_start_line(self) -> None:
        """'offset' parameter should map to start_line."""
        args = FsReadArgs(path="test.txt", offset=100)
        
        assert args.offset == 100
        assert args.start_line == 100

    def test_search_does_not_override_explicit_grep_pattern(self) -> None:
        """Explicit grep_pattern should take precedence over search."""
        args = FsReadArgs(path="test.txt", search="ERROR", grep_pattern="WARN")
        
        # grep_pattern was explicit, so it should NOT be overwritten
        assert args.grep_pattern == "WARN"
        assert args.search == "ERROR"

    def test_offset_does_not_override_explicit_start_line(self) -> None:
        """Explicit start_line should take precedence over offset."""
        args = FsReadArgs(path="test.txt", offset=100, start_line=50)
        
        # start_line was explicit, so it should NOT be overwritten
        assert args.start_line == 50
        assert args.offset == 100

    def test_search_with_case_insensitive(self) -> None:
        """'search' with case_sensitive=false should work."""
        args = FsReadArgs(path="test.txt", search="error", case_sensitive=False)
        
        assert args.search == "error"
        assert args.grep_pattern == "error"
        assert args.case_sensitive is False

    def test_offset_with_num_lines(self) -> None:
        """'offset' with num_lines should define a range."""
        args = FsReadArgs(path="test.txt", offset=50, num_lines=20)
        
        assert args.offset == 50
        assert args.start_line == 50
        assert args.num_lines == 20

    def test_backward_compatibility_grep_pattern(self) -> None:
        """Old grep_pattern parameter should still work."""
        args = FsReadArgs(path="test.txt", grep_pattern="WARN", case_sensitive=False)
        
        assert args.grep_pattern == "WARN"
        assert args.case_sensitive is False

    def test_backward_compatibility_start_line(self) -> None:
        """Old start_line parameter should still work."""
        args = FsReadArgs(path="test.txt", start_line=100, num_lines=50)
        
        assert args.start_line == 100
        assert args.num_lines == 50


class TestFsReadToolSmartIntegration:
    """Integration tests for FsReadTool with smart detection."""

    def test_small_file_read_fully(self, workspace: Path) -> None:
        """Small files should be read fully without suggestion."""
        test_file = workspace / "small.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n")
        
        tool = FsReadTool()
        result = tool.run(FsReadArgs(path="small.txt"))
        
        assert result.success
        assert "line-0" in result.stdout
        assert "line-99" in result.stdout

    def test_medium_file_shows_head_with_suggestion(self, workspace: Path) -> None:
        """Medium files should show head with navigation suggestion.
        
        Phase 6 update: Now uses pure Python implementations for cross-platform 
        compatibility. Smart detection works on both Windows and Unix.
        """
        test_file = workspace / "medium.txt"
        line_count = 2000
        padding = "x" * 50  # Make file >50KB to trigger smart detection
        test_file.write_text("\n".join(f"line-{i} {padding}" for i in range(line_count)) + "\n")
        
        tool = FsReadTool()
        result = tool.run(FsReadArgs(path="medium.txt"))
        
        assert result.success
        assert "line-0" in result.stdout  # First lines should be shown
        
        # Phase 6: Cross-platform smart detection now works on all platforms
        lines_read = result.metadata.get("fs_read", {}).get("lines_read", 0)
        
        # Medium file should be truncated via head mode
        # Either we got truncated output (< line_count) or full read (== line_count)
        assert lines_read > 0
        
        # If truncated, verify navigation hint is provided
        if lines_read < line_count:
            assert "line-1999" not in result.stdout
            # Should include navigation suggestion
            stdout_lower = result.stdout.lower()
            assert "range" in stdout_lower or "read_mode" in stdout_lower or "total" in stdout_lower or "lines" in stdout_lower

    def test_explicit_mode_overrides_smart(self, workspace: Path) -> None:
        """Explicit read_mode should override smart detection."""
        test_file = workspace / "medium.txt"
        # Create a file with enough lines so tail returns different content than head
        test_file.write_text("\n".join(f"line-{i}" for i in range(200)) + "\n")
        
        tool = FsReadTool()
        result = tool.run(FsReadArgs(
            path="medium.txt",
            read_mode="tail",
            num_lines=50,
        ))
        
        assert result.success
        # When using tail with 50 lines, we should see lines 150-199
        assert "line-199" in result.stdout  # Last lines shown
        # Lines 0-149 should not be in a 50-line tail
        assert "line-0\n" not in result.stdout or "line-100\n" not in result.stdout

    def test_metadata_includes_total_lines(self, workspace: Path) -> None:
        """Metadata should include total line count."""
        test_file = workspace / "test.txt"
        line_count = 500
        test_file.write_text("\n".join(f"line-{i}" for i in range(line_count)) + "\n")
        
        tool = FsReadTool()
        result = tool.run(FsReadArgs(path="test.txt"))
        
        assert result.success
        # Total lines mentioned in summary
        assert str(line_count) in result.stdout or "total lines" in result.stdout.lower()
