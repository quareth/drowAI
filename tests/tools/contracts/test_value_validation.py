"""Value validation contract tests.

These tests validate that tool argument values conform to expected formats
for common security tool parameters.
"""

from __future__ import annotations

from typing import Dict, List, Type

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.validation.value_validator import (
    ValueValidator,
    ValueValidationResult,
    validate_tool_args,
)


class TestValueValidationContracts:
    """Test value validation for tool arguments."""

    @pytest.fixture
    def validator(self) -> ValueValidator:
        return ValueValidator()

    def test_valid_ip_addresses(self, validator: ValueValidator) -> None:
        """Test IP address validation."""
        result = ValueValidationResult()
        
        # Valid IPv4
        validator.validate_ip_address("192.168.1.1", "ip", result)
        assert result.valid
        
        # Valid IPv6
        result = ValueValidationResult()
        validator.validate_ip_address("::1", "ip", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_ip_address("2001:db8::1", "ip", result)
        assert result.valid

    def test_invalid_ip_addresses(self, validator: ValueValidator) -> None:
        """Test invalid IP address rejection."""
        result = ValueValidationResult()
        
        validator.validate_ip_address("999.999.999.999", "ip", result)
        assert not result.valid

        result = ValueValidationResult()
        validator.validate_ip_address("not-an-ip", "ip", result)
        assert not result.valid

    def test_valid_cidr_notation(self, validator: ValueValidator) -> None:
        """Test CIDR notation validation."""
        result = ValueValidationResult()
        
        validator.validate_ip_network("192.168.1.0/24", "network", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_ip_network("10.0.0.0/8", "network", result)
        assert result.valid

    def test_invalid_cidr_notation(self, validator: ValueValidator) -> None:
        """Test invalid CIDR rejection."""
        result = ValueValidationResult()
        
        validator.validate_ip_network("192.168.1.1/33", "network", result)
        assert not result.valid

    def test_valid_hostnames(self, validator: ValueValidator) -> None:
        """Test hostname validation."""
        result = ValueValidationResult()
        
        validator.validate_hostname("example.com", "host", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_hostname("sub.example.com", "host", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_hostname("test-server", "host", result)
        assert result.valid

    def test_invalid_hostnames(self, validator: ValueValidator) -> None:
        """Test invalid hostname rejection."""
        result = ValueValidationResult()
        
        validator.validate_hostname("-invalid.com", "host", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_hostname("invalid-.com", "host", result)
        assert not result.valid

    def test_valid_urls(self, validator: ValueValidator) -> None:
        """Test URL validation."""
        result = ValueValidationResult()
        
        validator.validate_url("http://example.com", "url", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_url("https://example.com:8080/path", "url", result)
        assert result.valid

    def test_invalid_urls(self, validator: ValueValidator) -> None:
        """Test invalid URL handling."""
        result = ValueValidationResult()
        
        # URL without host should fail
        validator.validate_url("http://", "url", result)
        assert not result.valid

    def test_valid_ports(self, validator: ValueValidator) -> None:
        """Test port validation."""
        result = ValueValidationResult()
        
        validator.validate_port(80, "port", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_port(443, "port", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_port(65535, "port", result)
        assert result.valid

    def test_invalid_ports(self, validator: ValueValidator) -> None:
        """Test invalid port rejection."""
        result = ValueValidationResult()
        
        validator.validate_port(0, "port", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_port(65536, "port", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_port(-1, "port", result)
        assert not result.valid

    def test_valid_port_specs(self, validator: ValueValidator) -> None:
        """Test port specification validation."""
        result = ValueValidationResult()
        
        validator.validate_port_spec("80", "ports", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_port_spec("80,443,8080", "ports", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_port_spec("1-1000", "ports", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_port_spec("80,443,8000-9000", "ports", result)
        assert result.valid

    def test_invalid_port_specs(self, validator: ValueValidator) -> None:
        """Test invalid port specification rejection."""
        result = ValueValidationResult()
        
        validator.validate_port_spec("0", "ports", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_port_spec("65536", "ports", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_port_spec("1000-500", "ports", result)  # Invalid range
        assert not result.valid

    def test_path_traversal_detection(self, validator: ValueValidator) -> None:
        """Test path traversal detection."""
        result = ValueValidationResult()
        
        validator.validate_file_path("../etc/passwd", "path", result)
        assert len(result.warnings) > 0  # Should warn about ..

    def test_timeout_validation(self, validator: ValueValidator) -> None:
        """Test timeout value validation."""
        result = ValueValidationResult()
        
        validator.validate_timeout(30, "timeout", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_timeout(-1, "timeout", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_timeout(100000, "timeout", result)
        assert len(result.warnings) > 0  # Very long timeout

    def test_threads_validation(self, validator: ValueValidator) -> None:
        """Test thread count validation."""
        result = ValueValidationResult()
        
        validator.validate_threads(10, "threads", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_threads(0, "threads", result)
        assert not result.valid
        
        result = ValueValidationResult()
        validator.validate_threads(10000, "threads", result)
        assert len(result.warnings) > 0  # High thread count


class TestToolArgsValidation:
    """Test value validation on actual tool arguments."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.dns.amass",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_fixture_args_valid(self, tool_id: str) -> None:
        """Test that fixture arguments pass value validation."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args = args_class(**minimal_params)
        
        result = validate_tool_args(args, tool_id)
        
        # Should not have errors (warnings are OK)
        assert result.valid, f"Validation errors: {result.errors}"


class TestEdgeCaseValues:
    """Test edge case value handling."""

    def test_special_characters_in_target(self) -> None:
        """Test targets with special characters."""
        validator = ValueValidator()
        
        # Should handle IDN domains
        result = ValueValidationResult()
        validator.validate_target("münchen.example.com", "target", result)
        # May warn but shouldn't crash
        
    def test_ipv6_targets(self) -> None:
        """Test IPv6 address handling."""
        validator = ValueValidator()
        
        result = ValueValidationResult()
        validator.validate_target("::1", "target", result)
        assert result.valid
        
        result = ValueValidationResult()
        validator.validate_target("fe80::1%eth0", "target", result)
        # Interface-specific addresses are complex
        
    def test_url_with_credentials(self) -> None:
        """Test URL with embedded credentials."""
        validator = ValueValidator()
        
        result = ValueValidationResult()
        validator.validate_url("http://user:pass@example.com", "url", result)
        assert result.valid

    def test_empty_values(self) -> None:
        """Test empty value handling."""
        validator = ValueValidator()
        
        result = ValueValidationResult()
        validator.validate_target("", "target", result)
        assert not result.valid

    def test_whitespace_in_paths(self) -> None:
        """Test paths with whitespace."""
        validator = ValueValidator()
        
        result = ValueValidationResult()
        validator.validate_file_path("/path/with spaces/file.txt", "path", result)
        # Should be valid but may warn

    def test_unicode_in_paths(self) -> None:
        """Test paths with unicode characters."""
        validator = ValueValidator()
        
        result = ValueValidationResult()
        validator.validate_file_path("/path/with/émojis/📁.txt", "path", result)
        # Should handle gracefully
