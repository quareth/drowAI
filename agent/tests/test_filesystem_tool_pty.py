"""Tests for filesystem command synthesis through PTY transport."""

from __future__ import annotations

import os
from typing import Dict, Any
from unittest.mock import MagicMock, patch

import pytest

from agent.executor import EnhancedCommandExecutor
from agent.tools.filesystem._helpers import resolve_filesystem_command_path
from agent.tool_runtime.command_preparation import prepare_tool_command
from agent.utils.workspace_helpers import resolve_container_path


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


def _make_executor(workspace_path: str) -> EnhancedCommandExecutor:
    config = MagicMock()
    config.task_id = 1
    config.workspace_path = workspace_path
    config.openai_api_key = "test-key-mock"
    config.model_name = "gpt-4"
    config.individual_tool_timeout = 60
    config.tool_execution_timeout = 60
    return EnhancedCommandExecutor(config=config)


class TestFilesystemCommandPathResolution:
    """Test container-runtime path resolution for filesystem command transport."""

    def test_relative_path_resolves_under_workspace(self):
        path = resolve_filesystem_command_path(
            "logs/a.txt",
            resolve_container_path=resolve_container_path,
        )

        assert path == "/workspace/logs/a.txt"

    @pytest.mark.parametrize("value", [None, "", "."])
    def test_default_paths_resolve_to_workspace(self, value):
        path = resolve_filesystem_command_path(
            value,
            resolve_container_path=resolve_container_path,
        )

        assert path == "/workspace"

    @pytest.mark.parametrize("value", ["/", "/opt", "/tmp/file.txt", "/workspace"])
    def test_absolute_kali_paths_are_preserved(self, value):
        path = resolve_filesystem_command_path(
            value,
            resolve_container_path=resolve_container_path,
        )

        assert path == value

    def test_relative_traversal_cannot_escape_workspace(self):
        with pytest.raises(ValueError, match="resolves outside container workspace"):
            resolve_filesystem_command_path(
                "../../../etc/passwd",
                resolve_container_path=resolve_container_path,
            )

    def test_null_byte_rejected(self):
        with pytest.raises(ValueError, match="null byte"):
            resolve_filesystem_command_path(
                "file.txt\x00/etc/passwd",
                resolve_container_path=resolve_container_path,
            )


class TestReadFilePTYCommands:
    """Test filesystem.read_file PTY command generation."""

    def test_head_mode_command(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file", {"path": "file.txt", "read_mode": "head", "num_lines": 5}
        )
        assert "head -n 5" in cmd
        assert "file.txt" in cmd

    def test_tail_mode_command(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file", {"path": "file.txt", "read_mode": "tail", "num_lines": 7}
        )
        assert "tail -n 7" in cmd

    def test_read_tail_alias_uses_read_file_tail_mode(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_tail", {"path": "file.txt", "lines": 50}
        )

        assert "tail -n 50" in cmd
        assert "file.txt" in cmd

    def test_read_tail_alias_preserves_line_numbers(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_tail",
            {"path": "file.txt", "lines": 5, "show_line_numbers": True},
        )

        assert "tail -n 5" in cmd
        assert "awk" in cmd
        assert "NR" in cmd

    def test_read_head_alias_uses_read_file_head_mode(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_head", {"path": "file.txt", "lines": 25}
        )

        assert "head -n 25" in cmd

    def test_grep_alias_uses_read_file_grep_mode(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.grep",
            {
                "path": "file.txt",
                "pattern": "error",
                "ignore_case": True,
                "max_matches": 12,
            },
        )

        assert "grep -E -n -m 12 -i" in cmd
        assert "error" in cmd

    @pytest.mark.asyncio
    async def test_prepare_read_tail_file_comm_command_uses_alias(self, tmp_path):
        executor = _make_executor(str(tmp_path))

        prepared = await prepare_tool_command(
            tool_id="filesystem.read_tail",
            parameters={"path": "file.txt", "lines": 20},
            config=executor.config,
            logger=None,
            transport="file-comm",
            explicit_command_builder=executor._tool_to_shell_command,
        )

        assert "tail -n 20" in prepared.command

    def test_range_mode_command(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "file.txt", "read_mode": "range", "start_line": 10, "num_lines": 5},
        )
        assert "sed -n '10,14p'" in cmd

    def test_grep_mode_command(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "file.txt", "read_mode": "grep", "grep_pattern": "ERROR"},
        )
        assert "grep -E -n" in cmd
        assert "ERROR" in cmd

    def test_search_alias_uses_grep_mode_command(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "file.txt", "search": "ERROR"},
        )

        assert "grep -E -n" in cmd
        assert "head -c" not in cmd

    def test_grep_case_insensitive(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {
                "path": "file.txt",
                "read_mode": "grep",
                "grep_pattern": "error",
                "case_sensitive": False,
            },
        )
        assert "-i" in cmd

    def test_full_mode_fallback(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command("filesystem.read_file", {"path": "file.txt"})
        assert cmd.startswith("head -c ")

    def test_absolute_path_preserved(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "/opt/a.txt"},
        )

        assert "/opt/a.txt" in cmd
        assert "/workspace/opt" not in cmd

    def test_line_numbers_with_head(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {
                "path": "file.txt",
                "read_mode": "head",
                "num_lines": 3,
                "include_line_numbers": True,
            },
        )
        assert "awk" in cmd
        assert "NR" in cmd

    def test_line_numbers_with_tail(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {
                "path": "file.txt",
                "read_mode": "tail",
                "num_lines": 2,
                "include_line_numbers": True,
            },
        )
        assert "tail -n 2" in cmd
        assert "awk" in cmd
        assert "NR" in cmd

    def test_line_numbers_do_not_wrap_grep_mode(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {
                "path": "file.txt",
                "read_mode": "grep",
                "grep_pattern": "ERROR",
                "include_line_numbers": True,
            },
        )

        assert "grep -E -n" in cmd
        assert "awk" not in cmd

    def test_search_text_regex_uses_extended_grep(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.search_text",
            {
                "path": "artifacts/nmap.xml",
                "query": "<(address|port)\\b",
                "use_regex": True,
            },
        )

        assert "grep -n -H -r -E" in cmd
        assert "-F" not in cmd

    def test_search_text_nonrecursive_directory_uses_find(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.search_text",
            {
                "path": "artifacts",
                "query": "address",
                "recursive": False,
            },
        )

        assert "find /workspace/artifacts -maxdepth 1 -type f" in cmd
        assert "else grep" in cmd

    def test_list_dir_absolute_path_preserved(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.list_dir",
            {"path": "/usr/share"},
        )

        assert "ls -la /usr/share" in cmd

    def test_find_paths_absolute_root_preserved(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.find_paths",
            {"path": "/", "filename_glob": "target"},
        )

        assert "find / -name target" in cmd

    def test_search_text_absolute_path_preserved(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.search_text",
            {"path": "/var/log", "query": "ERROR"},
        )

        assert "/var/log" in cmd
        assert "/workspace/var/log" not in cmd

    def test_move_and_copy_absolute_paths_preserved(self, tmp_path):
        executor = _make_executor(str(tmp_path))

        move_cmd = executor._tool_to_shell_command(
            "filesystem.move_path",
            {"src": "/tmp/a.txt", "dest": "/opt/a.txt"},
        )
        copy_cmd = executor._tool_to_shell_command(
            "filesystem.copy_path",
            {"src": "/tmp/a.txt", "dest": "/opt/a.txt"},
        )

        assert move_cmd == "mv /tmp/a.txt /opt/a.txt"
        assert copy_cmd == "cp -r /tmp/a.txt /opt/a.txt"

    def test_workspace_path_validation(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        with pytest.raises(ValueError):
            executor._tool_to_shell_command("filesystem.read_file", {"path": "../etc/passwd"})

    def test_binary_mode_not_supported(self, tmp_path, monkeypatch):
        executor = _make_executor(str(tmp_path))
        monkeypatch.setattr(executor, "_is_pty_enabled", lambda: True)
        assert (
            executor._should_use_pty(
                "filesystem.read_file",
                {"path": "file.txt", "encoding": None, "start_byte": 1},
            )
            is False
        )

    def test_byte_mode_ignores_line_numbers(self, tmp_path):
        executor = _make_executor(str(tmp_path))
        cmd = executor._tool_to_shell_command(
            "filesystem.read_file",
            {
                "path": "file.bin",
                "read_mode": "byte",
                "start_byte": 5,
                "max_bytes": 10,
                "include_line_numbers": True,
            },
        )

        assert "awk" not in cmd
