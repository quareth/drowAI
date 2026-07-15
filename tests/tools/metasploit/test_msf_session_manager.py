"""Tests for the Metasploit Session Manager.

Tests cover:
- Named session creation and management
- Prompt detection with confidence scoring
- Output buffering and replay
- Session health checking
- Session recovery
- Metrics and observability"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.exploitation_tools.metasploit.msf_session_manager import (
    SessionHealth,
    PromptType,
    PromptDetectionResult,
    PromptDetector,
    OutputBuffer,
    MsfNamedSession,
    MsfSessionMetrics,
    MsfSessionManager,
    get_msf_session_manager,
)
from backend.services.terminal.contracts import build_named_agent_session_id


# =============================================================================
# Prompt Detection Tests
# =============================================================================


class TestPromptDetector:
    """Tests for PromptDetector class."""

    def test_detect_msf6_prompt(self):
        """Should detect basic msf6 prompt."""
        output = "Some output\nmsf6 > "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.MSF6
        assert result.confidence >= 0.9

    def test_detect_msf6_module_prompt(self):
        """Should detect msf6 prompt with module context."""
        output = "[*] Using exploit/windows/smb/ms17_010\nmsf6 exploit(windows/smb/ms17_010_eternalblue) > "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.MSF6_MODULE
        assert result.module_context == "windows/smb/ms17_010_eternalblue"
        assert result.confidence >= 0.9

    def test_detect_meterpreter_prompt(self):
        """Should detect meterpreter prompt."""
        output = "[+] Session 1 opened\nmeterpreter > "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.METERPRETER
        assert result.confidence >= 0.9

    def test_detect_shell_root_prompt(self):
        """Should detect root shell prompt."""
        output = "uid=0(root) gid=0(root)\nroot@target:~# "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.SHELL_ROOT

    def test_detect_shell_user_prompt(self):
        """Should detect user shell prompt."""
        output = "Welcome to Ubuntu\nuser@target:~$ "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.SHELL_USER

    def test_detect_mysql_prompt(self):
        """Should detect MySQL prompt."""
        output = "Reading table information\nmysql> "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.MYSQL

    def test_no_prompt_detected(self):
        """Should return not detected for output without prompt."""
        output = "Some random output without any prompt"
        result = PromptDetector.detect(output)

        assert result.detected is False
        assert result.prompt_type == PromptType.UNKNOWN

    def test_strip_ansi_codes(self):
        """Should detect prompt even with ANSI color codes."""
        # msf6 prompt with color codes
        output = "\x1b[0m\x1b[4mmsf6\x1b[0m > "
        result = PromptDetector.detect(output)

        assert result.detected is True
        assert result.prompt_type == PromptType.MSF6

    def test_empty_output(self):
        """Should handle empty output gracefully."""
        result = PromptDetector.detect("")
        assert result.detected is False

        result = PromptDetector.detect("   \n\n  ")
        assert result.detected is False

    def test_wait_timeout_for_prompt(self):
        """Should return appropriate timeout based on prompt type."""
        # Meterpreter needs longer timeout
        meterpreter_timeout = PromptDetector.wait_timeout_for_prompt(
            PromptType.METERPRETER
        )
        msf6_timeout = PromptDetector.wait_timeout_for_prompt(PromptType.MSF6)

        assert meterpreter_timeout > msf6_timeout


# =============================================================================
# Output Buffer Tests
# =============================================================================


class TestOutputBuffer:
    """Tests for OutputBuffer class."""

    def test_append_and_get_all(self):
        """Should append data and retrieve all."""
        buffer = OutputBuffer(max_bytes=1024)
        buffer.append(b"Hello ")
        buffer.append(b"World")

        assert buffer.get_all() == b"Hello World"
        assert buffer.size == 11

    def test_max_bytes_eviction(self):
        """Should evict old data when max_bytes exceeded."""
        buffer = OutputBuffer(max_bytes=20)
        buffer.append(b"0123456789")  # 10 bytes
        buffer.append(b"ABCDEFGHIJ")  # 10 bytes
        buffer.append(b"XYZ")  # 3 bytes, should evict first chunk

        assert buffer.size <= 20
        # First chunk should be evicted
        all_data = buffer.get_all()
        assert b"0123456789" not in all_data

    def test_get_recent(self):
        """Should get most recent data up to limit."""
        buffer = OutputBuffer(max_bytes=1024)
        buffer.append(b"First chunk ")
        buffer.append(b"Second chunk ")
        buffer.append(b"Third chunk")

        recent = buffer.get_recent(max_bytes=20)
        # Should get last chunk(s) up to 20 bytes
        assert len(recent) <= 20
        assert b"Third" in recent

    def test_get_since_timestamp(self):
        """Should get data since a specific timestamp."""
        buffer = OutputBuffer(max_bytes=1024)
        buffer.append(b"Old data")

        cutoff_time = time.time()
        time.sleep(0.01)  # Small delay

        buffer.append(b"New data")

        since_data = buffer.get_since(cutoff_time)
        assert b"New data" in since_data
        # Old data might or might not be included depending on timing

    def test_clear(self):
        """Should clear all data."""
        buffer = OutputBuffer(max_bytes=1024)
        buffer.append(b"Some data")

        buffer.clear()

        assert buffer.size == 0
        assert buffer.get_all() == b""

    def test_get_chunks_with_timestamps(self):
        """Should return chunks with timestamps for replay."""
        buffer = OutputBuffer(max_bytes=1024)
        buffer.append(b"Chunk 1")
        buffer.append(b"Chunk 2")

        chunks = buffer.get_chunks()

        assert len(chunks) == 2
        for timestamp, data in chunks:
            assert isinstance(timestamp, float)
            assert isinstance(data, bytes)


# =============================================================================
# Named Session Tests
# =============================================================================


class TestMsfNamedSession:
    """Tests for MsfNamedSession dataclass."""

    def test_creation(self):
        """Should create session with defaults."""
        session = MsfNamedSession(
            task_id=123,
            session_name="handler",
            session_id="agent_task_123_handler",
        )

        assert session.task_id == 123
        assert session.session_name == "handler"
        assert session.health == SessionHealth.UNKNOWN
        assert session.current_prompt is None
        assert len(session.command_history) == 0

    def test_update_activity(self):
        """Should update last activity timestamp."""
        session = MsfNamedSession(
            task_id=123,
            session_name="main",
            session_id="agent_task_123_main",
        )
        old_activity = session.last_activity

        time.sleep(0.01)
        session.update_activity()

        assert session.last_activity > old_activity

    def test_record_command(self):
        """Should record commands in history."""
        session = MsfNamedSession(
            task_id=123,
            session_name="main",
            session_id="agent_task_123_main",
        )

        session.record_command("use exploit/multi/handler", "Module loaded", True)
        session.record_command("set LHOST 192.168.1.1", "LHOST => 192.168.1.1", True)

        assert len(session.command_history) == 2
        assert session.command_history[0]["command"] == "use exploit/multi/handler"
        assert session.command_history[1]["success"] is True

    def test_command_history_limit(self):
        """Should limit command history to max_history."""
        session = MsfNamedSession(
            task_id=123,
            session_name="main",
            session_id="agent_task_123_main",
            max_history=5,
        )

        for i in range(10):
            session.record_command(f"command_{i}", "", True)

        assert len(session.command_history) == 5
        # Should keep most recent
        assert session.command_history[-1]["command"] == "command_9"

    def test_to_dict(self):
        """Should serialize to dictionary."""
        session = MsfNamedSession(
            task_id=123,
            session_name="handler",
            session_id="agent_task_123_handler",
            health=SessionHealth.HEALTHY,
            current_prompt=PromptType.MSF6,
        )

        data = session.to_dict()

        assert data["task_id"] == 123
        assert data["session_name"] == "handler"
        assert data["health"] == "healthy"
        assert data["current_prompt"] == "msf6"


# =============================================================================
# Session Metrics Tests
# =============================================================================


class TestMsfSessionMetrics:
    """Tests for MsfSessionMetrics class."""

    def test_increment(self):
        """Should increment metric counters."""
        metrics = MsfSessionMetrics()

        metrics.increment("sessions_created")
        metrics.increment("sessions_created")
        metrics.increment("commands_executed", 5)

        assert metrics.sessions_created == 2
        assert metrics.commands_executed == 5

    def test_to_dict(self):
        """Should convert to dictionary."""
        metrics = MsfSessionMetrics()
        metrics.sessions_created = 10
        metrics.commands_executed = 50

        data = metrics.to_dict()

        assert data["sessions_created"] == 10
        assert data["commands_executed"] == 50
        assert "sessions_recovered" in data


# =============================================================================
# Session Manager Tests
# =============================================================================


class TestMsfSessionManager:
    """Tests for MsfSessionManager class."""

    @pytest.fixture
    def manager(self):
        """Create a session manager for testing."""
        return MsfSessionManager()

    @pytest.fixture
    def mock_terminal_manager(self):
        """Create mock terminal session manager."""
        mock_manager = MagicMock()
        mock_manager.sessions = {}
        return mock_manager

    def test_make_session_id(self, manager):
        """Should generate correct session IDs."""
        session_id = manager._make_session_id(123, "handler")
        assert session_id == "agent_task_123_handler"
        assert session_id == build_named_agent_session_id(123, "handler")

        # Should sanitize special characters
        session_id = manager._make_session_id(456, "my-session.name")
        assert session_id == "agent_task_456_my_session_name"
        assert session_id == build_named_agent_session_id(456, "my-session.name")

    def test_metrics_property(self, manager):
        """Should expose metrics."""
        assert isinstance(manager.metrics, MsfSessionMetrics)

    def test_get_session_info_not_found(self, manager):
        """Should return None for non-existent session."""
        info = manager.get_session_info(999, "nonexistent")
        assert info is None

    def test_get_all_sessions_empty(self, manager):
        """Should return empty list when no sessions."""
        sessions = manager.get_all_sessions()
        assert sessions == []

    def test_get_output_replay_no_session(self, manager):
        """Should return empty bytes for non-existent session."""
        replay = manager.get_output_replay(999, "nonexistent")
        assert replay == b""

    @pytest.mark.asyncio
    async def test_close_session_not_found(self, manager):
        """Should handle closing non-existent session gracefully."""
        # Mock terminal manager
        with patch.object(manager, "_get_terminal_manager") as mock_get:
            mock_tm = MagicMock()
            mock_tm.close_session = AsyncMock(return_value=False)
            mock_get.return_value = mock_tm

            result = await manager.close_session(999, "nonexistent")
            # Should not raise, just return result from terminal manager
            assert result is False

    @pytest.mark.asyncio
    async def test_get_or_create_session_reuses_existing_terminal_session_by_canonical_id(
        self,
        manager,
    ):
        """Existing named sessions should still be found via terminal_manager.sessions."""
        existing_session = MsfNamedSession(
            task_id=123,
            session_name="handler",
            session_id="agent_task_123_handler",
        )
        manager._sessions[123] = {"handler": existing_session}

        terminal_session = MagicMock()
        terminal_session.is_active = True
        mock_terminal_manager = MagicMock()
        mock_terminal_manager.sessions = {
            existing_session.session_id: terminal_session,
        }

        with patch.object(
            manager,
            "_get_terminal_manager",
            return_value=mock_terminal_manager,
        ):
            reused = await manager.get_or_create_session(123, "handler")

        assert reused is existing_session

    @pytest.mark.asyncio
    async def test_create_terminal_session_uses_internal_context_user_identity(self, manager):
        """Named session creation should use resolver-derived user identity (never user_id=0)."""
        session_id = "agent_task_123_handler"
        terminal_manager = MagicMock()
        terminal_manager.sessions = {}
        terminal_session = SimpleNamespace(is_active=True, reader_task=None)

        with (
            patch.object(manager, "_get_terminal_manager", return_value=terminal_manager),
            patch.object(
                manager,
                "_resolve_internal_runtime_context",
                return_value=SimpleNamespace(
                    tenant_id=1,
                    task_id=123,
                    workspace_id="task-123",
                    runtime_placement_mode="local",
                    actor_type=SimpleNamespace(value="agent"),
                    actor_id="agent_session:handler",
                    user_id=321,
                    runner_id=None,
                    execution_site_id=None,
                ),
            ) as mock_resolve_context,
            patch(
                "agent.tools.exploitation_tools.metasploit.msf_session_manager.RuntimeOperationService",
                return_value=SimpleNamespace(
                    run_for_context=AsyncMock(
                        side_effect=[
                            SimpleNamespace(ok=True, metadata={"delegate_result": "running"}),
                            SimpleNamespace(
                                ok=True,
                                metadata={
                                    "delegate_result": {
                                        "exec_id": "exec-123",
                                        "socket": object(),
                                        "container_name": "task-123",
                                    }
                                },
                                error_message=None,
                            ),
                        ]
                    )
                ),
            ),
            patch(
                "backend.services.terminal_session_manager.TerminalSession",
                return_value=terminal_session,
            ) as mock_terminal_session_ctor,
        ):
            created = await manager._create_terminal_session(
                task_id=123,
                session_id=session_id,
                session_name="handler",
                cols=120,
                rows=30,
            )

        assert created is terminal_session
        mock_resolve_context.assert_called_once_with(task_id=123, session_name="handler")
        constructor_kwargs = mock_terminal_session_ctor.call_args.kwargs
        assert constructor_kwargs["user_id"] == 321
        assert constructor_kwargs["user_id"] != 0
        assert terminal_manager.sessions[session_id] is terminal_session


class TestMsfSessionManagerIntegration:
    """Integration tests for MsfSessionManager (synchronous only).
    
    Note: Async integration tests that require real terminal sessions
    are skipped - they need actual PTY infrastructure to run properly.
    """

    def test_metrics_tracking_directly(self):
        """Test that metrics can be tracked without async operations."""
        manager = MsfSessionManager()
        
        # Directly increment metrics
        manager._metrics.increment("commands_executed")
        manager._metrics.increment("sessions_created")
        
        assert manager.metrics.commands_executed == 1
        assert manager.metrics.sessions_created == 1

    def test_session_creation_tracking(self):
        """Test session objects can be created and tracked."""
        manager = MsfSessionManager()
        
        # Create a named session object directly
        session = MsfNamedSession(
            task_id=123,
            session_name="test_handler",
            session_id="agent_task_123_test_handler",
        )
        
        # Track it in manager
        manager._sessions[123] = {"test_handler": session}
        
        # Verify retrieval (returns list of dicts from to_dict())
        all_sessions = manager.get_all_sessions(123)
        assert len(all_sessions) == 1
        assert all_sessions[0]["session_name"] == "test_handler"

    def test_output_buffer_integration(self):
        """Test output buffer can store and retrieve data."""
        manager = MsfSessionManager()
        
        # Create session with buffer
        session = MsfNamedSession(
            task_id=456,
            session_name="main",
            session_id="agent_task_456_main",
        )
        manager._sessions[456] = {"main": session}
        
        # Add output to buffer
        session.output_buffer.append(b"test output line 1\n")
        session.output_buffer.append(b"test output line 2\n")
        
        # Get replay
        replay = manager.get_output_replay(456, "main")
        assert b"test output line 1" in replay
        assert b"test output line 2" in replay


# =============================================================================
# Module-level Function Tests
# =============================================================================


class TestGetMsfSessionManager:
    """Tests for get_msf_session_manager function."""

    def test_returns_instance(self):
        """Should return MsfSessionManager instance."""
        manager = get_msf_session_manager()
        assert isinstance(manager, MsfSessionManager)

    def test_returns_same_instance(self):
        """Should return same instance (singleton)."""
        manager1 = get_msf_session_manager()
        manager2 = get_msf_session_manager()
        assert manager1 is manager2


# =============================================================================
# Session Health Tests
# =============================================================================


class TestSessionHealth:
    """Tests for SessionHealth enum."""

    def test_health_values(self):
        """Should have expected health status values."""
        assert SessionHealth.HEALTHY.value == "healthy"
        assert SessionHealth.DEGRADED.value == "degraded"
        assert SessionHealth.UNRESPONSIVE.value == "unresponsive"
        assert SessionHealth.DEAD.value == "dead"
        assert SessionHealth.UNKNOWN.value == "unknown"


class TestPromptType:
    """Tests for PromptType enum."""

    def test_prompt_type_values(self):
        """Should have expected prompt type values."""
        assert PromptType.MSF6.value == "msf6"
        assert PromptType.MSF6_MODULE.value == "msf6_module"
        assert PromptType.METERPRETER.value == "meterpreter"
        assert PromptType.SHELL_ROOT.value == "shell_root"
        assert PromptType.SHELL_USER.value == "shell_user"
