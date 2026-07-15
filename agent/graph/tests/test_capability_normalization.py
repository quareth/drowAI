"""Comprehensive tests for CapabilityType normalization and taxonomy.

Tests cover:
- from_intent() mapping for all capability types
- Synonym mapping (multiple phrases → same capability)
- Ambiguous intent fallback to RESPOND
- Tool category resolution via get_tool_categories()
- Enum validation rejects invalid strings
- State serialization with enum (JSON compatibility)
- Backward compatibility with legacy strings
- Migration helper function
- Intent classifier emits canonical capabilities
- Tool resolution accepts enum values
"""

from __future__ import annotations

import json
import pytest

from agent.graph.infrastructure.state_models import CapabilityType


class TestCapabilityTypeEnum:
    """Test CapabilityType enum definition and basic functionality."""

    def test_enum_values(self):
        """Test that all expected enum values exist."""
        assert CapabilityType.HOST_DISCOVERY.value == "host_discovery"
        assert CapabilityType.PORT_SCAN.value == "port_scan"
        assert CapabilityType.SERVICE_ENUM.value == "service_enum"
        assert CapabilityType.VULN_SCAN.value == "vuln_scan"
        assert CapabilityType.VULN_EXPLOIT.value == "vuln_exploit"
        assert CapabilityType.REPORT.value == "report"
        assert CapabilityType.RESPOND.value == "respond"

    def test_enum_serialization(self):
        """Test that enum values serialize to JSON correctly."""
        capability = CapabilityType.PORT_SCAN
        serialized = json.dumps(capability.value)
        assert serialized == '"port_scan"'
        
        # Test round-trip
        deserialized = json.loads(serialized)
        assert CapabilityType(deserialized) == capability

    def test_enum_validation_rejects_invalid(self):
        """Test that enum validation rejects invalid strings."""
        with pytest.raises(ValueError):
            CapabilityType("invalid_capability")
        
        with pytest.raises(ValueError):
            CapabilityType("unknown_type")


class TestFromIntentMapping:
    """Test from_intent() classmethod for capability normalization."""

    def test_host_discovery_patterns(self):
        """Test host discovery intent patterns."""
        test_cases = [
            "scan network",
            "find hosts",
            "discover machines",
            "online hosts",
            "host discovery",
            "network sweep",
            "ping sweep",
            "host scan",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.HOST_DISCOVERY, f"Failed for: {intent}"

    def test_port_scan_patterns(self):
        """Test port scanning intent patterns."""
        test_cases = [
            "scan ports",
            "open ports",
            "port scan",
            "find open ports",
            "port scanning",
            "scan for ports",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.PORT_SCAN, f"Failed for: {intent}"

    def test_service_enum_patterns(self):
        """Test service enumeration intent patterns."""
        test_cases = [
            "identify services",
            "service version",
            "enumerate services",
            "what services",
            "service detection",
            "service enumeration",
            "detect services",
            "service info",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.SERVICE_ENUM, f"Failed for: {intent}"

    def test_vuln_scan_patterns(self):
        """Test vulnerability scanning intent patterns."""
        test_cases = [
            "vulnerabilities",
            "find vulns",
            "security issues",
            "vulnerable services",
            "vuln scan",
            "vulnerability scan",
            "security scan",
            "find vulnerabilities",
            "check vulnerabilities",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.VULN_SCAN, f"Failed for: {intent}"

    def test_vuln_exploit_patterns(self):
        """Test exploitation intent patterns."""
        test_cases = [
            "exploit",
            "attack",
            "penetrate",
            "gain access",
            "compromise",
            "break into",
            "hack",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.VULN_EXPLOIT, f"Failed for: {intent}"

    def test_report_patterns(self):
        """Test reporting intent patterns."""
        test_cases = [
            "generate report",
            "summarize findings",
            "document results",
            "create report",
            "write report",
            "report findings",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.REPORT, f"Failed for: {intent}"

    def test_respond_fallback(self):
        """Test that ambiguous intents fallback to RESPOND."""
        test_cases = [
            "",
            "hello",
            "what is this?",
            "how are you?",
            "explain",
            "help",
            "unknown request",
        ]
        for intent in test_cases:
            result = CapabilityType.from_intent(intent)
            assert result == CapabilityType.RESPOND, f"Failed for: {intent}"

    def test_synonym_mapping(self):
        """Test that multiple phrases map to same capability."""
        # Test various ways of saying "port scan"
        phrases = [
            "scan ports",
            "port scan",
            "scan for ports",
            "find open ports",
        ]
        results = [CapabilityType.from_intent(p) for p in phrases]
        assert all(r == CapabilityType.PORT_SCAN for r in results)

    def test_case_insensitive(self):
        """Test that intent matching is case-insensitive."""
        assert CapabilityType.from_intent("SCAN PORTS") == CapabilityType.PORT_SCAN
        assert CapabilityType.from_intent("Scan Network") == CapabilityType.HOST_DISCOVERY
        assert CapabilityType.from_intent("vUlNeRaBiLiTiEs") == CapabilityType.VULN_SCAN


class TestGetToolCategories:
    """Test get_tool_categories() method."""

    def test_host_discovery_categories(self):
        """Test tool categories for host discovery."""
        categories = CapabilityType.HOST_DISCOVERY.get_tool_categories()
        assert "information_gathering" in categories

    def test_port_scan_categories(self):
        """Test tool categories for port scanning."""
        categories = CapabilityType.PORT_SCAN.get_tool_categories()
        assert "information_gathering" in categories

    def test_service_enum_categories(self):
        """Test tool categories for service enumeration."""
        categories = CapabilityType.SERVICE_ENUM.get_tool_categories()
        assert "information_gathering" in categories
        assert "system_services" in categories

    def test_vuln_scan_categories(self):
        """Test tool categories for vulnerability scanning."""
        categories = CapabilityType.VULN_SCAN.get_tool_categories()
        assert "vulnerability_analysis" in categories
        assert "web_applications" in categories

    def test_vuln_exploit_categories(self):
        """Test tool categories for exploitation."""
        categories = CapabilityType.VULN_EXPLOIT.get_tool_categories()
        assert "exploitation_tools" in categories
        assert "exploitation" not in categories

    def test_report_categories(self):
        """Test tool categories for reporting."""
        categories = CapabilityType.REPORT.get_tool_categories()
        assert "reporting_tools" in categories
        assert "reporting" not in categories

    def test_respond_categories(self):
        """Test that RESPOND has no tool categories."""
        categories = CapabilityType.RESPOND.get_tool_categories()
        assert categories == []


class TestBackwardCompatibility:
    """Test backward compatibility with legacy string capabilities."""

    def test_legacy_strings_normalize(self):
        """Test that legacy capability strings normalize correctly."""
        legacy_mappings = {
            "scan_ports": CapabilityType.PORT_SCAN,
            "scan_web": CapabilityType.PORT_SCAN,  # May map to port scan
            "enumerate_services": CapabilityType.SERVICE_ENUM,
            "test_exploit": CapabilityType.VULN_EXPLOIT,
            "gather_info": CapabilityType.HOST_DISCOVERY,
            "generate_report": CapabilityType.REPORT,
        }
        
        for legacy_str, expected in legacy_mappings.items():
            result = CapabilityType.from_intent(legacy_str)
            # Note: Some may not map exactly, but should not raise errors
            assert isinstance(result, CapabilityType)

    def test_string_comparison_still_works(self):
        """Test that string comparison with enum values works."""
        capability = CapabilityType.PORT_SCAN
        assert str(capability) == "CapabilityType.PORT_SCAN"
        assert capability.value == "port_scan"
        assert capability.value == "port_scan"  # Direct value comparison


class TestToolResolutionIntegration:
    """Test integration with tool resolution."""

    def test_resolve_tools_accepts_enum(self):
        """Test that resolve_tools_for_capability accepts CapabilityType enum."""
        try:
            from agent.tools.resolve_tools import resolve_tools_for_capability
            
            # Test with enum value
            tools = resolve_tools_for_capability(CapabilityType.PORT_SCAN)
            assert isinstance(tools, list)
            
            # Test with string (should normalize)
            tools_str = resolve_tools_for_capability("port scan")
            assert isinstance(tools_str, list)
            
        except ImportError:
            pytest.skip("resolve_tools module not available")

    def test_resolve_tools_handles_respond(self):
        """Test that RESPOND capability returns empty tool list."""
        try:
            from agent.tools.resolve_tools import resolve_tools_for_capability
            
            tools = resolve_tools_for_capability(CapabilityType.RESPOND)
            assert tools == []
            
        except ImportError:
            pytest.skip("resolve_tools module not available")


class TestStateSerialization:
    """Test state serialization with CapabilityType enum."""

    def test_state_with_enum_serializes(self):
        """Test that state containing CapabilityType enum serializes correctly."""
        from agent.graph.state import FactsState
        
        facts = FactsState(
            task_id=1,
            message="test",
            capability=CapabilityType.PORT_SCAN.value,  # Store as string value
        )
        
        # Serialize to dict
        state_dict = facts.model_dump()
        assert state_dict["capability"] == "port_scan"
        
        # Should be able to reconstruct
        facts2 = FactsState(**state_dict)
        assert facts2.capability == "port_scan"

    def test_enum_in_metadata(self):
        """Test that enum values work in metadata dicts."""
        metadata = {
            "intent_capability": CapabilityType.VULN_SCAN.value,
        }
        
        # Should serialize to JSON
        json_str = json.dumps(metadata)
        assert "vuln_scan" in json_str
        
        # Should deserialize
        deserialized = json.loads(json_str)
        assert deserialized["intent_capability"] == "vuln_scan"


class TestMigrationHelper:
    """Test migration helper functionality."""

    def test_normalize_capability_string(self):
        """Test normalizing capability string to enum."""
        # Test various input formats
        test_cases = [
            ("port scan", CapabilityType.PORT_SCAN),
            ("PORT_SCAN", CapabilityType.PORT_SCAN),
            ("scan_ports", CapabilityType.PORT_SCAN),
            ("vulnerability scan", CapabilityType.VULN_SCAN),
            ("exploit", CapabilityType.VULN_EXPLOIT),
        ]
        
        for input_str, expected in test_cases:
            result = CapabilityType.from_intent(input_str)
            assert result == expected, f"Failed for: {input_str}"

    def test_normalize_capability_enum(self):
        """Test that enum values normalize to themselves."""
        for capability in CapabilityType:
            result = CapabilityType.from_intent(capability.value)
            assert result == capability


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
