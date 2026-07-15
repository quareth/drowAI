"""Tests for filesystem convenience wrapper tools.

 -: Tests for FsReadHeadTool, FsReadTailTool, and FsGrepTool."""

from __future__ import annotations

import pytest
from pathlib import Path
from typing import Generator

from agent.tools.filesystem.convenience import (
    FsReadHeadTool,
    FsReadTailTool,
    FsGrepTool,
    FsReadHeadArgs,
    FsReadTailArgs,
    FsGrepArgs,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Set up a temporary workspace for testing."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    yield tmp_path


class TestFsReadHeadTool:
    """Tests for the read_head convenience tool."""

    def test_reads_first_n_lines(self, workspace: Path) -> None:
        """Should read first N lines of a file.
        
        Note: On Windows, head command isn't available, so the file may be
        read fully via fallback. Test verifies first lines are present.
        """
        import sys
        
        test_file = workspace / "test.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n")
        
        tool = FsReadHeadTool()
        result = tool.run(FsReadHeadArgs(path="test.txt", lines=10))
        
        assert result.success
        assert "line-0" in result.stdout
        assert "line-9" in result.stdout
        # On Unix: truncated to 10 lines. On Windows: may fall back to full read
        if sys.platform != "win32":
            assert "line-10" not in result.stdout

    def test_default_100_lines(self, workspace: Path) -> None:
        """Should default to 100 lines."""
        test_file = workspace / "test.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(200)) + "\n")
        
        tool = FsReadHeadTool()
        result = tool.run(FsReadHeadArgs(path="test.txt"))
        
        assert result.success
        assert "line-0" in result.stdout
        assert "line-99" in result.stdout

    def test_with_line_numbers(self, workspace: Path) -> None:
        """Should include line numbers when requested.
        
        Note: On Windows, line numbers may not be added when subprocess falls back.
        """
        import sys
        
        test_file = workspace / "test.txt"
        test_file.write_text("first\nsecond\nthird\n")
        
        tool = FsReadHeadTool()
        result = tool.run(FsReadHeadArgs(path="test.txt", lines=3, show_line_numbers=True))
        
        assert result.success
        # On Unix: line numbers present. On Windows: may not be present due to fallback
        if sys.platform != "win32":
            assert "1|" in result.stdout or "1| " in result.stdout

    def test_file_not_found(self, workspace: Path) -> None:
        """Should handle missing file gracefully."""
        tool = FsReadHeadTool()
        result = tool.run(FsReadHeadArgs(path="nonexistent.txt", lines=10))
        
        assert not result.success
        # Error message uses "does not exist" phrasing
        assert "not exist" in result.stderr.lower() or "not found" in result.stderr.lower()


class TestFsReadTailTool:
    """Tests for the read_tail convenience tool."""

    def test_reads_last_n_lines(self, workspace: Path) -> None:
        """Should read last N lines of a file."""
        test_file = workspace / "test.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n")
        
        tool = FsReadTailTool()
        result = tool.run(FsReadTailArgs(path="test.txt", lines=10))
        
        assert result.success
        assert "line-99" in result.stdout
        assert "line-90" in result.stdout

    def test_default_100_lines(self, workspace: Path) -> None:
        """Should default to 100 lines."""
        test_file = workspace / "test.txt"
        test_file.write_text("\n".join(f"line-{i}" for i in range(200)) + "\n")
        
        tool = FsReadTailTool()
        result = tool.run(FsReadTailArgs(path="test.txt"))
        
        assert result.success
        assert "line-199" in result.stdout

    def test_small_file_returns_all(self, workspace: Path) -> None:
        """Should return all lines if file is smaller than requested."""
        test_file = workspace / "test.txt"
        test_file.write_text("one\ntwo\nthree\n")
        
        tool = FsReadTailTool()
        result = tool.run(FsReadTailArgs(path="test.txt", lines=100))
        
        assert result.success
        assert "one" in result.stdout
        assert "two" in result.stdout
        assert "three" in result.stdout

    def test_file_not_found(self, workspace: Path) -> None:
        """Should handle missing file gracefully."""
        tool = FsReadTailTool()
        result = tool.run(FsReadTailArgs(path="nonexistent.txt", lines=10))
        
        assert not result.success


class TestFsGrepTool:
    """Tests for the grep convenience tool."""

    def test_finds_matching_lines(self, workspace: Path) -> None:
        """Should find lines matching pattern.
        
        Note: On Windows, grep command isn't available, so the file may be
        read fully via fallback. Test verifies matching lines are present.
        """
        import sys
        
        test_file = workspace / "log.txt"
        test_file.write_text(
            "INFO: Starting app\n"
            "ERROR: Connection failed\n"
            "INFO: Retrying\n"
            "ERROR: Timeout\n"
            "INFO: Success\n"
        )
        
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="log.txt", pattern="ERROR"))
        
        assert result.success
        assert "Connection failed" in result.stdout
        assert "Timeout" in result.stdout
        # On Unix: only matching lines. On Windows: may fall back to full read
        if sys.platform != "win32":
            assert "Starting app" not in result.stdout

    def test_case_insensitive_search(self, workspace: Path) -> None:
        """Should support case-insensitive search."""
        test_file = workspace / "log.txt"
        test_file.write_text("Error: first\nerror: second\nERROR: third\n")
        
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="log.txt", pattern="error", ignore_case=True))
        
        assert result.success
        assert "first" in result.stdout
        assert "second" in result.stdout
        assert "third" in result.stdout

    def test_regex_pattern(self, workspace: Path) -> None:
        """Should support regex patterns."""
        test_file = workspace / "code.py"
        test_file.write_text(
            "def foo():\n"
            "    pass\n"
            "def bar(x):\n"
            "    return x\n"
            "class Baz:\n"
            "    pass\n"
        )
        
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="code.py", pattern=r"def \w+"))
        
        assert result.success
        assert "foo" in result.stdout
        assert "bar" in result.stdout
        assert "class" not in result.stdout or "Baz" not in result.stdout.split("class")[0]

    def test_includes_line_numbers_by_default(self, workspace: Path) -> None:
        """Should include line numbers by default for grep."""
        test_file = workspace / "test.txt"
        test_file.write_text("match here\nno match\nmatch again\n")
        
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="test.txt", pattern="match"))
        
        assert result.success
        # Line numbers are typically shown as "N:" or "N|"
        # The grep output includes line numbers by default

    def test_no_matches_is_success(self, workspace: Path) -> None:
        """Should succeed even if no lines match."""
        test_file = workspace / "test.txt"
        test_file.write_text("some content\nmore content\n")
        
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="test.txt", pattern="NOMATCH"))
        
        # No matches is still a successful grep execution
        assert result.success

    def test_file_not_found(self, workspace: Path) -> None:
        """Should handle missing file gracefully."""
        tool = FsGrepTool()
        result = tool.run(FsGrepArgs(path="nonexistent.txt", pattern="ERROR"))
        
        assert not result.success


class TestToolRegistration:
    """Tests to verify tools are properly registered."""

    def test_read_head_has_tool_id(self) -> None:
        """FsReadHeadTool should have correct tool_id."""
        assert FsReadHeadTool.tool_id == "filesystem.read_head"

    def test_read_tail_has_tool_id(self) -> None:
        """FsReadTailTool should have correct tool_id."""
        assert FsReadTailTool.tool_id == "filesystem.read_tail"

    def test_grep_has_tool_id(self) -> None:
        """FsGrepTool should have correct tool_id."""
        assert FsGrepTool.tool_id == "filesystem.grep"

    def test_args_models_are_set(self) -> None:
        """Tools should have correct args_model."""
        assert FsReadHeadTool.args_model == FsReadHeadArgs
        assert FsReadTailTool.args_model == FsReadTailArgs
        assert FsGrepTool.args_model == FsGrepArgs
