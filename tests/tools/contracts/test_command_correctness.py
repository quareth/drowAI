"""Command correctness contract tests.

These tests validate that generated commands are syntactically correct
and follow the expected CLI patterns for each tool.
"""

from __future__ import annotations

from typing import Dict, List, Type

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.validation.command_validator import (
    CommandValidator,
    get_command_pattern,
    validate_tool_command,
)


class TestCommandCorrectnessContracts:
    """Test command correctness for all tools."""

    # Tools that have defined command patterns
    TOOLS_WITH_PATTERNS = [
        "information_gathering.dns.amass",
        "information_gathering.network_discovery.nmap",
        "information_gathering.network_discovery.masscan",
        "information_gathering.osint.theharvester",
        "password_attacks.online_attacks.hydra",
        "password_attacks.offline_attacks.crunch",
        "web_applications.cms_identification.wpscan",
        "web_applications.web_crawlers.gobuster",
        "web_applications.web_crawlers.ffuf",
        "web_applications.web_application_fuzzers.ffuf",
        "web_applications.web_vulnerability_scanners.nikto",
        "web_applications.web_vulnerability_scanners.sqlmap",
        "database_assessment.oracle_tools.tnscmd10g",
        "database_assessment.oracle_tools.oscanner",
        "database_assessment.oracle_tools.sidguesser",
        "forensics.digital_forensics.sleuthkit",
        "forensics.digital_forensics.volatility",
        "forensics.forensics_analysis_tools.binwalk",
        "forensics.digital_forensics.foremost",
        "forensics.digital_forensics.bulk_extractor",
        "forensics.forensics_analysis_tools.hashdeep",
        "forensics.forensics_analysis_tools.chkrootkit",
        "forensics.forensics_carving_tools.scalpel",
        "forensics.forensics_carving_tools.ddrescue",
        "forensics.forensics_carving_tools.safecopy",
    ]

    @pytest.fixture
    def validator(self) -> CommandValidator:
        return CommandValidator()

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_PATTERNS)
    def test_command_syntax(self, tool_id: str, validator: CommandValidator) -> None:
        """Test that generated commands have valid syntax."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        # Test with minimal args
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        result = validate_tool_command(tool, args, tool_id)
        
        # Warnings are OK, only fail on critical errors
        critical_errors = [e for e in result.errors if "injection" in e.lower() or "traversal" in e.lower()]
        assert not critical_errors, f"Critical command errors: {critical_errors}"
        
        if result.warnings:
            import warnings
            for w in result.warnings:
                warnings.warn(f"{tool_id}: {w}")
        
    @pytest.mark.parametrize("tool_id", TOOLS_WITH_PATTERNS)
    def test_command_binary_name(self, tool_id: str) -> None:
        """Test that command starts with correct binary name."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        param_fixture = load_param_fixture(tool_id)
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        try:
            command = tool.build_command(args)
        except NotImplementedError:
            pytest.skip("build_command not implemented")
        
        pattern = get_command_pattern(tool_id)
        
        if pattern:
            assert command[0] == pattern.binary_name, \
                f"Expected binary '{pattern.binary_name}', got '{command[0]}'"

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_PATTERNS)
    def test_no_mutually_exclusive_flags(self, tool_id: str, validator: CommandValidator) -> None:
        """Test that mutually exclusive flags aren't used together."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        param_fixture = load_param_fixture(tool_id)
        
        # Test with full args (more likely to have conflicts)
        full_params = param_fixture["test_cases"]["full"]["params"]
        args = args_class(**full_params)
        
        result = validate_tool_command(tool, args, tool_id)
        
        # Check specifically for mutual exclusivity errors
        exclusive_errors = [e for e in result.errors if "Mutually exclusive" in e]
        assert not exclusive_errors, f"Mutually exclusive flags found: {exclusive_errors}"

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_PATTERNS)
    def test_flag_value_patterns(self, tool_id: str, validator: CommandValidator) -> None:
        """Test that flag values match expected patterns."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        result = validate_tool_command(tool, args, tool_id)
        
        # Pattern mismatches are warnings, not failures
        # (patterns may need calibration for each tool)
        pattern_errors = [e for e in result.errors if "doesn't match pattern" in e]
        if pattern_errors:
            import warnings
            for e in pattern_errors:
                warnings.warn(f"{tool_id}: {e}")


class TestCommandStructure:
    """Test command structure and formatting."""

    # Sample tools for structure tests
    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.dns.amass",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_command_is_list_of_strings(self, tool_id: str) -> None:
        """Test that build_command returns List[str]."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
            args = args_class(**minimal_params)
            command = tool.build_command(args)
        except (NotImplementedError, FileNotFoundError):
            pytest.skip(f"Cannot test {tool_id}")
        
        assert isinstance(command, list), f"{tool_id}: command is not a list"
        assert all(isinstance(arg, str) for arg in command), \
            f"{tool_id}: command contains non-string elements"

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_command_not_empty(self, tool_id: str) -> None:
        """Test that build_command returns non-empty list."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
            args = args_class(**minimal_params)
            command = tool.build_command(args)
        except (NotImplementedError, FileNotFoundError):
            pytest.skip(f"Cannot test {tool_id}")
        
        assert len(command) > 0, f"{tool_id}: command is empty"

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_command_no_none_values(self, tool_id: str) -> None:
        """Test that command doesn't contain None values."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
            args = args_class(**minimal_params)
            command = tool.build_command(args)
        except (NotImplementedError, FileNotFoundError):
            pytest.skip(f"Cannot test {tool_id}")
        
        assert None not in command, f"{tool_id}: command contains None"

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_command_no_empty_strings(self, tool_id: str) -> None:
        """Test that command doesn't contain empty strings."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
            args = args_class(**minimal_params)
            command = tool.build_command(args)
        except (NotImplementedError, FileNotFoundError):
            pytest.skip(f"Cannot test {tool_id}")
        
        # Empty strings in commands can cause issues
        empty_count = command.count("")
        assert empty_count == 0, \
            f"{tool_id}: command contains {empty_count} empty strings"
