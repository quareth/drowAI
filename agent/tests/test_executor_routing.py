"""Tests for Executor Routing Logic
Tests lane-aware routing between PTY, file-comm, and direct execution transports."""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from agent.executor import EnhancedCommandExecutor
from agent.config import AgentConfig
from agent.models import ExecutionResult
from backend.services.terminal.contracts import build_named_agent_session_id


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


class TestPtyRouting:
    """Test PTY routing logic"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance with mocked logger"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        logger = MagicMock()
        return EnhancedCommandExecutor(config, logger)
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_pty_routing_success(self, executor):
        """Test successful PTY routing when flag enabled"""
        # Mock PTY execution
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="PTY output",
                stderr="",
                exit_code=0
            )
            
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"}
            )
            
            # Should use PTY
            mock_pty.assert_called_once()
            assert result.success
            assert result.stdout == "PTY output"
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "false"})
    async def test_pty_disabled_by_flag_fails_closed_without_file_comm(self, executor):
        """Container lane fails closed when PTY is disabled and file-comm is unavailable."""
        # Reset cached flag
        executor._pty_enabled_cached = None
        executor._file_comm = None

        with patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"},
            )

        assert result.success is False
        assert result.exit_code == 3
        assert "Route policy violation" in result.stderr
        mock_direct.assert_not_called()
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_pty_fallback_fails_closed_without_file_comm(self, executor):
        """Container lane fails closed when PTY fails and file-comm is unavailable."""
        executor._should_use_pty = MagicMock(return_value=True)
        executor._file_comm = None
        # Mock PTY execution failure
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.side_effect = Exception("PTY unavailable")

            with patch("agent.executor.run_tool_by_name") as mock_direct:
                result = await executor._execute_single_tool_internal(
                    "shell.exec",
                    {"command": "echo hello", "transport": "pty"},
                )

                mock_pty.assert_called_once()
                mock_direct.assert_not_called()
                assert result.success is False
                assert result.exit_code == 3
                assert "Route policy violation" in result.stderr
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_unsupported_container_tool_fails_closed_without_file_comm(self, executor):
        """Unknown tools fail validation before any transport fallback is attempted."""
        with patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "nmap.scan",
                {"target": "192.168.1.1", "transport": "pty"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "Validation error:" in result.stderr
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"}, clear=False)
    async def test_pty_ffuf_inline_wordlist_materializes_in_task_workspace(self, tmp_path):
        """PTY command building binds WORKSPACE to the task host workspace for ffuf wordlists."""
        config = AgentConfig(
            workspace_path=str(tmp_path),
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())
        executor._pty_enabled_cached = None

        captured: dict[str, str] = {}

        async def _fake_execute_via_pty(*, command, task_id, timeout_sec, workspace_path, interrupt_id=None, tool_call_id=None):
            captured["command"] = command
            captured["workspace_path"] = workspace_path
            return SimpleNamespace(status="success", stdout="", stderr="", exit_code=0)

        with patch("agent.tools.shell._pty_executor.execute_via_pty", side_effect=_fake_execute_via_pty), patch(
            "agent.tool_runtime.pty_transport.build_command_transport_tool_result",
            return_value=ExecutionResult(success=True, stdout="", stderr="", exit_code=0),
        ):
            result = await executor._execute_via_pty(
                "web_applications.web_application_fuzzers.ffuf",
                {
                    "target": "https://example.com/data/FUZZ",
                    "inline_wordlist": ["1", "2", "3"],
                },
            )

        assert result.success is True
        assert "/workspace/wordlists/ffuf_fuzzer_" in captured["command"]
        materialized = list((tmp_path / "wordlists").glob("ffuf_fuzzer_*.txt"))
        assert len(materialized) == 1
        assert materialized[0].read_text(encoding="utf-8") == "1\n2\n3\n"


class TestShouldUsePty:
    """Test _should_use_pty() decision logic"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        return EnhancedCommandExecutor(config, MagicMock())
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_should_use_pty_shell_tool(self, executor):
        """Test PTY enabled for shell tools"""
        executor._pty_enabled_cached = None  # Reset cache
        
        result = executor._should_use_pty(
            "shell.exec",
            {"command": "ls", "transport": "pty"}
        )
        
        assert result is True
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_should_use_pty_filesystem_tool(self, executor):
        """Test PTY enabled for filesystem tools"""
        executor._pty_enabled_cached = None
        
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "test.txt", "transport": "pty"}
        )
        
        assert result is True
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_should_not_use_pty_unsupported_tool(self, executor):
        """Test PTY disabled for unsupported tools"""
        executor._pty_enabled_cached = None
        
        result = executor._should_use_pty(
            "nmap.scan",
            {"target": "192.168.1.1", "transport": "pty"}
        )
        
        assert result is False

    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_should_not_use_pty_file_comm_opt_out(self, executor):
        """Test PTY opt-out when canonical file-comm transport is requested."""
        executor._pty_enabled_cached = None

        result = executor._should_use_pty(
            "shell.exec",
            {"command": "ls", "transport": "file-comm"},
        )

        assert result is False
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "false"})
    def test_should_not_use_pty_flag_disabled(self, executor):
        """Test PTY disabled by feature flag"""
        executor._pty_enabled_cached = None
        
        result = executor._should_use_pty(
            "shell.exec",
            {"command": "ls", "transport": "pty"}
        )
        
        assert result is False
    
    def test_should_not_use_pty_no_transport_param(self, executor):
        """Test PTY not used without transport parameter"""
        os.environ["ENABLE_PTY_EXECUTION"] = "false"
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "shell.exec",
            {"command": "ls"}  # No transport param
        )
        
        assert result is False

    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_pty_routing(self, executor):
        """Test filesystem.read_file routes to PTY when explicitly requested."""
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "test.txt", "encoding": "utf-8", "transport": "pty"},
        )
        assert result is True

    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_binary_skips_pty(self, executor):
        """Test binary reads skip PTY."""
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "data.bin", "encoding": None, "start_byte": 1, "transport": "pty"},
        )
        assert result is False

    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_encoding_none_full_read_uses_pty(self, executor):
        """Explicit encoding=None without byte-range arguments uses PTY."""
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "data.bin", "encoding": None, "transport": "pty"},
        )
        assert result is True

    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_byte_range_uses_pty(self, executor):
        """Test byte ranges with text encoding use PTY."""
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "filesystem.read_file",
            {
                "path": "data.bin",
                "encoding": "utf-8",
                "start_byte": 10,
                "max_bytes": 50,
                "transport": "pty",
            },
        )
        assert result is True
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_without_encoding_param_uses_pty_when_explicit(self, executor):
        """Test filesystem.read_file without encoding uses PTY when explicitly requested.
        
        This is the bug fix test case: When encoding is not in parameters dict,
        the tool should assume the schema default (utf-8) and use PTY, not skip it.
        """
        executor._pty_enabled_cached = None
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "test.txt", "transport": "pty"},  # No encoding parameter - defaults to utf-8
        )
        assert result is True
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_read_file_default_params_require_explicit_transport(self, executor):
        """filesystem.read_file should require explicit transport=pty for PTY routing."""
        executor._pty_enabled_cached = None
        
        # Minimal params - just path (no explicit PTY request)
        result = executor._should_use_pty("filesystem.read_file", {"path": "config.yaml"})
        assert result is False
        
        # With explicit request PTY should be used
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "config.yaml", "transport": "pty"},
        )
        assert result is True

        # With read_mode but no encoding and no explicit PTY request
        result = executor._should_use_pty(
            "filesystem.read_file",
            {"path": "log.txt", "read_mode": "tail", "num_lines": 100}
        )
        assert result is False
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_filesystem_namespace_requires_explicit_pty(self, executor):
        """Test filesystem.* tools only route to PTY when transport=pty."""
        executor._pty_enabled_cached = None
        
        # No explicit transport: should not use PTY
        result = executor._should_use_pty("filesystem.list_dir", {"path": "."})
        assert result is False
        
        result = executor._should_use_pty("filesystem.read_file", {"path": "test.txt"})
        assert result is False

        result = executor._should_use_pty("filesystem.write_file", {"path": "output.txt", "content": "data"})
        assert result is False

        # Explicit PTY transport: should use PTY
        result = executor._should_use_pty("filesystem.list_dir", {"path": ".", "transport": "pty"})
        assert result is True

        result = executor._should_use_pty("filesystem.read_file", {"path": "test.txt", "transport": "pty"})
        assert result is True
        
        result = executor._should_use_pty(
            "filesystem.write_file",
            {"path": "output.txt", "content": "data", "transport": "pty"},
        )
        assert result is True
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_tool_to_shell_command_handles_filesystem_namespace(self, executor):
        """Test _tool_to_shell_command works with filesystem.* namespace"""
        # filesystem.list_dir should generate ls command
        cmd = executor._tool_to_shell_command("filesystem.list_dir", {"path": "test"})
        assert "ls -la" in cmd
        
        # filesystem.read_file should work
        cmd = executor._tool_to_shell_command("filesystem.read_file", {"path": "test/file.txt"})
        assert "head -c" in cmd


class TestToolSupportsPty:
    """Test _tool_supports_pty() helper"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance"""
        config = AgentConfig()
        config.openai_api_key = "test-key"
        config.model_name = "gpt-4"
        return EnhancedCommandExecutor(config, MagicMock())
    
    def test_shell_exec_supports_pty(self, executor):
        """Test shell.exec supports PTY"""
        assert executor._tool_supports_pty("shell.exec") is True
    
    def test_shell_script_supports_pty(self, executor):
        """Test shell.script supports PTY"""
        assert executor._tool_supports_pty("shell.script") is True
    
    def test_filesystem_tools_support_pty(self, executor):
        """Test filesystem tools support PTY"""
        fs_tools = [
            "filesystem.read_file",
            "filesystem.write_file",
            "filesystem.list_dir",
            "filesystem.delete_path",
            "filesystem.make_dir",
        ]
        
        for tool in fs_tools:
            assert executor._tool_supports_pty(tool) is True
    
    def test_unsupported_tools_dont_support_pty(self, executor):
        """Test unsupported tools don't support PTY"""
        unsupported = [
            "nmap.scan",
            "gobuster.dir",
            "metasploit.exploit",
            "sqlmap.scan",
        ]
        
        for tool in unsupported:
            assert executor._tool_supports_pty(tool) is False


class TestFeatureFlagCaching:
    """Test feature flag caching behavior"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance"""
        config = AgentConfig()
        config.openai_api_key = "test-key"
        config.model_name = "gpt-4"
        return EnhancedCommandExecutor(config, MagicMock())
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    def test_flag_cached_after_first_check(self, executor):
        """Test feature flag is cached after first check"""
        executor._pty_enabled_cached = None
        
        # First check
        result1 = executor._is_pty_enabled()
        assert result1 is True
        
        # Change environment (should not affect cached value)
        os.environ["ENABLE_PTY_EXECUTION"] = "false"
        
        # Second check (should use cached value)
        result2 = executor._is_pty_enabled()
        assert result2 is True  # Still True from cache
    
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "false"})
    def test_flag_logs_on_first_check(self, executor):
        """Test feature flag logs status on first check"""
        executor._pty_enabled_cached = None
        
        executor._is_pty_enabled()
        
        # Should have logged the flag state
        assert executor.logger.log_operation.called


class TestMetricsInstrumentation:
    """Test metrics are tracked correctly"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        return EnhancedCommandExecutor(config, MagicMock())
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    @patch('backend.services.metrics.utils.safe_inc')
    async def test_metrics_tracked_on_success(self, mock_safe_inc, executor):
        """Test metrics tracked on successful PTY execution"""
        # Mock PTY execution
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="output",
                stderr="",
                exit_code=0
            )
            
            await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"}
            )
            
            # Should increment attempts and success metrics
            calls = [call[0][0] for call in mock_safe_inc.call_args_list]
            assert "executor_pty_attempts" in calls
            assert "executor_pty_success" in calls
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    @patch('backend.services.metrics.utils.safe_inc')
    async def test_metrics_tracked_on_fallback(self, mock_safe_inc, executor):
        """Test metrics tracked on PTY fallback"""
        executor._file_comm = None
        # Mock PTY execution failure
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.side_effect = Exception("PTY failed")
            await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"}
            )

            # Should increment attempts and fallback metrics
            calls = [call[0][0] for call in mock_safe_inc.call_args_list]
            assert "executor_pty_attempts" in calls
            assert "executor_pty_fallback" in calls


class TestRoutingPriority:
    """Test routing priority: PTY -> file-comm (container lane fail-closed if unavailable)."""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance"""
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())
        # Mock file-comm
        executor._file_comm = MagicMock()
        return executor
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_pty_takes_priority_over_filecomm(self, executor):
        """Test PTY is tried before file-comm when requested"""
        # Mock PTY execution
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="PTY output",
                stderr="",
                exit_code=0
            )
            
            # Mock file-comm (should not be called)
            with patch.object(executor, '_execute_tool_via_comm') as mock_comm:
                mock_comm.return_value = ExecutionResult(
                    success=True,
                    stdout="Comm output",
                    stderr="",
                    exit_code=0
                )
                
                result = await executor._execute_single_tool_internal(
                    "shell.exec",
                    {"command": "echo hello", "transport": "pty"}
                )
                
                # Should use PTY, not file-comm
                mock_pty.assert_called_once()
                mock_comm.assert_not_called()
                assert result.stdout == "PTY output"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_pty_forwards_hitl_correlation_ids(self, executor):
        """PTY path forwards interrupt/tool-call correlation IDs."""
        with patch.object(executor, "_execute_via_pty") as mock_pty:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="PTY output",
                stderr="",
                exit_code=0,
            )
            await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"},
                interrupt_id="it-123",
                tool_call_id="tc-456",
            )

            mock_pty.assert_called_once()
            call_args, call_kwargs = mock_pty.call_args
            assert call_args[0] == "shell.exec"
            assert call_args[1]["command"] == "echo hello"
            assert call_args[1]["transport"] == "pty"
            assert call_kwargs["interrupt_id"] == "it-123"
            assert call_kwargs["tool_call_id"] == "tc-456"
            assert call_kwargs["timeout_plan"].deadline_seconds == 600
    
    @pytest.mark.asyncio
    async def test_filecomm_used_when_no_pty_requested(self, executor):
        """Test file-comm is used when PTY not requested"""
        # Mock file-comm
        with patch.object(executor, '_execute_tool_via_comm') as mock_comm:
            mock_comm.return_value = ExecutionResult(
                success=True,
                stdout="Comm output",
                stderr="",
                exit_code=0
            )
            
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello"}  # No transport param
            )
            
            # Should use file-comm
            mock_comm.assert_called_once()
            assert result.stdout == "Comm output"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_parallel_execution_without_identity_skips_pty_for_filecomm(self, executor):
        """Parallel calls without isolated PTY identity keep the safe fallback path."""
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor,
            "_execute_tool_via_comm",
        ) as mock_comm:
            mock_comm.return_value = ExecutionResult(
                success=True,
                stdout="Comm output",
                stderr="",
                exit_code=0,
            )

            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello"},
                allow_pty=False,
            )

        mock_pty.assert_not_called()
        mock_comm.assert_called_once()
        assert result.stdout == "Comm output"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_parallel_execution_with_named_identity_routes_to_pty(self, executor):
        """Parallel calls with a named PTY session can use PTY transport."""
        executor._pty_enabled_cached = None
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor,
            "_execute_tool_via_comm",
        ) as mock_comm:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="PTY output",
                stderr="",
                exit_code=0,
            )

            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello", "transport": "pty"},
                allow_pty=True,
                tool_call_id="tc_one",
                tool_batch_id="tb_one",
                session_name="parallel_tb_one_tc_one",
                cleanup_session=True,
                artifact_stamp=123456,
            )

        mock_pty.assert_called_once()
        mock_comm.assert_not_called()
        assert mock_pty.call_args.kwargs["session_name"] == "parallel_tb_one_tc_one"
        assert mock_pty.call_args.kwargs["cleanup_session"] is True
        assert mock_pty.call_args.kwargs["artifact_stamp"] == 123456
        assert result.stdout == "PTY output"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_named_pty_transport_records_before_cleanup(self, executor):
        """Named PTY transport records command history before closing the session."""
        executor._pty_enabled_cached = None
        shell_result = SimpleNamespace(status="success", stdout="ok", stderr="", exit_code=0)

        async def _fake_execute_via_pty(**kwargs):
            assert kwargs["session_name"] == "parallel_tb_tc"
            assert "cleanup_session" not in kwargs
            return shell_result

        with patch("agent.tools.shell._pty_executor.execute_via_pty", side_effect=_fake_execute_via_pty), patch(
            "agent.utils.workspace_helpers.resolve_host_workspace_path",
            return_value="/tmp",
        ), patch("backend.services.terminal_session_manager.terminal_session_manager") as mock_terminal_manager:
            mock_terminal_manager.close_session = AsyncMock(return_value=True)

            result = await executor._execute_via_pty(
                "shell.exec",
                {"command": "echo ok", "transport": "pty"},
                tool_call_id="tc",
                tool_batch_id="tb",
                session_name="parallel_tb_tc",
                cleanup_session=True,
                artifact_stamp=123,
            )

        assert result.success is True
        mock_terminal_manager.record_agent_command.assert_called_once()
        assert mock_terminal_manager.record_agent_command.call_args.kwargs["session_name"] == "parallel_tb_tc"
        mock_terminal_manager.close_session.assert_awaited_once_with(
            build_named_agent_session_id(1, "parallel_tb_tc")
        )
        method_names = [call[0] for call in mock_terminal_manager.method_calls]
        assert method_names.index("record_agent_command") < method_names.index("close_session")
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_filecomm_fallback_after_pty_failure(self, executor):
        """Test file-comm is tried after PTY failure"""
        # Mock PTY execution failure
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.side_effect = Exception("PTY failed")
            
            # Mock file-comm
            with patch.object(executor, '_execute_tool_via_comm') as mock_comm:
                mock_comm.return_value = ExecutionResult(
                    success=True,
                    stdout="Comm output",
                    stderr="",
                    exit_code=0
                )
                
                result = await executor._execute_single_tool_internal(
                    "shell.exec",
                    {"command": "echo hello", "transport": "pty"}
                )
                
                # Should try PTY, then fall back to file-comm
                mock_pty.assert_called_once()
                mock_comm.assert_called_once()
                assert result.stdout == "Comm output"
    
    @pytest.mark.asyncio
    @patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"})
    async def test_fail_closed_when_pty_and_filecomm_unavailable(self, executor):
        """Container lane must fail closed when allowed transports are unavailable."""
        # Remove file-comm
        executor._file_comm = None

        # Mock PTY execution failure
        with patch.object(executor, '_execute_via_pty') as mock_pty:
            mock_pty.side_effect = Exception("PTY failed")

            with patch("agent.executor.run_tool_by_name") as mock_direct:
                result = await executor._execute_single_tool_internal(
                    "shell.exec",
                    {"command": "echo hello", "transport": "pty"},
                )

                mock_pty.assert_called_once()
                mock_direct.assert_not_called()
                assert result.success is False
                assert result.exit_code == 3
                assert "Route policy violation" in result.stderr

    @pytest.mark.asyncio
    async def test_fail_closed_includes_route_policy_metadata(self, executor):
        """Fail-closed responses should include deterministic route-policy diagnostics."""
        executor._file_comm = None
        executor._should_use_pty = MagicMock(return_value=False)

        with patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"command": "echo hello"},
            )

        metadata = getattr(result, "metadata", {})
        route_policy = metadata.get("route_policy", {}) if isinstance(metadata, dict) else {}
        assert result.success is False
        assert result.exit_code == 3
        assert route_policy.get("selected_lane") == "container_scoped"
        assert route_policy.get("selected_authority") == "container_local_transport"
        assert route_policy.get("selected_transport") == "blocked-direct"
        assert route_policy.get("event") == "route_policy_violation"
        assert route_policy.get("fallback_reason")
        mock_direct.assert_not_called()


class TestArtifactPropagation:
    """Test artifact propagation across executor transport paths."""

    @pytest.mark.asyncio
    async def test_backend_scoped_direct_execution_propagates_artifacts(self, tmp_path):
        config = AgentConfig(
            workspace_path=str(tmp_path),
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())
        executor._file_comm = None
        executor._should_use_pty = MagicMock(return_value=False)

        with patch("agent.executor.run_tool_by_name") as mock_direct:
            mock_direct.return_value = MagicMock(
                success=True,
                stdout="Direct output",
                stderr="",
                exit_code=0,
                metadata={},
                validation_errors=None,
                artifacts=["artifacts/direct.txt"],
            )

            result = await executor._execute_single_tool_internal(
                "knowledge.cve_lookup",
                {"product": "openssl", "version": "3.0.0"},
            )

        assert result.success is True
        assert getattr(result, "artifacts", []) == ["artifacts/direct.txt"]

    @pytest.mark.asyncio
    async def test_file_comm_execution_propagates_artifacts(self, tmp_path):
        config = AgentConfig(
            workspace_path=str(tmp_path),
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())

        file_comm = MagicMock()
        file_comm.send_command = AsyncMock(return_value="cmd-1")
        file_comm.wait_for_result = AsyncMock(
            return_value={
                "success": True,
                "stdout": "comm output",
                "stderr": "",
                "exit_code": 0,
                "metadata": {"source": "file-comm"},
                "artifacts": ["artifacts/from-comm.txt"],
            }
        )
        executor._file_comm = file_comm

        result = await executor._execute_tool_via_comm(
            "shell.exec",
            {"command": "echo hello"},
        )

        assert result is not None
        assert getattr(result, "artifacts", []) == ["artifacts/from-comm.txt"]
        assert getattr(result, "command_text", None) == "echo hello"

    @pytest.mark.asyncio
    async def test_file_comm_execution_derives_command_text_from_tool_build_command(self, tmp_path):
        config = AgentConfig(
            workspace_path=str(tmp_path),
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())

        file_comm = MagicMock()
        file_comm.send_command = AsyncMock(return_value="cmd-2")
        file_comm.wait_for_result = AsyncMock(
            return_value={
                "success": True,
                "stdout": "scan output",
                "stderr": "",
                "exit_code": 0,
                "metadata": {},
            }
        )
        executor._file_comm = file_comm

        result = await executor._execute_tool_via_comm(
            "information_gathering.network_discovery.nmap",
            {"target": "127.0.0.1"},
        )

        assert result is not None
        command_text = getattr(result, "command_text", None)
        assert isinstance(command_text, str) and command_text.strip()
        assert command_text.startswith("nmap ")

    @pytest.mark.asyncio
    async def test_file_comm_execution_sanitizes_credential_bearing_command_text(self, tmp_path):
        config = AgentConfig(
            workspace_path=str(tmp_path),
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())

        file_comm = MagicMock()
        file_comm.send_command = AsyncMock(return_value="cmd-3")
        file_comm.wait_for_result = AsyncMock(
            return_value={
                "success": True,
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "metadata": {
                    "command_text": "curl --oauth2-bearer super-secret-token https://example.com"
                },
            }
        )
        executor._file_comm = file_comm

        result = await executor._execute_tool_via_comm(
            "information_gathering.web_enumeration.http_request",
            {
                "target": "https://example.com",
                "auth_mode": "bearer",
                "bearer_token": "super-secret-token",
            },
        )

        assert result is not None
        command_text = getattr(result, "command_text", "")
        assert "super-secret-token" not in command_text
        assert "<REDACTED>" in command_text


class TestContainerPathResolution:
    """Test _resolve_container_path() for PTY command path translation"""
    
    @pytest.fixture
    def executor(self):
        """Create executor instance with host workspace path"""
        config = AgentConfig(
            workspace_path="/workspaces/drowAI/agent/workspaces/task-1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        logger = MagicMock()
        return EnhancedCommandExecutor(config, logger)
    
    def test_empty_path_returns_workspace(self, executor):
        """Empty path should return /workspace"""
        assert executor._resolve_container_path("") == "/workspace"
    
    def test_dot_path_returns_workspace(self, executor):
        """'.' path should return /workspace"""
        assert executor._resolve_container_path(".") == "/workspace"
    
    def test_relative_path_joined_with_workspace(self, executor):
        """Relative paths should be joined with /workspace"""
        result = executor._resolve_container_path("artifacts")
        assert result == "/workspace/artifacts"
    
    def test_nested_relative_path(self, executor):
        """Nested relative paths should work correctly"""
        result = executor._resolve_container_path("artifacts/nmap")
        assert result == "/workspace/artifacts/nmap"
    
    def test_relative_path_with_file(self, executor):
        """Relative path to file should work"""
        result = executor._resolve_container_path("scope.md")
        assert result == "/workspace/scope.md"
    
    def test_host_absolute_path_translated(self, executor):
        """Host absolute path starting with workspace should be translated"""
        host_path = "/workspaces/drowAI/agent/workspaces/task-1/artifacts"
        result = executor._resolve_container_path(host_path)
        assert result == "/workspace/artifacts"
    
    def test_host_workspace_root_translated(self, executor):
        """Host workspace root should translate to /workspace"""
        host_path = "/workspaces/drowAI/agent/workspaces/task-1"
        result = executor._resolve_container_path(host_path)
        assert result == "/workspace"
    
    def test_foreign_absolute_path_rejected(self, executor):
        """Absolute paths outside workspace should be rejected"""
        with pytest.raises(ValueError, match="cannot be resolved for container"):
            executor._resolve_container_path("/etc/passwd")

    def test_tmp_path_rejected(self, executor):
        """Absolute /tmp paths should be rejected for container mapping."""
        with pytest.raises(ValueError, match="cannot be resolved for container"):
            executor._resolve_container_path("/tmp/file.txt")
    
    def test_parent_traversal_rejected(self, executor):
        """Path traversal outside workspace should be rejected"""
        with pytest.raises(ValueError, match="resolves outside container workspace"):
            executor._resolve_container_path("../../../etc/passwd")
    
    def test_normalized_path_with_dots(self, executor):
        """Paths with . and .. that stay within workspace should work"""
        result = executor._resolve_container_path("artifacts/../logs/test.log")
        assert result == "/workspace/logs/test.log"
    
    def test_tool_to_shell_command_uses_container_path(self, executor):
        """_tool_to_shell_command should use container paths for filesystem tools"""
        # Test list_dir generates container path
        cmd = executor._tool_to_shell_command("filesystem.list_dir", {"path": "."})
        assert "/workspace" in cmd
        assert "/workspaces/drowAI" not in cmd
    
    def test_tool_to_shell_command_handles_relative_path(self, executor):
        """_tool_to_shell_command should handle relative paths correctly"""
        cmd = executor._tool_to_shell_command("filesystem.list_dir", {"path": "artifacts"})
        assert "/workspace/artifacts" in cmd


class TestShellValidationGate:
    """Invalid shell args must return validation_error before transport routing."""

    @pytest.fixture
    def executor(self):
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())
        executor._file_comm = MagicMock()
        executor._should_use_pty = MagicMock(return_value=True)
        return executor

    @pytest.mark.asyncio
    async def test_shell_exec_missing_command_stops_before_transport(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert getattr(result, "validation_errors", None)
        assert "Validation error:" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_script_missing_body_stops_before_transport(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.script",
                {"transport": "pty", "interpreter": "bash"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert getattr(result, "validation_errors", None)
        assert "Validation error:" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_over_length_rejected_before_transport(self, executor):
        over_limit_command = "echo " + ("a" * 316)  # 321 chars total
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": over_limit_command},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "exceeds max length" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_length_boundary_allowed(self, executor):
        boundary_command = "echo " + ("a" * 315)  # 320 chars total
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="ok",
                stderr="",
                exit_code=0,
            )
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": boundary_command},
            )

        assert result.success is True
        mock_pty.assert_called_once()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_multiline_policy_violation_rejected(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "echo safe\nrm -rf /"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "Shell policy violation in command line 2" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_wrapper_payload_policy_violation_rejected(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "bash -lc 'rm -rf /'"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "Shell policy violation in wrapper payload line 1" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_chained_dangerous_command_rejected(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "echo safe; rm -rf /"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "command line 1 segment 2" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_wrapper_chained_dangerous_command_rejected(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "bash -lc 'echo safe; rm -rf /'"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "wrapper payload line 1 segment 2" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_exec_wrapper_with_absolute_shell_path_rejected(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "/bin/bash -lc 'rm -rf /'"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "wrapper payload line 1" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()


class TestGenericValidationGate:
    """Validation failures must fail closed before transport fallback."""

    @pytest.fixture
    def executor(self):
        config = AgentConfig(
            workspace_path="/workspace/task_1",
            task_id="1",
            openai_api_key="test-key",
            model_name="gpt-4",
        )
        executor = EnhancedCommandExecutor(config, MagicMock())
        executor._file_comm = MagicMock()
        executor._should_use_pty = MagicMock(return_value=True)
        return executor

    @pytest.mark.asyncio
    async def test_nmap_missing_target_stops_before_transport(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "information_gathering.network_discovery.nmap",
                {"transport": "pty", "ports": "80"},
            )

        assert result.success is False
        assert result.exit_code == -1
        assert getattr(result, "validation_errors", None)
        assert "Validation error:" in result.stderr
        mock_pty.assert_not_called()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_pty_validation_exception_does_not_fallback_to_file_comm(self, executor):
        valid_parameters = {"transport": "pty", "target": "127.0.0.1", "ports": "80"}
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            mock_pty.side_effect = ValueError("target must be a valid scan target")
            result = await executor._execute_single_tool_internal(
                "information_gathering.network_discovery.nmap",
                valid_parameters,
            )

        assert result.success is False
        assert result.exit_code == -1
        assert getattr(result, "validation_errors", None)
        assert "Validation error:" in result.stderr
        mock_pty.assert_called_once()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"SHELL_POLICY_ENFORCEMENT": "permissive"})
    async def test_shell_exec_standalone_removal_still_allowed(self, executor):
        with patch.object(executor, "_execute_via_pty") as mock_pty, patch.object(
            executor, "_execute_tool_via_comm"
        ) as mock_comm, patch("agent.executor.run_tool_by_name") as mock_direct:
            mock_pty.return_value = ExecutionResult(
                success=True,
                stdout="removed",
                stderr="",
                exit_code=0,
            )
            result = await executor._execute_single_tool_internal(
                "shell.exec",
                {"transport": "pty", "command": "rm -f /workspace/tmp.txt"},
            )

        assert result.success is True
        mock_pty.assert_called_once()
        mock_comm.assert_not_called()
        mock_direct.assert_not_called()
