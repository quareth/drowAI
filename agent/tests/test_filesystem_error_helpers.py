"""Tests for LLM-friendly filesystem error helpers.

These tests verify that error messages include actionable suggestions
that enable LLM agents to self-correct without human intervention.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from agent.tools.filesystem._error_helpers import (
    build_llm_error,
    _find_similar_names,
    _list_available_items,
    _list_available_directories,
    format_error_with_path_context,
)


class TestBuildLlmError:
    """Test the build_llm_error function for various error types."""

    def test_not_found_lists_available_files(self, tmp_path: Path) -> None:
        """Error should list available files in the directory."""
        (tmp_path / "readme.txt").touch()
        (tmp_path / "config.yaml").touch()

        error = build_llm_error(
            error_type="not_found",
            path="redme.txt",
            workspace=tmp_path,
            message="File 'redme.txt' does not exist.",
        )

        assert "readme.txt" in error
        assert "config.yaml" in error
        assert "filesystem.list_dir" in error

    def test_not_found_suggests_similar_filename(self, tmp_path: Path) -> None:
        """Error should suggest similar filenames for typos."""
        (tmp_path / "configuration.yaml").touch()

        error = build_llm_error(
            error_type="not_found",
            path="config.yaml",
            workspace=tmp_path,
            message="File 'config.yaml' does not exist.",
        )

        assert "Did you mean" in error
        assert "configuration.yaml" in error

    def test_not_found_handles_nested_path(self, tmp_path: Path) -> None:
        """Error should handle nested paths correctly."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "actual_file.txt").touch()

        error = build_llm_error(
            error_type="not_found",
            path="subdir/wrong_file.txt",
            workspace=tmp_path,
            message="File 'subdir/wrong_file.txt' does not exist.",
        )

        assert "actual_file.txt" in error
        assert "subdir" in error

    def test_is_directory_suggests_list_dir(self, tmp_path: Path) -> None:
        """Directory error should suggest list_dir."""
        (tmp_path / "subdir").mkdir()

        error = build_llm_error(
            error_type="is_directory",
            path="subdir",
            workspace=tmp_path,
            message="Cannot read 'subdir'.",
        )

        assert "filesystem.list_dir" in error
        assert "directory" in error.lower()
        assert "filesystem.read_file" in error

    def test_permission_denied_includes_stat_suggestion(self, tmp_path: Path) -> None:
        """Permission error should suggest stat_path."""
        error = build_llm_error(
            error_type="permission_denied",
            path="locked_file.txt",
            workspace=tmp_path,
            message="Permission denied.",
        )

        assert "filesystem.stat_path" in error
        assert "permission" in error.lower()

    def test_path_out_of_workspace_explains_constraints(self, tmp_path: Path) -> None:
        """Path escape error should explain workspace constraints."""
        error = build_llm_error(
            error_type="path_out_of_workspace",
            path="../../../etc/passwd",
            workspace=tmp_path,
            message="Path escapes workspace.",
        )

        assert "relative" in error.lower()
        assert "absolute" in error.lower()
        assert ".." in error
        assert "filesystem.list_dir" in error

    def test_missing_parent_suggests_create_parents(self, tmp_path: Path) -> None:
        """Missing parent error should suggest create_parents option."""
        error = build_llm_error(
            error_type="missing_parent",
            path="nonexistent/subdir/file.txt",
            workspace=tmp_path,
            message="Parent directory does not exist.",
        )

        assert "create_parents=true" in error or "create_parents" in error
        assert "filesystem.make_dir" in error

    def test_missing_parent_lists_existing_directories(self, tmp_path: Path) -> None:
        """Missing parent error should list existing directories."""
        (tmp_path / "existing_dir").mkdir()
        (tmp_path / "another_dir").mkdir()

        error = build_llm_error(
            error_type="missing_parent",
            path="wrong_dir/file.txt",
            workspace=tmp_path,
            message="Parent directory does not exist.",
        )

        assert "existing_dir" in error or "another_dir" in error

    def test_would_overwrite_explains_safe_mode(self, tmp_path: Path) -> None:
        """Overwrite error should explain safe mode and options."""
        error = build_llm_error(
            error_type="would_overwrite",
            path="config.yaml",
            workspace=tmp_path,
            message="File already exists.",
        )

        assert "overwrite='overwrite'" in error or "overwrite" in error.lower()
        assert "filesystem.read_file" in error
        assert "filesystem.append_file" in error

    def test_would_overwrite_includes_existing_size(self, tmp_path: Path) -> None:
        """Overwrite error should show existing file size when provided."""
        error = build_llm_error(
            error_type="would_overwrite",
            path="config.yaml",
            workspace=tmp_path,
            message="File already exists.",
            context={"existing_size": 1234},
        )

        assert "1234" in error
        assert "bytes" in error.lower()

    def test_io_error_suggests_list_dir(self, tmp_path: Path) -> None:
        """IO error should suggest verifying workspace state."""
        error = build_llm_error(
            error_type="io_error",
            path="corrupted.dat",
            workspace=tmp_path,
            message="IO error occurred.",
        )

        assert "filesystem.list_dir" in error
        assert "disk" in error.lower() or "I/O" in error

    def test_invalid_range_includes_total_lines(self, tmp_path: Path) -> None:
        """Invalid range error should include total lines context."""
        error = build_llm_error(
            error_type="invalid_range",
            path="file.txt",
            workspace=tmp_path,
            message="Invalid line range.",
            context={"total_lines": 50},
        )

        assert "50" in error
        assert "start_line" in error or "end_line" in error

    def test_line_out_of_range_suggests_append(self, tmp_path: Path) -> None:
        """Line out of range error should suggest append alternative."""
        error = build_llm_error(
            error_type="line_out_of_range",
            path="file.txt",
            workspace=tmp_path,
            message="Line number exceeds file length.",
            context={"total_lines": 10},
        )

        assert "10" in error
        assert "filesystem.append_file" in error

    def test_not_empty_suggests_recursive(self, tmp_path: Path) -> None:
        """Not empty error should suggest recursive option."""
        error = build_llm_error(
            error_type="not_empty",
            path="directory",
            workspace=tmp_path,
            message="Directory not empty.",
        )

        assert "recursive=true" in error or "recursive" in error.lower()
        assert "filesystem.list_dir" in error

    def test_already_exists_suggests_alternatives(self, tmp_path: Path) -> None:
        """Already exists error should suggest alternatives."""
        error = build_llm_error(
            error_type="already_exists",
            path="existing_dir",
            workspace=tmp_path,
            message="Path already exists.",
        )

        assert "filesystem.delete_path" in error
        assert "already exists" in error.lower()

    def test_source_not_found_for_copy_move(self, tmp_path: Path) -> None:
        """Source not found error should list available files."""
        (tmp_path / "actual_source.txt").touch()

        error = build_llm_error(
            error_type="source_not_found",
            path="wrong_source.txt",
            workspace=tmp_path,
            message="Source file not found.",
        )

        assert "actual_source.txt" in error
        assert "filesystem.list_dir" in error

    def test_dest_exists_suggests_overwrite(self, tmp_path: Path) -> None:
        """Destination exists error should suggest overwrite option."""
        error = build_llm_error(
            error_type="dest_exists",
            path="source.txt",
            workspace=tmp_path,
            message="Cannot copy.",
            context={"dest": "existing_dest.txt"},
        )

        assert "overwrite" in error.lower()
        assert "existing_dest.txt" in error

    def test_unknown_error_type_has_fallback(self, tmp_path: Path) -> None:
        """Unknown error types should have a sensible fallback."""
        error = build_llm_error(
            error_type="unknown_exotic_error",
            path="file.txt",
            workspace=tmp_path,
            message="Something went wrong.",
        )

        assert "filesystem.list_dir" in error
        assert "Something went wrong" in error


class TestFindSimilarNames:
    """Test fuzzy filename matching."""

    def test_finds_exact_case_mismatch(self) -> None:
        """Should match files with different case."""
        candidates = ["README.md", "config.yaml", "test.py"]
        matches = _find_similar_names("readme.md", candidates)

        assert "README.md" in matches

    def test_finds_typos(self) -> None:
        """Should find similar names with typos."""
        candidates = ["configuration.yaml", "settings.json", "data.csv"]
        matches = _find_similar_names("configurtion.yaml", candidates)

        assert "configuration.yaml" in matches

    def test_handles_extensions(self) -> None:
        """Should match when extension is missing or different."""
        candidates = ["config.yaml", "config.json", "config.ini"]
        matches = _find_similar_names("config", candidates)

        # Should find at least one config file
        assert any("config" in m for m in matches)

    def test_returns_empty_for_no_match(self) -> None:
        """Should return empty list when nothing is similar."""
        candidates = ["completely_different.txt"]
        matches = _find_similar_names("xyz123", candidates)

        assert matches == []

    def test_respects_max_results(self) -> None:
        """Should limit number of results."""
        candidates = [f"file{i}.txt" for i in range(10)]
        matches = _find_similar_names("file0.txt", candidates, max_results=2)

        assert len(matches) <= 2

    def test_handles_directory_trailing_slash(self) -> None:
        """Should handle directory names with trailing slash."""
        candidates = ["subdir/", "config.yaml"]
        matches = _find_similar_names("subdirr", candidates)

        assert "subdir" in matches


class TestListAvailableItems:
    """Test available item listing."""

    def test_lists_files_and_directories(self, tmp_path: Path) -> None:
        """Should list both files and directories."""
        (tmp_path / "file.txt").touch()
        (tmp_path / "subdir").mkdir()

        items = _list_available_items(tmp_path)

        assert "file.txt" in items
        assert "subdir/" in items  # Directories have trailing slash

    def test_skips_hidden_files(self, tmp_path: Path) -> None:
        """Should skip files starting with dot."""
        (tmp_path / ".hidden").touch()
        (tmp_path / "visible.txt").touch()

        items = _list_available_items(tmp_path)

        assert ".hidden" not in items
        assert "visible.txt" in items

    def test_respects_max_items(self, tmp_path: Path) -> None:
        """Should limit number of items returned."""
        for i in range(20):
            (tmp_path / f"file{i:02d}.txt").touch()

        items = _list_available_items(tmp_path, max_items=5)

        assert len(items) == 5

    def test_handles_nonexistent_directory(self) -> None:
        """Should return empty list for nonexistent directory."""
        items = _list_available_items(Path("/nonexistent/path/xyz"))

        assert items == []


class TestListAvailableDirectories:
    """Test directory-only listing."""

    def test_lists_only_directories(self, tmp_path: Path) -> None:
        """Should only list directories, not files."""
        (tmp_path / "file.txt").touch()
        (tmp_path / "subdir").mkdir()

        dirs = _list_available_directories(tmp_path)

        assert "subdir/" in dirs
        assert "file.txt" not in dirs
        assert not any("file" in d for d in dirs)

    def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        """Should skip directories starting with dot."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()

        dirs = _list_available_directories(tmp_path)

        assert ".git/" not in dirs
        assert "src/" in dirs


class TestFormatErrorWithPathContext:
    """Test the convenience wrapper function."""

    def test_formats_read_operation(self, tmp_path: Path) -> None:
        """Should format error with read operation context."""
        error = format_error_with_path_context(
            error_type="not_found",
            path="missing.txt",
            workspace=tmp_path,
            operation="read",
        )

        assert "Failed to read 'missing.txt'" in error
        assert "filesystem.list_dir" in error

    def test_formats_write_operation_with_details(self, tmp_path: Path) -> None:
        """Should include operation details."""
        error = format_error_with_path_context(
            error_type="permission_denied",
            path="readonly.txt",
            workspace=tmp_path,
            operation="write",
            details="Access denied by system",
        )

        assert "Failed to write 'readonly.txt'" in error
        assert "Access denied by system" in error
        assert "filesystem.stat_path" in error
