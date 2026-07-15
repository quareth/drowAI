"""
Tests for the Metasploit Session State Manager.

The session state manager tracks Metasploit framework state including
active sessions, background jobs, routes, and module context.
"""

from __future__ import annotations

import asyncio
import pytest
import tempfile
import os

from agent.tools.exploitation_tools.metasploit.session_state import (
    RouteInfo,
    MsfSessionState,
    AsyncSessionLock,
    SessionStateManager,
    get_session_state_manager,
)
from agent.tools.exploitation_tools.metasploit.output_parser import (
    SessionInfo,
    JobInfo,
    MsfParseResult,
)


class TestMsfSessionState:
    """Test MsfSessionState class."""

    @pytest.fixture
    def state(self):
        """Create empty session state."""
        return MsfSessionState(task_id="test_task")

    def test_add_session(self, state):
        """Test adding a session."""
        session = SessionInfo(id=1, type="meterpreter")
        state.add_session(session)

        assert 1 in state.sessions
        assert state.sessions[1].type == "meterpreter"

    def test_remove_session(self, state):
        """Test removing a session."""
        session = SessionInfo(id=1, type="shell")
        state.add_session(session)

        removed = state.remove_session(1)

        assert removed is not None
        assert removed.id == 1
        assert 1 not in state.sessions

    def test_add_job(self, state):
        """Test adding a background job."""
        job = JobInfo(id=1, name="handler", module="exploit/multi/handler")
        state.add_job(job)

        assert 1 in state.jobs
        assert state.jobs[1].name == "handler"

    def test_remove_job(self, state):
        """Test removing a job."""
        job = JobInfo(id=1, name="test")
        state.add_job(job)

        removed = state.remove_job(1)

        assert removed is not None
        assert 1 not in state.jobs

    def test_add_route(self, state):
        """Test adding a pivot route."""
        route = RouteInfo(subnet="10.0.0.0", netmask="255.0.0.0", session_id=1)
        state.add_route(route)

        assert len(state.routes) == 1
        assert state.routes[0].subnet == "10.0.0.0"

    def test_set_module(self, state):
        """Test setting current module."""
        state.set_module("exploit/windows/smb/ms17_010_eternalblue")

        assert state.current_module == "exploit/windows/smb/ms17_010_eternalblue"

    def test_add_command_to_history(self, state):
        """Test command history tracking."""
        state.add_command("search smb")
        state.add_command("use exploit/multi/handler")

        assert len(state.command_history) == 2
        assert "search smb" in state.command_history

    def test_command_history_limit(self, state):
        """Test command history respects max limit."""
        state.max_history = 5

        for i in range(10):
            state.add_command(f"command_{i}")

        assert len(state.command_history) == 5
        assert "command_9" in state.command_history
        assert "command_0" not in state.command_history

    def test_update_from_parse_result(self, state):
        """Test updating state from parsed output."""
        result = MsfParseResult(
            sessions=[SessionInfo(id=1, type="meterpreter")],
            jobs=[JobInfo(id=1, name="handler")],
        )

        state.update_from_parse_result(result)

        assert 1 in state.sessions
        assert 1 in state.jobs

    def test_get_active_sessions(self, state):
        """Test getting active sessions."""
        state.add_session(SessionInfo(id=1, type="shell", opened=True))
        state.add_session(SessionInfo(id=2, type="meterpreter", opened=True))
        state.add_session(SessionInfo(id=3, type="shell", opened=False))

        active = state.get_active_sessions()

        assert len(active) == 2

    def test_get_meterpreter_sessions(self, state):
        """Test getting meterpreter sessions."""
        state.add_session(SessionInfo(id=1, type="shell", opened=True))
        state.add_session(SessionInfo(id=2, type="meterpreter", opened=True))
        state.add_session(SessionInfo(id=3, type="meterpreter", opened=True))

        meterpreters = state.get_meterpreter_sessions()

        assert len(meterpreters) == 2

    def test_to_dict(self, state):
        """Test serialization to dictionary."""
        state.add_session(SessionInfo(id=1, type="meterpreter"))
        state.add_job(JobInfo(id=1, name="handler"))
        state.set_module("exploit/multi/handler")

        d = state.to_dict()

        assert d["current_module"] == "exploit/multi/handler"
        assert "1" in d["sessions"] or 1 in d["sessions"]
        assert "1" in d["jobs"] or 1 in d["jobs"]

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "current_module": "exploit/multi/handler",
            "sessions": {
                "1": {"id": 1, "type": "meterpreter", "opened": True},
            },
            "jobs": {
                "1": {"id": 1, "name": "handler", "module": ""},
            },
            "routes": [
                {"subnet": "10.0.0.0", "netmask": "255.0.0.0", "session_id": 1},
            ],
            "command_history": ["test command"],
            "task_id": "test",
        }

        state = MsfSessionState.from_dict(data)

        assert state.current_module == "exploit/multi/handler"
        assert 1 in state.sessions
        assert 1 in state.jobs
        assert len(state.routes) == 1

    def test_get_context_summary(self, state):
        """Test context summary generation."""
        state.set_module("exploit/multi/handler")
        state.add_session(SessionInfo(id=1, type="meterpreter"))
        state.add_job(JobInfo(id=1))

        summary = state.get_context_summary()

        assert "Module:" in summary
        assert "Sessions:" in summary
        assert "jobs:" in summary.lower()


class TestAsyncSessionLock:
    """Test AsyncSessionLock class."""

    @pytest.fixture
    def lock_manager(self):
        """Create lock manager."""
        return AsyncSessionLock(default_timeout=5.0)

    @pytest.mark.asyncio
    async def test_acquire_and_release(self, lock_manager):
        """Test acquiring and releasing lock."""
        async with lock_manager.acquire("session_1"):
            assert lock_manager.is_locked("session_1")

        # Lock should be released after context exit
        assert not lock_manager.is_locked("session_1")

    @pytest.mark.asyncio
    async def test_concurrent_access_blocked(self, lock_manager):
        """Test that concurrent access is blocked."""
        acquired_order = []

        async def task(name: str, delay: float):
            async with lock_manager.acquire("session_1"):
                acquired_order.append(f"{name}_start")
                await asyncio.sleep(delay)
                acquired_order.append(f"{name}_end")

        # Start two tasks that try to acquire same lock
        await asyncio.gather(
            task("first", 0.1),
            task("second", 0.1),
        )

        # First task should complete before second starts
        assert acquired_order.index("first_end") < acquired_order.index("second_start")

    @pytest.mark.asyncio
    async def test_timeout_on_acquire(self, lock_manager):
        """Test timeout when lock cannot be acquired."""
        lock_manager._default_timeout = 0.1

        async def hold_lock():
            async with lock_manager.acquire("session_1"):
                await asyncio.sleep(1.0)

        async def try_acquire():
            async with lock_manager.acquire("session_1", timeout=0.1):
                pass

        # Start holder task
        holder_task = asyncio.create_task(hold_lock())
        await asyncio.sleep(0.05)  # Let holder acquire lock

        # Try to acquire should timeout
        with pytest.raises(asyncio.TimeoutError):
            await try_acquire()

        holder_task.cancel()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass


class TestSessionStateManager:
    """Test SessionStateManager class."""

    @pytest.fixture
    def temp_workspace(self):
        """Create temporary workspace directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def manager(self, temp_workspace):
        """Create state manager with temp workspace."""
        return SessionStateManager(workspace_root=temp_workspace)

    @pytest.mark.asyncio
    async def test_get_state_creates_new(self, manager):
        """Test getting state creates new if not exists."""
        state = await manager.get_state("new_task")

        assert isinstance(state, MsfSessionState)

    @pytest.mark.asyncio
    async def test_save_and_load_state(self, manager, temp_workspace):
        """Test saving and loading state."""
        # Create and modify state
        state = await manager.get_state("test_task")
        state.add_session(SessionInfo(id=1, type="meterpreter"))
        state.set_module("exploit/multi/handler")

        # Save state
        await manager.save_state("test_task")

        # Verify file exists
        state_file = os.path.join(temp_workspace, "test_task", "msf_state.json")
        assert os.path.exists(state_file)

        # Create new manager and load
        new_manager = SessionStateManager(workspace_root=temp_workspace)
        loaded_state = await new_manager.get_state("test_task")

        assert loaded_state.current_module == "exploit/multi/handler"
        assert 1 in loaded_state.sessions

    @pytest.mark.asyncio
    async def test_update_state(self, manager):
        """Test updating state from parse result."""
        result = MsfParseResult(
            sessions=[SessionInfo(id=1, type="meterpreter")],
        )

        state = await manager.update_state(
            "test_task",
            result,
            command="use exploit/multi/handler",
        )

        assert 1 in state.sessions
        assert "use exploit/multi/handler" in state.command_history

    @pytest.mark.asyncio
    async def test_clear_state(self, manager, temp_workspace):
        """Test clearing state."""
        # Create and save state
        state = await manager.get_state("test_task")
        state.add_session(SessionInfo(id=1, type="shell"))
        await manager.save_state("test_task")

        # Clear state
        await manager.clear_state("test_task")

        # Verify file removed and state cleared
        state_file = os.path.join(temp_workspace, "test_task", "msf_state.json")
        assert not os.path.exists(state_file)

    @pytest.mark.asyncio
    async def test_locked_state(self, manager):
        """Test getting state with lock."""
        async with manager.locked_state("test_task") as state:
            state.add_session(SessionInfo(id=1, type="shell"))

        # State should be accessible after lock release
        state = await manager.get_state("test_task")
        assert 1 in state.sessions


class TestGetSessionStateManager:
    """Test module-level convenience function."""

    def test_get_session_state_manager_returns_instance(self):
        """get_session_state_manager should return instance."""
        manager = get_session_state_manager()
        assert isinstance(manager, SessionStateManager)
