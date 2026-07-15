"""Tests for Tool-to-Command Converter"""

import shlex
import pytest
from unittest.mock import MagicMock, patch

from agent.executor import EnhancedCommandExecutor
from agent.config import AgentConfig


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


class TestToolToShellCommand:
    """Test _tool_to_shell_command method"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance for testing"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-5.2",
        )
        logger = MagicMock()
        return EnhancedCommandExecutor(config, logger)
    
    def test_shell_exec_conversion(self, executor):
        """Test shell.exec tool conversion"""
        command = executor._tool_to_shell_command(
            "shell.exec",
            {"command": "ls -la"}
        )
        
        assert command == "ls -la"

    def test_tool_to_shell_command_delegates_to_transport_router(self, executor):
        """Shell/filesystem command synthesis should stay owned by the transport router."""
        with patch("agent.executor.build_pty_transport_command", return_value="delegated-cmd") as mock_build:
            command = executor._tool_to_shell_command("shell.exec", {"command": "ls -la"})

        assert command == "delegated-cmd"
        mock_build.assert_called_once()
        call_args = mock_build.call_args
        assert call_args[0][0] == "shell.exec"
        assert call_args[0][1] == {"command": "ls -la"}
        assert callable(call_args.kwargs["resolve_container_path_fn"])
        assert call_args.kwargs["logger"] is executor.logger
    
    def test_shell_script_conversion(self, executor):
        """Test shell.script tool conversion"""
        script = "for i in {1..5}; do echo $i; done"
        command = executor._tool_to_shell_command(
            "shell.script",
            {"script": script}
        )

        assert command == f"bash -c {shlex.quote(script)}"
    
    def test_filesystem_read_file_conversion(self, executor):
        """Test filesystem.read_file tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "test.txt"}
        )
        
        assert "head -c" in command
        assert "test.txt" in command
    
    def test_filesystem_write_file_conversion(self, executor):
        """Test filesystem.write_file tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.write_file",
            {"path": "output.txt", "content": "Hello World"}
        )
        
        assert "cat >" in command
        assert "output.txt" in command
        assert "Hello World" in command
        assert "EOF" in command
    
    def test_filesystem_delete_path_conversion(self, executor):
        """Test filesystem.delete_path tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.delete_path",
            {"path": "old_file.txt"}
        )
        
        assert "rm -rf" in command
        assert "old_file.txt" in command
    
    def test_filesystem_make_dir_conversion(self, executor):
        """Test filesystem.make_dir tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.make_dir",
            {"path": "new_dir"}
        )
        
        assert "mkdir -p" in command
        assert "new_dir" in command
    
    def test_filesystem_list_dir_conversion(self, executor):
        """Test filesystem.list_dir tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.list_dir",
            {"path": "artifacts"}
        )
        
        assert "ls -la" in command
        assert "| head -n" in command
        assert "/workspace/artifacts" in command
    
    def test_filesystem_find_paths_conversion(self, executor):
        """Test filesystem.find_paths tool conversion"""
        command = executor._tool_to_shell_command(
            "filesystem.find_paths",
            {"path": ".", "filename_glob": "*.py"}
        )
        
        assert "find" in command
        assert "| head -n" in command
        assert "*.py" in command

    def test_filesystem_search_text_conversion_is_bounded(self, executor):
        """Test filesystem.search_text PTY command includes output bound."""
        command = executor._tool_to_shell_command(
            "filesystem.search_text",
            {"path": ".", "query": "TODO", "recursive": True},
        )

        assert "grep" in command
        assert "| head -n" in command
        assert "TODO" in command
    
    def test_unsupported_tool_raises_error(self, executor):
        """Test that unsupported tools raise ValueError"""
        with pytest.raises(ValueError) as exc_info:
            executor._tool_to_shell_command(
                "nmap.scan",
                {"target": "192.168.1.1"}
            )

        assert str(exc_info.value) == (
            "Tool nmap.scan does not support PTY execution. "
            "PTY is only available for shell (shell.exec, shell.script) "
            "and filesystem (filesystem.*) tools."
        )
    
    def test_special_characters_are_quoted(self, executor):
        """Test that special characters are properly quoted"""
        command = executor._tool_to_shell_command(
            "filesystem.read_file",
            {"path": "file with spaces.txt"}
        )
        
        # Should use shlex.quote() to handle spaces
        assert "'" in command or '"' in command


class TestResolveWorkspacePath:
    """Test _resolve_workspace_path method"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance for testing"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-5.2",
        )
        logger = MagicMock()
        return EnhancedCommandExecutor(config, logger)
    
    def test_relative_path_resolution(self, executor):
        """Test relative path gets resolved to workspace"""
        path = executor._resolve_workspace_path("test.txt")
        
        assert path.startswith("/workspace/task_1")
        assert path.endswith("test.txt")
    
    def test_absolute_path_validation(self, executor):
        """Test absolute path is validated against workspace"""
        path = executor._resolve_workspace_path("/workspace/task_1/test.txt")
        
        assert path == "/workspace/task_1/test.txt"
    
    def test_path_traversal_blocked(self, executor):
        """Test path traversal attempts are blocked"""
        with pytest.raises(ValueError) as exc_info:
            executor._resolve_workspace_path("../../etc/passwd")
        
        assert "outside workspace" in str(exc_info.value)
    
    def test_absolute_path_outside_workspace_blocked(self, executor):
        """Test absolute paths outside workspace are blocked"""
        with pytest.raises(ValueError):
            executor._resolve_workspace_path("/etc/passwd")
    
    def test_normalization(self, executor):
        """Test path normalization resolves . and ..  correctly"""
        path = executor._resolve_workspace_path("./subdir/../test.txt")
        
        # Should normalize to just test.txt
        assert path == "/workspace/task_1/test.txt"
