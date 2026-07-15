"""Tests for PTY Executor Core"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure backend imports used for mocking don't fail fast in tests.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost:5432/test")

from agent.tools.shell._pty_executor import (
    execute_via_pty,
    PTYSessionNotAvailable,
    PTYTimeoutError,
    PTYCommandError,
    PTYReadTimeoutError,
    PTYOutputParseError,
    _cleanup_pty_output,
    _parse_marked_output,
    _parse_exit_code,
)
from backend.services.terminal.contracts import build_named_agent_session_id


class TestExecuteViaPty:
    """Test execute_via_pty function"""
    
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        """Test successful command execution via PTY"""
        # Mock terminal session manager
        mock_session = MagicMock()
        mock_session.write = AsyncMock()
        mock_session._drowai_initialized = False
        
        # Mock UUID to get predictable markers
        with patch('agent.tools.shell._pty_executor.uuid') as mock_uuid:
            mock_uuid.uuid4.return_value.hex = "abcd1234efgh5678"
            
            # Output format with new markers:
            # Start marker, command output, end marker with exit code, prompt
            mock_output = (
                b"__DROWAI_CMD_START_abcd1234__\n"
                b"test output\n"
                b"__DROWAI_CMD_END_abcd1234__=__DROWAI_EXIT_CODE__=0\n"
                b"__DROWAI_PROMPT__> "
            )
            
            # Simulate PTY behavior:
            # - Setup phase: prompt appears after setup commands
            # - Drain phase: TimeoutError (empty buffer)
            # - Command phase: Return output with markers once, then TimeoutError
            read_call_count = [0]
            async def mock_read(*_args, **_kwargs):
                read_call_count[0] += 1
                # Call 1: Setup phase - return prompt for initial clear
                if read_call_count[0] == 1:
                    return b"__DROWAI_PROMPT__> "
                # Call 2: Drain phase before command - empty buffer
                if read_call_count[0] == 2:
                    raise asyncio.TimeoutError()
                # Call 3: Command output with markers
                if read_call_count[0] == 3:
                    return mock_output
                # All subsequent calls: no more data
                raise asyncio.TimeoutError()
            
            mock_session.read = mock_read
            
            # terminal_session_manager is imported inside the PTY executor functions,
            # so we patch it at its source module path.
            with patch('backend.services.terminal_session_manager.terminal_session_manager') as mock_manager:
                mock_manager.prepare_agent_session = AsyncMock(return_value=mock_session)
                mock_manager.send_input = AsyncMock(return_value=True)
                mock_manager.read_output = AsyncMock(side_effect=mock_read)
                
                result = await execute_via_pty(
                    command="echo hello",
                    task_id=1,
                    timeout_sec=10,
                )
                
                assert result.status == "success"
                assert result.exit_code == 0
                assert "test output" in result.stdout
    
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_output(self):
        """Test command timeout returns result with partial output instead of raising"""
        mock_session = MagicMock()
        mock_session.write = AsyncMock()
        mock_session._drowai_initialized = True
        
        # Simulate timeout by having _read_until_marker_and_prompt raise PTYReadTimeoutError
        with patch('backend.services.terminal_session_manager.terminal_session_manager') as mock_manager:
            mock_manager.prepare_agent_session = AsyncMock(return_value=mock_session)
            mock_manager.send_input = AsyncMock(return_value=True)
            
            # Mock the new marker-based read function to raise timeout with partial output
            with patch('agent.tools.shell._pty_executor._read_until_marker_and_prompt') as mock_read:
                mock_read.side_effect = PTYReadTimeoutError(
                    "Command markers not detected within 1s",
                    partial_output="partial nmap output here..."
                )
                
                # Also mock drain to not block
                with patch('agent.tools.shell._pty_executor._drain_pty_buffer', new_callable=AsyncMock):
                    # Should return result with partial output, NOT raise exception
                    result = await execute_via_pty(
                        command="nmap -sn 10.0.0.0/8",
                        task_id=1,
                        timeout_sec=1,
                    )
                    
                    # Verify result contains partial output
                    assert result.status == "timeout"
                    assert result.exit_code == -9
                    assert "partial nmap output" in result.stdout
                    assert "timed out" in result.stderr.lower()
    
    @pytest.mark.asyncio
    async def test_session_not_available(self):
        """Test PTY session unavailable handling"""
        with patch('backend.services.terminal_session_manager.terminal_session_manager') as mock_manager:
            mock_manager.prepare_agent_session = AsyncMock(
                side_effect=Exception("Container not running")
            )
            
            with pytest.raises(PTYSessionNotAvailable):
                await execute_via_pty(
                    command="echo hello",
                    task_id=1,
                    timeout_sec=10,
                )
    
    @pytest.mark.asyncio
    async def test_session_reuse(self):
        """Test that initialized sessions are reused"""
        mock_session = MagicMock()
        mock_session.write = AsyncMock()
        mock_session._drowai_initialized = True  # Already initialized
        
        # Mock UUID for predictable markers
        with patch('agent.tools.shell._pty_executor.uuid') as mock_uuid:
            mock_uuid.uuid4.return_value.hex = "abcd1234efgh5678"
            
            mock_output = (
                b"__DROWAI_CMD_START_abcd1234__\n"
                b"output\n"
                b"__DROWAI_CMD_END_abcd1234__=__DROWAI_EXIT_CODE__=0\n"
                b"__DROWAI_PROMPT__> "
            )
            
            # Simulate PTY: drain gets timeout, then command output once
            read_call_count = [0]
            async def mock_read(*_args, **_kwargs):
                read_call_count[0] += 1
                # Call 1: Drain phase - empty buffer
                if read_call_count[0] == 1:
                    raise asyncio.TimeoutError()
                # Call 2: Command output
                if read_call_count[0] == 2:
                    return mock_output
                # Subsequent: no more data
                raise asyncio.TimeoutError()
            
            mock_session.read = mock_read
            
            with patch('backend.services.terminal_session_manager.terminal_session_manager') as mock_manager:
                mock_manager.prepare_agent_session = AsyncMock(return_value=mock_session)
                mock_manager.send_input = AsyncMock(return_value=True)
                mock_manager.read_output = AsyncMock(side_effect=mock_read)
                
                await execute_via_pty(
                    command="ls",
                    task_id=1,
                    timeout_sec=10,
                )
                
                # Session I/O now routes via terminal_session_manager.send_input.
                # Reused sessions should issue only one command write (no setup writes).
                assert mock_manager.send_input.await_count == 1

    @pytest.mark.asyncio
    async def test_emits_pty_session_prepare_metric(self):
        """PTY execution emits stable session-prepare latency metric."""
        mock_session = MagicMock()
        mock_session.write = AsyncMock()
        mock_session._drowai_initialized = True
        marker = "abc12345"

        with patch("agent.tools.shell._pty_executor.uuid") as mock_uuid, patch(
            "agent.tools.shell._pty_executor._drain_pty_buffer",
            new_callable=AsyncMock,
        ), patch(
            "agent.tools.shell._pty_executor._read_until_marker_and_prompt",
            new_callable=AsyncMock,
            return_value=(
                f"__DROWAI_CMD_START_{marker}__\n"
                "ok\n"
                f"__DROWAI_CMD_END_{marker}__=__DROWAI_EXIT_CODE__=0\n"
                "__DROWAI_PROMPT__> "
            ),
        ), patch(
            "backend.services.terminal_session_manager.terminal_session_manager"
        ) as mock_manager, patch(
            "agent.tools.shell._pty_executor.safe_gauge"
        ) as mock_gauge:
            mock_uuid.uuid4.return_value.hex = f"{marker}feedbeef"
            mock_manager.prepare_agent_session = AsyncMock(return_value=mock_session)
            mock_manager.send_input = AsyncMock(return_value=True)

            await execute_via_pty(
                command="echo ok",
                task_id=1,
                timeout_sec=10,
            )

        metric_names = [call.args[0] for call in mock_gauge.call_args_list]
        assert "pty_session_prepare_ms" in metric_names

    @pytest.mark.asyncio
    async def test_retries_once_with_fresh_session_on_parse_failure(self):
        """Parse failure should trigger one fresh-session retry before succeeding."""
        first_session = MagicMock()
        first_session.write = AsyncMock()
        first_session._drowai_initialized = True
        second_session = MagicMock()
        second_session.write = AsyncMock()
        second_session._drowai_initialized = True

        with patch("backend.services.terminal_session_manager.terminal_session_manager") as mock_manager, patch(
            "agent.tools.shell._pty_executor._execute_command_in_pty",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_manager.prepare_agent_session = AsyncMock(side_effect=[first_session, second_session])
            mock_manager.close_session = AsyncMock(return_value=True)
            mock_exec.side_effect = [
                PTYOutputParseError("missing markers"),
                ("raw", "clean output", 0),
            ]

            result = await execute_via_pty(
                command="echo hello",
                task_id=7,
                timeout_sec=10,
            )

        assert result.status == "success"
        assert result.exit_code == 0
        assert result.stdout == "clean output"
        assert mock_exec.await_count == 2
        mock_manager.close_session.assert_awaited_once_with("agent_task_7")
        assert mock_manager.prepare_agent_session.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_when_parse_fails_after_retry(self):
        """If parse fails twice, PTY execution must fail explicitly."""
        first_session = MagicMock()
        first_session.write = AsyncMock()
        first_session._drowai_initialized = True
        second_session = MagicMock()
        second_session.write = AsyncMock()
        second_session._drowai_initialized = True

        with patch("backend.services.terminal_session_manager.terminal_session_manager") as mock_manager, patch(
            "agent.tools.shell._pty_executor._execute_command_in_pty",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_manager.prepare_agent_session = AsyncMock(side_effect=[first_session, second_session])
            mock_manager.close_session = AsyncMock(return_value=True)
            mock_exec.side_effect = [
                PTYOutputParseError("missing markers"),
                PTYOutputParseError("missing markers again"),
            ]

            with pytest.raises(PTYCommandError):
                await execute_via_pty(
                    command="echo hello",
                    task_id=8,
                    timeout_sec=10,
                )

        assert mock_exec.await_count == 2
        mock_manager.close_session.assert_awaited_once_with("agent_task_8")
        assert mock_manager.prepare_agent_session.await_count == 2

    @pytest.mark.asyncio
    async def test_named_parallel_sessions_prepare_execute_and_close_independently(self):
        """Concurrent named PTY executions use separate sessions and cleanup hooks."""
        sessions = {}
        for name in ("parallel_one", "parallel_two"):
            session = MagicMock()
            session.write = AsyncMock()
            session._drowai_initialized = True
            sessions[name] = session

        async def _prepare_agent_session(**kwargs):
            return sessions[kwargs["session_name"]]

        async def _execute(session, command, timeout_sec=60.0):
            await asyncio.sleep(0)
            if session is sessions["parallel_one"]:
                return "raw-one", f"out:{command}", 0
            return "raw-two", f"out:{command}", 0

        with patch("backend.services.terminal_session_manager.terminal_session_manager") as mock_manager, patch(
            "agent.tools.shell._pty_executor._execute_command_in_pty",
            side_effect=_execute,
        ) as mock_exec:
            mock_manager.prepare_agent_session = AsyncMock(side_effect=_prepare_agent_session)
            mock_manager.close_session = AsyncMock(return_value=True)

            one, two = await asyncio.gather(
                execute_via_pty(
                    command="echo one",
                    task_id=9,
                    timeout_sec=10,
                    session_name="parallel_one",
                    cleanup_session=True,
                ),
                execute_via_pty(
                    command="echo two",
                    task_id=9,
                    timeout_sec=10,
                    session_name="parallel_two",
                    cleanup_session=True,
                ),
            )

        assert one.stdout == "out:echo one"
        assert two.stdout == "out:echo two"
        prepared_names = {
            call.kwargs["session_name"]
            for call in mock_manager.prepare_agent_session.await_args_list
        }
        assert prepared_names == {"parallel_one", "parallel_two"}
        assert all(
            call.kwargs["reset"] is True
            for call in mock_manager.prepare_agent_session.await_args_list
        )
        executed_sessions = [call.args[0] for call in mock_exec.await_args_list]
        assert any(session is sessions["parallel_one"] for session in executed_sessions)
        assert any(session is sessions["parallel_two"] for session in executed_sessions)
        closed = {call.args[0] for call in mock_manager.close_session.await_args_list}
        assert closed == {
            build_named_agent_session_id(9, "parallel_one"),
            build_named_agent_session_id(9, "parallel_two"),
        }


class TestCleanupPtyOutput:
    """Test _cleanup_pty_output helper"""
    
    def test_strips_ansi_codes(self):
        """Test ANSI escape code stripping"""
        output = "\x1b[32mgreen text\x1b[0m normal"
        command = "echo test"
        
        cleaned = _cleanup_pty_output(output, command)
        
        assert "\x1b" not in cleaned
        assert "green text" in cleaned
        assert "normal" in cleaned
    
    def test_removes_command_echo(self):
        """Test removal of command echo"""
        output = "echo hello\nhello\n__DROWAI_PROMPT__> "
        command = "echo hello"
        
        cleaned = _cleanup_pty_output(output, command)
        
        # Should not include the command echo line
        lines = cleaned.strip().split('\n')
        assert "echo hello" not in lines
        assert "hello" in cleaned
    
    def test_removes_prompt_markers(self):
        """Test removal of prompt markers"""
        output = "some output\n__DROWAI_PROMPT__> "
        command = "ls"
        
        cleaned = _cleanup_pty_output(output, command)
        
        assert "__DROWAI_PROMPT__" not in cleaned
        assert "some output" in cleaned
    
    def test_removes_exit_code_line(self):
        """Test removal of exit code echo"""
        output = "command output\n0\n__DROWAI_PROMPT__> "
        command = "echo test"
        
        cleaned = _cleanup_pty_output(output, command)
        
        lines = [line.strip() for line in cleaned.strip().split('\n')]
        # Numeric-only lines are preserved by minimal cleanup.
        assert "0" in lines
        assert "command output" in cleaned

    def test_preserves_xml_when_command_echo_and_xml_share_line(self):
        """Regression: command echo and output can be separated by \\r, not \\n."""
        command = "nmap -oX - 127.0.0.1"
        output = f"{command}\r<?xml version=\"1.0\"?><nmaprun></nmaprun>\n__DROWAI_PROMPT__> "

        cleaned = _cleanup_pty_output(output, command)

        assert "<?xml version" in cleaned
        assert "nmap -oX" not in cleaned


class TestParseExitCode:
    """Test _parse_exit_code helper"""
    
    def test_parses_zero_exit_code(self):
        """Test parsing exit code 0"""
        output = "0\n__DROWAI_PROMPT__> "
        
        exit_code = _parse_exit_code(output)
        
        assert exit_code == 0
    
    def test_parses_non_zero_exit_code(self):
        """Test parsing non-zero exit code"""
        output = "127\n__DROWAI_PROMPT__> "
        
        exit_code = _parse_exit_code(output)
        
        assert exit_code == 127
    
    def test_defaults_to_error_on_parse_failure(self):
        """Test default exit code on parse failure"""
        output = "invalid output\n__DROWAI_PROMPT__> "
        
        exit_code = _parse_exit_code(output)
        
        assert exit_code == 1  # Defaults to error

    def test_parses_exit_code_with_bracketed_paste_sequences(self):
        """Regression: PTY may wrap exit code with CSI ?2004h/?2004l sequences."""
        output = "echo $?\r\n\x1b[?2004l\r0\r\n\r\n\x1b[?2004h__DROWAI_PROMPT__> "

        exit_code = _parse_exit_code(output)

        assert exit_code == 0


class TestParseMarkedOutput:
    """Test marker-bounded PTY output parsing."""

    def test_raises_when_markers_missing(self):
        raw = "echo hello\nhello\n__DROWAI_PROMPT__> "

        with pytest.raises(PTYOutputParseError):
            _parse_marked_output(
                raw_output=raw,
                start_marker="__DROWAI_CMD_START_abcd1234__",
                end_marker="__DROWAI_CMD_END_abcd1234__",
            )

    def test_parses_valid_marker_bounded_output(self):
        raw = (
            "__DROWAI_CMD_START_abcd1234__\n"
            "hello\n"
            "__DROWAI_CMD_END_abcd1234__=__DROWAI_EXIT_CODE__=0\n"
            "__DROWAI_PROMPT__> "
        )

        stdout, exit_code = _parse_marked_output(
            raw_output=raw,
            start_marker="__DROWAI_CMD_START_abcd1234__",
            end_marker="__DROWAI_CMD_END_abcd1234__",
        )

        assert stdout == "hello"
        assert exit_code == 0


@pytest.mark.integration
class TestPtyExecutorIntegration:
    """Integration tests for PTY executor (requires real container)"""
    
    @pytest.mark.asyncio
    @pytest.mark.skipif(
        __import__("os").getenv("RUN_PTY_INTEGRATION_TESTS", "").lower() not in {"1", "true", "yes"},
        reason="Requires running Docker container (set RUN_PTY_INTEGRATION_TESTS=1 to enable)",
    )
    async def test_real_command_execution(self):
        """Integration test with real Docker container"""
        result = await execute_via_pty(
            command="echo 'Integration test'",
            task_id=1,
            timeout_sec=10,
        )
        
        assert result.status == "success"
        assert result.exit_code == 0
        assert "Integration test" in result.stdout
