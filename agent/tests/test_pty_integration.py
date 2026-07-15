"""Integration tests for PTY execution.

 of PTY Execution Implementation Plan.
Tests PTY execution flow with mocked PTY sessions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys
import tempfile
import re
import shlex
import subprocess
import time

# Mock DATABASE_URL before any imports that might need it
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost:5432/test")


class MockShellCommandResult:
    """Mock shell command result for testing."""
    
    def __init__(self, stdout="", stderr="", exit_code=0, status="success"):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.status = status


def create_mock_config(task_id=1, workspace_path="/workspace/1"):
    """Create a mock config that satisfies EnhancedCommandExecutor requirements."""
    config = MagicMock()
    config.task_id = task_id
    config.workspace_path = workspace_path
    config.openai_api_key = "test-key-mock"  # Needed for EnhancedActionPlanner
    config.model_name = "gpt-4"
    config.individual_tool_timeout = 60
    config.tool_execution_timeout = 60
    return config


class TestPTYToolExecution:
    """Test PTY execution with real tools using mocked PTY session."""
    
    @pytest.mark.asyncio
    async def test_nmap_via_pty_uses_build_command(self):
        """Test nmap execution via PTY uses tool.build_command()."""
        # Mock the OpenAI client to avoid actual API calls
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            # Mock PTY executor
            mock_result = MockShellCommandResult(
                stdout='<?xml version="1.0"?><nmaprun><runstats><hosts up="1" total="1"/></runstats></nmaprun>',
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                # Execute
                result = await executor._execute_via_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1", "ports": "80,443"}
                )
                
                # Verify PTY was called
                assert mock_pty.called
                call_args = mock_pty.call_args
                command = call_args.kwargs.get("command") or call_args[1].get("command", call_args[0][0] if call_args[0] else "")
                
                # Verify command was built by tool (contains nmap-specific flags)
                assert "nmap" in command
                assert "192.168.1.1" in command
                assert "-p" in command or "80,443" in command
                
                # Verify result
                assert result.success
                assert result.exit_code == 0
    
    @pytest.mark.asyncio
    async def test_nmap_via_pty_parses_output(self):
        """Test nmap PTY execution parses XML output correctly."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            xml_output = """<?xml version="1.0"?>
<nmaprun>
    <host>
        <address addr="192.168.1.1" addrtype="ipv4"/>
        <ports>
            <port portid="80" protocol="tcp">
                <state state="open"/>
                <service name="http"/>
            </port>
        </ports>
    </host>
    <runstats>
        <hosts up="1" down="0" total="1"/>
    </runstats>
</nmaprun>"""
            
            mock_result = MockShellCommandResult(
                stdout=xml_output,
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                result = await executor._execute_via_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1", "ports": "80"}
                )

                # Verify metadata was parsed
                assert hasattr(result, "metadata")
                metadata = getattr(result, "metadata", {})
                assert metadata.get("hosts_up") == 1
                assert metadata.get("hosts_total") == 1
                assert len(metadata.get("open_ports", [])) == 1

    @pytest.mark.asyncio
    async def test_pty_artifacts_written_under_task_workspace(self):
        """PTY should write artifacts under the task workspace (same as file-comm)."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()

            from agent.executor import EnhancedCommandExecutor

            xml_output = """<?xml version="1.0"?>
<nmaprun>
  <runstats><hosts up="1" down="0" total="1"/></runstats>
</nmaprun>"""

            mock_result = MockShellCommandResult(
                stdout=xml_output,
                stderr="",
                exit_code=0,
                status="success"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                # Force host workspace resolution to pick this real directory.
                config = create_mock_config(task_id=9999, workspace_path=tmpdir)
                executor = EnhancedCommandExecutor(config=config)

                with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                    mock_pty.return_value = mock_result

                    result = await executor._execute_via_pty(
                        "information_gathering.network_discovery.nmap",
                        {"target": "127.0.0.1", "ports": "80"}
                    )

                    artifacts = getattr(result, "artifacts", [])
                    assert artifacts, "Expected artifacts to be created"
                    # Tools return relative paths like artifacts/nmap_<ts>.xml
                    artifact_rel = artifacts[0]
                    assert artifact_rel.startswith("artifacts/")
                    artifact_abs = os.path.join(tmpdir, artifact_rel.replace("/", os.sep))
                    assert os.path.exists(artifact_abs)
    
    @pytest.mark.asyncio
    async def test_masscan_via_pty(self):
        """Test masscan execution via PTY."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            json_output = '{"ip": "192.168.1.1", "ports": [{"port": 80, "proto": "tcp", "status": "open"}]}'
            
            mock_result = MockShellCommandResult(
                stdout=json_output,
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                result = await executor._execute_via_pty(
                    "information_gathering.network_discovery.masscan",
                    {"target": "192.168.1.0/24", "ports": "80"}
                )
                
                # Verify command contains masscan
                call_args = mock_pty.call_args
                command = call_args.kwargs.get("command") or call_args[1].get("command", call_args[0][0] if call_args[0] else "")
                assert "masscan" in command
                
                assert result.success
    
    @pytest.mark.asyncio
    async def test_shell_exec_via_pty(self):
        """Test shell.exec uses manual command mapping (not tool.build_command)."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            mock_result = MockShellCommandResult(
                stdout="test output",
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                result = await executor._execute_via_pty(
                    "shell.exec",
                    {"command": "echo hello"}
                )
                
                # Verify exact command was passed (no tool.build_command wrapping)
                call_args = mock_pty.call_args
                command = call_args.kwargs.get("command") or call_args[1].get("command", call_args[0][0] if call_args[0] else "")
                assert "echo hello" in command
                
                assert result.success
    
    @pytest.mark.asyncio
    async def test_fs_read_file_via_pty(self):
        """Test fs.read_file uses manual command mapping."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            mock_result = MockShellCommandResult(
                stdout="file contents",
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                result = await executor._execute_via_pty(
                    "filesystem.read_file",
                    {"path": "test.txt"}
                )
                
                # Verify bounded full-read command was generated
                call_args = mock_pty.call_args
                command = call_args.kwargs.get("command") or call_args[1].get("command", call_args[0][0] if call_args[0] else "")
                assert "head -c" in command
                
                assert result.success

    @pytest.mark.asyncio
    async def test_fs_read_head_via_pty_matches_shell(self):
        """Ensure PTY head mode matches direct shell output with line numbers."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()

            from agent.executor import EnhancedCommandExecutor

            with tempfile.TemporaryDirectory() as tmpdir:
                os.makedirs(os.path.join(tmpdir, "logs"), exist_ok=True)
                file_path = os.path.join(tmpdir, "logs", "sample.txt")
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write("one\ntwo\nthree\nfour\nfive\n")

                async def fake_execute(command: str, task_id: int, timeout: int | None = None, **kwargs):
                    proc = subprocess.run(command, shell=True, capture_output=True, text=True)
                    return MockShellCommandResult(
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        exit_code=proc.returncode,
                        status="success" if proc.returncode == 0 else "error",
                    )

                with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                    mock_pty.side_effect = fake_execute

                    config = create_mock_config(task_id=4242, workspace_path=tmpdir)
                    executor = EnhancedCommandExecutor(config=config)
                    params = {
                        "path": "logs/sample.txt",
                        "read_mode": "head",
                        "num_lines": 3,
                        "include_line_numbers": True,
                    }

                    result = await executor._execute_via_pty("filesystem.read_file", params)
                    expected_proc = subprocess.run(
                        executor._tool_to_shell_command("filesystem.read_file", params),
                        shell=True,
                        capture_output=True,
                        text=True,
                    )

                    assert result.stdout.strip() == expected_proc.stdout.strip()
                    assert result.exit_code == expected_proc.returncode

    @pytest.mark.asyncio
    async def test_fs_read_tail_large_file_via_pty(self):
        """Tail mode over large files should complete quickly via PTY mapping."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()

            from agent.executor import EnhancedCommandExecutor

            with tempfile.TemporaryDirectory() as tmpdir:
                os.makedirs(os.path.join(tmpdir, "logs"), exist_ok=True)
                file_path = os.path.join(tmpdir, "logs", "large.txt")
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(f"line-{i}" for i in range(1, 5001)))

                async def fake_execute(command: str, task_id: int, timeout: int | None = None, **kwargs):
                    if "tail -n" in command:
                        try:
                            tail_segment = command.split("tail -n", 1)[1].strip()
                            tail_tokens = shlex.split(tail_segment, posix=os.name != "nt")
                            if len(tail_tokens) >= 2:
                                num_lines = int(tail_tokens[0])
                                target = tail_tokens[1].strip("\"'")
                                candidate_targets = [target]
                                if target.startswith("/workspace/"):
                                    candidate_targets.append(
                                        os.path.join(tmpdir, os.path.relpath(target, "/workspace")).replace("/", os.sep)
                                    )
                                if target.startswith("workspace/"):
                                    candidate_targets.append(
                                        os.path.join(tmpdir, target.replace("workspace/", ""))
                                    )

                                for candidate in candidate_targets:
                                    if os.path.exists(candidate):
                                        with open(candidate, "r", encoding="utf-8") as handle:
                                            output = "".join(handle.readlines()[-num_lines:])
                                        return MockShellCommandResult(
                                            stdout=output,
                                            stderr="",
                                            exit_code=0,
                                            status="success",
                                        )
                        except Exception:
                            pass

                    proc = subprocess.run(command, shell=True, capture_output=True, text=True)
                    return MockShellCommandResult(
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        exit_code=proc.returncode,
                        status="success" if proc.returncode == 0 else "error",
                    )

                with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                    mock_pty.side_effect = fake_execute

                    config = create_mock_config(task_id=5252, workspace_path=tmpdir)
                    executor = EnhancedCommandExecutor(config=config)
                    params = {"path": "logs/large.txt", "read_mode": "tail", "num_lines": 50}

                    start = time.time()
                    result = await executor._execute_via_pty("filesystem.read_file", params)
                    duration = time.time() - start

                    assert result.success
                    assert duration < 2.0

    @pytest.mark.asyncio
    async def test_pty_read_file_head_mode(self):
        """Test PTY execution of fs.read_file with head mode."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="line1\nline2\n", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.txt", "read_mode": "head", "num_lines": 2}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "head -n 2" in command

    @pytest.mark.asyncio
    async def test_pty_read_file_tail_mode(self):
        """Test PTY execution of fs.read_file with tail mode."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="last\n", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.txt", "read_mode": "tail", "num_lines": 5}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "tail -n 5" in command

    @pytest.mark.asyncio
    async def test_pty_read_file_range_mode(self):
        """Test PTY execution of fs.read_file with range mode."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="range\n", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.txt", "read_mode": "range", "start_line": 3, "num_lines": 2}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "sed -n '3,4p'" in command

    @pytest.mark.asyncio
    async def test_pty_read_file_grep_mode(self):
        """Test PTY execution of fs.read_file with grep mode."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="match\n", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.txt", "read_mode": "grep", "grep_pattern": "match", "case_sensitive": False}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "grep -n" in command
                assert " -i " in command

    @pytest.mark.asyncio
    async def test_pty_read_file_byte_range(self):
        """Test PTY execution of fs.read_file with byte range."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="bytes", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.bin", "read_mode": "byte", "start_byte": 10, "max_bytes": 5, "encoding": "utf-8"}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "dd if=" in command or "head -c" in command

    @pytest.mark.asyncio
    async def test_pty_read_file_with_line_numbers(self):
        """Test PTY execution with include_line_numbers=true."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.executor import EnhancedCommandExecutor

            mock_result = MockShellCommandResult(stdout="1| first", stderr="", exit_code=0, status="success")
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                executor = EnhancedCommandExecutor(config=create_mock_config())
                params = {"path": "logs/sample.txt", "read_mode": "head", "num_lines": 1, "include_line_numbers": True}
                await executor._execute_via_pty("filesystem.read_file", params)
                command = mock_pty.call_args.kwargs.get("command")
                assert "awk" in command


class TestPTYRouting:
    """Test PTY routing logic in executor."""
    
    def test_tool_supports_pty_for_nmap(self):
        """Test _tool_supports_pty returns True for nmap."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("information_gathering.network_discovery.nmap") is True
    
    def test_tool_supports_pty_for_masscan(self):
        """Test _tool_supports_pty returns True for masscan."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("information_gathering.network_discovery.masscan") is True
    
    def test_tool_supports_pty_for_amass(self):
        """Test _tool_supports_pty returns True for amass."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("information_gathering.dns.amass") is True
    
    def test_tool_supports_pty_for_theharvester(self):
        """Test _tool_supports_pty returns True for theharvester."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("information_gathering.osint.theharvester") is True
    
    def test_tool_supports_pty_for_shell(self):
        """Test _tool_supports_pty returns True for shell tools."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("shell.exec") is True
            assert executor._tool_supports_pty("shell.script") is True
    
    def test_tool_supports_pty_for_fs(self):
        """Test _tool_supports_pty returns True for filesystem tools."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            config = create_mock_config()
            executor = EnhancedCommandExecutor(config=config)
            
            assert executor._tool_supports_pty("filesystem.read_file") is True
            assert executor._tool_supports_pty("filesystem.write_file") is True
            assert executor._tool_supports_pty("filesystem.list_dir") is True
    
    def test_should_use_pty_when_enabled(self):
        """Test _should_use_pty returns True when enabled and supported."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            with patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"}):
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                # Reset cache
                executor._pty_enabled_cached = None
                
                assert executor._should_use_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1"}
                ) is True
    
    def test_should_use_pty_when_disabled(self):
        """Test _should_use_pty returns False when disabled."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            with patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "false"}):
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                # Reset cache
                executor._pty_enabled_cached = None
                
                assert executor._should_use_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1"}
                ) is False
    
    def test_should_use_pty_explicit_opt_out(self):
        """Test _should_use_pty respects explicit opt-out."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            with patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"}):
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                # Reset cache
                executor._pty_enabled_cached = None
                
                # Explicit opt-out via transport="file-comm" (canonical)
                executor._pty_enabled_cached = None
                assert executor._should_use_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1", "transport": "file-comm"}
                ) is False
                
                # Explicit opt-out via transport="file" (legacy alias)
                executor._pty_enabled_cached = None
                assert executor._should_use_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1", "transport": "file"}
                ) is False


class TestPTYFailure:
    """Test PTY failure behavior (no fallback - errors propagate)."""
    
    @pytest.mark.asyncio
    async def test_pty_failure_raises_exception(self):
        """Test that PTY failure raises exception (no silent fallback)."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            from agent.tools.shell._pty_executor import PTYSessionNotAvailable
            
            with patch.dict(os.environ, {"ENABLE_PTY_EXECUTION": "true"}):
                # Mock PTY to fail
                with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                    mock_pty.side_effect = PTYSessionNotAvailable("No PTY session")
                    
                    config = create_mock_config()
                    executor = EnhancedCommandExecutor(config=config)
                    executor._pty_enabled_cached = None
                    
                    # PTY failure should raise exception, not fall back
                    with pytest.raises(PTYSessionNotAvailable):
                        await executor._execute_single_tool_internal(
                            "information_gathering.network_discovery.nmap",
                            {"target": "192.168.1.1", "ports": "80"}
                        )


class TestPTYOutputParity:
    """Test that PTY execution produces same output as direct execution."""
    
    @pytest.mark.asyncio
    async def test_nmap_output_parity(self):
        """Test nmap PTY output matches direct execution format."""
        with patch('agent.reasoning.enhanced_planner.LLMClientFactory.get_client') as mock_openai:
            mock_openai.return_value = MagicMock()
            
            from agent.executor import EnhancedCommandExecutor
            
            # Expected metadata from nmap XML parsing
            xml_output = """<?xml version="1.0"?>
<nmaprun>
    <host>
        <address addr="192.168.1.1" addrtype="ipv4"/>
        <status state="up"/>
        <ports>
            <port portid="80" protocol="tcp">
                <state state="open"/>
                <service name="http"/>
            </port>
        </ports>
    </host>
    <runstats>
        <hosts up="1" down="0" total="1"/>
    </runstats>
</nmaprun>"""
            
            mock_result = MockShellCommandResult(
                stdout=xml_output,
                stderr="",
                exit_code=0,
                status="success"
            )
            
            with patch('agent.tools.shell._pty_executor.execute_via_pty', new_callable=AsyncMock) as mock_pty:
                mock_pty.return_value = mock_result
                
                config = create_mock_config()
                executor = EnhancedCommandExecutor(config=config)
                
                result = await executor._execute_via_pty(
                    "information_gathering.network_discovery.nmap",
                    {"target": "192.168.1.1", "ports": "80"}
                )

                # Verify result structure matches what run() would return
                assert result.success is True
                assert result.exit_code == 0
                assert result.stdout == xml_output
                assert result.stderr == ""
                
                # Verify metadata structure
                metadata = getattr(result, "metadata", {})
                assert "hosts_up" in metadata
                assert "hosts_total" in metadata
                assert "open_ports" in metadata
                assert "hosts" in metadata


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
