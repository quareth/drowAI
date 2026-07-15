"""
Tests for the Metasploit Mode Router.

The mode router determines whether operations should run in script mode
(msfconsole -x) or interactive mode (PTY session).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.tools.exploitation_tools.metasploit.mode_router import (
    ExecutionMode,
    MsfOperation,
    ModeRouter,
    ModeDecision,
    get_mode_router,
)


class TestMsfOperation:
    """Test MsfOperation enum values."""

    def test_script_mode_operations(self):
        """Verify operations categorized as script mode."""
        script_ops = [
            MsfOperation.SEARCH,
            MsfOperation.INFO,
            MsfOperation.DB_STATUS,
            MsfOperation.LIST_MODULES,
            MsfOperation.CHECK,
            MsfOperation.VERSION,
        ]
        for op in script_ops:
            assert op.value  # Has a value

    def test_interactive_mode_operations(self):
        """Verify operations categorized as interactive mode."""
        interactive_ops = [
            MsfOperation.EXPLOIT_HANDLER,
            MsfOperation.SESSION_INTERACT,
            MsfOperation.POST_EXPLOIT,
            MsfOperation.ROUTE_ADD,
            MsfOperation.JOBS,
            MsfOperation.PIVOT,
        ]
        for op in interactive_ops:
            assert op.value


class TestModeRouter:
    """Test ModeRouter class."""

    @pytest.fixture
    def router(self):
        """Create ModeRouter instance."""
        return ModeRouter(interactive_enabled=True)

    @pytest.fixture
    def router_no_interactive(self):
        """Create ModeRouter with interactive disabled."""
        return ModeRouter(interactive_enabled=False)

    @pytest.fixture
    def args(self):
        """Create a minimal mode-router argument object."""

        def _factory(**overrides):
            defaults = {
                "search_term": None,
                "session_id": None,
                "post_modules": None,
                "module_path": None,
                "module_name": None,
                "payload": None,
                "lhost": None,
                "lport": None,
                "resource_file": None,
                "command": None,
                "commands": None,
                "db_init": False,
                "db_rebuild_cache": None,
            }
            defaults.update(overrides)
            return SimpleNamespace(**defaults)

        return _factory

    def test_search_uses_script_mode(self, router, args):
        """Search operations should use script mode."""
        decision = router.determine_mode(args(search_term="smb"))

        assert decision.mode == ExecutionMode.SCRIPT
        assert decision.operation == MsfOperation.SEARCH

    def test_info_uses_script_mode(self, router, args):
        """Info command should use script mode."""
        decision = router.determine_mode(
            args(command="info exploit/windows/smb/ms17_010_eternalblue")
        )

        assert decision.mode == ExecutionMode.SCRIPT

    def test_auxiliary_scanner_uses_script(self, router, args):
        """Auxiliary scanners should use script mode."""
        decision = router.determine_mode(args(
            module_path="auxiliary/scanner/smb/smb_ms17_010",
        ))

        assert decision.mode == ExecutionMode.SCRIPT
        assert decision.operation == MsfOperation.AUXILIARY_SCAN

    def test_handler_uses_interactive_mode(self, router, args):
        """Handler setup should use interactive mode."""
        decision = router.determine_mode(args(
            module_path="exploit/multi/handler",
            payload="windows/meterpreter/reverse_tcp",
            lhost="192.168.1.100",
            lport=4444,
        ))

        assert decision.mode == ExecutionMode.INTERACTIVE
        assert decision.operation == MsfOperation.EXPLOIT_HANDLER

    def test_session_interaction_uses_interactive(self, router, args):
        """Session interaction should use interactive mode."""
        decision = router.determine_mode(args(session_id=1))

        assert decision.mode == ExecutionMode.INTERACTIVE
        assert decision.operation == MsfOperation.SESSION_INTERACT

    def test_post_module_uses_interactive(self, router, args):
        """Post-exploitation modules should use interactive mode."""
        decision = router.determine_mode(args(
            module_path="post/windows/gather/credentials",
            session_id=1,
        ))

        assert decision.mode == ExecutionMode.INTERACTIVE

    def test_fallback_to_script_when_interactive_disabled(
        self, router_no_interactive, args
    ):
        """Operations should fall back to script when interactive disabled."""
        decision = router_no_interactive.determine_mode(args(
            module_path="auxiliary/scanner/smb/smb_ms17_010",
        ))

        assert decision.mode == ExecutionMode.SCRIPT

    def test_force_interactive_patterns(self, router, args):
        """Commands with interactive patterns should force interactive mode."""
        decision = router.determine_mode(args(command="sessions -i 1"))

        assert decision.mode == ExecutionMode.INTERACTIVE

    def test_can_fallback_to_script(self, router):
        """Test fallback capability check."""
        assert router.can_fallback_to_script(MsfOperation.AUXILIARY_SCAN)
        assert router.can_fallback_to_script(MsfOperation.EXPLOIT_RUN)
        assert not router.can_fallback_to_script(MsfOperation.SESSION_INTERACT)
        assert not router.can_fallback_to_script(MsfOperation.POST_EXPLOIT)

    def test_requires_interactive(self, router):
        """Test interactive requirement check."""
        assert router.requires_interactive(MsfOperation.SESSION_INTERACT)
        assert router.requires_interactive(MsfOperation.POST_EXPLOIT)
        assert not router.requires_interactive(MsfOperation.SEARCH)
        assert not router.requires_interactive(MsfOperation.AUXILIARY_SCAN)

    def test_default_to_script_for_unknown(self, router, args):
        """Unknown operations should default to script mode."""
        decision = router.determine_mode(args())

        assert decision.mode == ExecutionMode.SCRIPT
        assert decision.can_fallback is True


class TestModeDecision:
    """Test ModeDecision dataclass."""

    def test_mode_decision_creation(self):
        """Test creating ModeDecision."""
        decision = ModeDecision(
            mode=ExecutionMode.SCRIPT,
            operation=MsfOperation.SEARCH,
            reason="Stateless search operation",
            can_fallback=True,
        )

        assert decision.mode == ExecutionMode.SCRIPT
        assert decision.operation == MsfOperation.SEARCH
        assert "Stateless" in decision.reason
        assert decision.can_fallback is True


class TestGetModeRouter:
    """Test module-level convenience function."""

    def test_get_mode_router_returns_instance(self):
        """get_mode_router should return a ModeRouter instance."""
        router = get_mode_router()
        assert isinstance(router, ModeRouter)

    def test_get_mode_router_is_cached(self):
        """get_mode_router should return same instance."""
        router1 = get_mode_router()
        router2 = get_mode_router()
        assert router1 is router2
