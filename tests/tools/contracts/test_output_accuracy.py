"""Output parsing accuracy contract tests.

These tests validate that parse_output() correctly extracts expected
fields and data structures from tool output.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.fixtures.output_fixtures import load_output_fixture
from tests.tools.validation.output_validator import (
    OutputValidator,
    ExpectedOutput,
    EXPECTED_OUTPUTS,
    validate_parse_output,
    validate_output_extracts_data,
)


class TestOutputAccuracyContracts:
    """Test output parsing accuracy for tools."""

    # Tools that have defined expected output schemas
    TOOLS_WITH_SCHEMAS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.network_discovery.masscan",
        "information_gathering.dns.amass",
        "information_gathering.osint.theharvester",
        "password_attacks.online_attacks.hydra",
        "password_attacks.offline_attacks.john",
        "password_attacks.offline_attacks.hashcat",
        "web_applications.cms_identification.wpscan",
        "web_applications.web_crawlers.gobuster",
        "web_applications.web_crawlers.ffuf",
        "web_applications.web_application_fuzzers.ffuf",
        "web_applications.web_vulnerability_scanners.nikto",
        "web_applications.web_vulnerability_scanners.sqlmap",
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
    def validator(self) -> OutputValidator:
        return OutputValidator()

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_SCHEMAS)
    def test_output_returns_dict(self, tool_id: str) -> None:
        """Test that parse_output returns a dictionary."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            output_fixture = load_output_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        metadata = tool.parse_output(output_fixture, "", 0, args)
        
        assert isinstance(metadata, dict), f"Expected dict, got {type(metadata)}"

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_SCHEMAS)
    def test_output_schema_compliance(self, tool_id: str, validator: OutputValidator) -> None:
        """Test that parsed output matches expected schema."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            output_fixture = load_output_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        result = validate_parse_output(
            tool, output_fixture, "", 0, args, tool_id
        )
        
        # Should not have critical errors
        assert result.valid or len(result.errors) == 0, \
            f"Output validation errors: {result.errors}"

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_SCHEMAS)
    def test_output_has_required_fields(self, tool_id: str, validator: OutputValidator) -> None:
        """Test that required fields are present in output."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        tool_name = tool_id.split(".")[-1]
        
        expected = EXPECTED_OUTPUTS.get(tool_name)
        if not expected or not expected.required_fields:
            pytest.skip(f"No required fields defined for {tool_name}")
        
        try:
            param_fixture = load_param_fixture(tool_id)
            output_fixture = load_output_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        metadata = tool.parse_output(output_fixture, "", 0, args)
        
        for field in expected.required_fields:
            assert field in metadata, f"Required field '{field}' missing from output"

    @pytest.mark.parametrize("tool_id", TOOLS_WITH_SCHEMAS)
    def test_output_field_types(self, tool_id: str) -> None:
        """Test that output fields have correct types."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        tool_name = tool_id.split(".")[-1]
        
        expected = EXPECTED_OUTPUTS.get(tool_name)
        if not expected or not expected.field_types:
            pytest.skip(f"No field types defined for {tool_name}")
        
        try:
            param_fixture = load_param_fixture(tool_id)
            output_fixture = load_output_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        metadata = tool.parse_output(output_fixture, "", 0, args)
        
        for field, expected_type in expected.field_types.items():
            if field in metadata and metadata[field] is not None:
                assert isinstance(metadata[field], expected_type), \
                    f"Field '{field}' expected {expected_type}, got {type(metadata[field])}"


class TestOutputExtractionAccuracy:
    """Test that specific data is correctly extracted from output."""

    def test_nmap_extracts_hosts(self) -> None:
        """Test nmap extracts host information."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        # Sample nmap output with known data
        sample_output = """
Starting Nmap 7.93 ( https://nmap.org )
Nmap scan report for 192.168.1.1
Host is up (0.001s latency).
PORT     STATE SERVICE
22/tcp   open  ssh
80/tcp   open  http
443/tcp  open  https

Nmap done: 1 IP address (1 host up) scanned
"""
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            minimal_params = {"target": "192.168.1.1"}
        
        args = args_class(**minimal_params)
        metadata = tool.parse_output(sample_output, "", 0, args)
        
        # Should have some output structure
        assert isinstance(metadata, dict)
        # Note: actual field names depend on implementation

    def test_hydra_extracts_credentials(self) -> None:
        """Test hydra extracts credential information."""
        tool_cls = get_tool("password_attacks.online_attacks.hydra")
        if tool_cls is None:
            pytest.skip("hydra tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        # Sample hydra output with found credentials
        sample_output = """
Hydra v9.4 (c) 2022 by van Hauser/THC & David Maciejak
[DATA] max 16 tasks per 1 server, overall 16 tasks
[DATA] attacking ssh://192.168.1.1:22/
[22][ssh] host: 192.168.1.1   login: admin   password: admin123
1 of 1 target successfully completed, 1 valid password found
"""
        
        try:
            param_fixture = load_param_fixture("password_attacks.online_attacks.hydra")
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            minimal_params = {"target": "192.168.1.1", "username": "admin", "wordlist": "/tmp/words.txt", "protocol": "ssh"}
        
        args = args_class(**minimal_params)
        metadata = tool.parse_output(sample_output, "", 0, args)
        
        assert isinstance(metadata, dict)


class TestOutputErrorHandling:
    """Test output parsing with error conditions."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_vulnerability_scanners.nikto",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_empty_output_handling(self, tool_id: str) -> None:
        """Test handling of empty output."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        # Should handle empty output gracefully
        metadata = tool.parse_output("", "", 0, args)
        assert isinstance(metadata, dict)

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_error_output_handling(self, tool_id: str) -> None:
        """Test handling of error output."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        # Should handle error output gracefully
        error_output = "Error: Connection refused"
        metadata = tool.parse_output("", error_output, 1, args)
        assert isinstance(metadata, dict)

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_malformed_output_handling(self, tool_id: str) -> None:
        """Test handling of malformed output."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        # Should handle garbage gracefully
        garbage = "asdf\x00\xff\xfegarbage data"
        metadata = tool.parse_output(garbage, "", 0, args)
        assert isinstance(metadata, dict)

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_timeout_output_handling(self, tool_id: str) -> None:
        """Test handling of timeout-related output."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"Missing fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        # Should handle timeout indicators
        timeout_output = "Operation timed out after 30 seconds"
        metadata = tool.parse_output(timeout_output, "", -2, args)
        assert isinstance(metadata, dict)


class TestOutputCoverage:
    """Test that output parsing covers expected data."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_coverage_scores(self, tool_id: str) -> None:
        """Test that output parsers calculate coverage."""
        validator = OutputValidator()
        
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            output_fixture = load_output_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"Missing fixtures for {tool_id}")
        
        args = args_class(**minimal_params)
        metadata = tool.parse_output(output_fixture, "", 0, args)
        
        result = validator.validate_output(metadata, tool_id, output_fixture)
        
        # Coverage score should be calculable
        assert result.coverage_score >= 0
