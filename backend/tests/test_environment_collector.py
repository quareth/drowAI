"""Tests for environment_collector module.

Tests cover:
- Parsing helpers for OS, network, routes, DNS
- Collection from container (mocked)
- Save/load roundtrip
- Full and compact formatting
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from backend.services.workspace.environment_collector import (
    ENV_INFO_FILENAME,
    collect_environment_info,
    save_environment_info,
    load_environment_info,
    format_environment_for_prompt,
    format_environment_compact,
    _parse_os_release,
    _parse_ip_addr,
    _parse_routes,
    _parse_dns,
    _create_empty_env_info,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def sample_env_info():
    """Sample environment info for testing."""
    return {
        "collected_at": "2024-12-28T15:30:00Z",
        "hostname": "kali-task-123",
        "os": {
            "name": "Kali GNU/Linux Rolling",
            "version": "2024.1",
            "kernel": "6.5.0-kali3-amd64",
        },
        "network": {
            "interfaces": [
                {"name": "lo", "ipv4": "127.0.0.1/8", "state": "UP"},
                {"name": "eth0", "ipv4": "172.17.0.2/16", "state": "UP"},
            ],
            "default_gateway": "172.17.0.1",
            "dns_servers": ["8.8.8.8", "8.8.4.4"],
        },
        "routes": [
            {"destination": "default", "gateway": "172.17.0.1", "interface": "eth0"},
            {"destination": "172.17.0.0/16", "gateway": None, "interface": "eth0"},
        ],
        "collection_errors": [],
    }


@pytest.fixture
def mock_workspace_path(tmp_path, monkeypatch):
    """Mock WorkspaceConfig to use temp directory."""
    def mock_get_path(task_id):
        return tmp_path / f"task-{task_id}"
    
    monkeypatch.setattr(
        "backend.services.workspace.environment_collector.WorkspaceConfig.get_task_workspace_path",
        mock_get_path
    )
    return tmp_path


# =============================================================================
# Test: Empty Env Info
# =============================================================================

class TestCreateEmptyEnvInfo:
    """Tests for _create_empty_env_info."""
    
    def test_has_required_keys(self):
        """Verify empty env info has all required keys."""
        env_info = _create_empty_env_info()
        
        assert "collected_at" in env_info
        assert "hostname" in env_info
        assert "os" in env_info
        assert "network" in env_info
        assert "routes" in env_info
        assert "collection_errors" in env_info
    
    def test_default_values(self):
        """Verify default values are set correctly."""
        env_info = _create_empty_env_info()
        
        assert env_info["hostname"] == "unknown"
        assert env_info["os"]["name"] == "unknown"
        assert env_info["network"]["interfaces"] == []
        assert env_info["routes"] == []


# =============================================================================
# Test: Parse OS Release
# =============================================================================

class TestParseOsRelease:
    """Tests for _parse_os_release."""
    
    def test_parse_kali_os_release(self):
        """Parse typical Kali os-release content."""
        content = '''PRETTY_NAME="Kali GNU/Linux Rolling"
NAME="Kali GNU/Linux"
VERSION_ID="2024.1"
VERSION="2024.1"
ID=kali
ID_LIKE=debian'''
        
        result = _parse_os_release(content)
        
        assert result["name"] == "Kali GNU/Linux Rolling"
        assert result["version"] == "2024.1"
    
    def test_parse_debian_os_release(self):
        """Parse Debian os-release content."""
        content = '''PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
VERSION_ID="12"'''
        
        result = _parse_os_release(content)
        
        assert result["name"] == "Debian GNU/Linux 12 (bookworm)"
        assert result["version"] == "12"
    
    def test_parse_empty_content(self):
        """Handle empty content gracefully."""
        result = _parse_os_release("")
        
        assert result["name"] == "unknown"
        assert result["version"] == "unknown"
    
    def test_parse_malformed_content(self):
        """Handle malformed content gracefully."""
        content = "not valid content\nno equals signs"
        
        result = _parse_os_release(content)
        
        assert result["name"] == "unknown"


# =============================================================================
# Test: Parse IP Address
# =============================================================================

class TestParseIpAddr:
    """Tests for _parse_ip_addr."""
    
    def test_parse_single_interface(self):
        """Parse output with single interface."""
        output = '''1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP
    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0'''
        
        result = _parse_ip_addr(output)
        
        assert len(result) == 2
        
        # Check loopback
        lo = next((i for i in result if i["name"] == "lo"), None)
        assert lo is not None
        assert lo["ipv4"] == "127.0.0.1/8"
        assert lo["state"] == "UP"
        
        # Check eth0
        eth0 = next((i for i in result if i["name"] == "eth0"), None)
        assert eth0 is not None
        assert eth0["ipv4"] == "172.17.0.2/16"
        assert eth0["state"] == "UP"
    
    def test_parse_interface_with_at_symbol(self):
        """Parse interface name with @if123 format."""
        output = '''3: eth0@if456: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 10.0.0.5/24 scope global eth0'''
        
        result = _parse_ip_addr(output)
        
        assert len(result) == 1
        assert result[0]["name"] == "eth0"  # Should strip @if456
        assert result[0]["ipv4"] == "10.0.0.5/24"
    
    def test_parse_interface_down(self):
        """Parse interface that is DOWN."""
        output = '''2: eth1: <BROADCAST,MULTICAST> mtu 1500 state DOWN
    inet 192.168.1.100/24 scope global eth1'''
        
        result = _parse_ip_addr(output)
        
        assert len(result) == 1
        assert result[0]["state"] == "DOWN"
    
    def test_parse_empty_output(self):
        """Handle empty output gracefully."""
        result = _parse_ip_addr("")
        assert result == []
    
    def test_skip_inet6_addresses(self):
        """Skip IPv6 addresses, only parse IPv4."""
        output = '''2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0
    inet6 fe80::42:acff:fe11:2/64 scope link'''
        
        result = _parse_ip_addr(output)
        
        assert len(result) == 1
        assert result[0]["ipv4"] == "172.17.0.2/16"
        assert "inet6" not in result[0].get("ipv4", "")


# =============================================================================
# Test: Parse Routes
# =============================================================================

class TestParseRoutes:
    """Tests for _parse_routes."""
    
    def test_parse_default_route(self):
        """Parse default route with gateway."""
        output = "default via 172.17.0.1 dev eth0"
        
        result = _parse_routes(output)
        
        assert len(result) == 1
        assert result[0]["destination"] == "default"
        assert result[0]["gateway"] == "172.17.0.1"
        assert result[0]["interface"] == "eth0"
    
    def test_parse_direct_route(self):
        """Parse direct route without gateway."""
        output = "172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2"
        
        result = _parse_routes(output)
        
        assert len(result) == 1
        assert result[0]["destination"] == "172.17.0.0/16"
        assert result[0]["gateway"] is None
        assert result[0]["interface"] == "eth0"
    
    def test_parse_multiple_routes(self):
        """Parse multiple routes."""
        output = '''default via 172.17.0.1 dev eth0
172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2
192.168.1.0/24 via 172.17.0.100 dev eth0'''
        
        result = _parse_routes(output)
        
        assert len(result) == 3
    
    def test_parse_empty_output(self):
        """Handle empty output gracefully."""
        result = _parse_routes("")
        assert result == []


# =============================================================================
# Test: Parse DNS
# =============================================================================

class TestParseDns:
    """Tests for _parse_dns."""
    
    def test_parse_multiple_nameservers(self):
        """Parse multiple nameserver entries."""
        content = '''# Generated by NetworkManager
nameserver 8.8.8.8
nameserver 8.8.4.4
options edns0'''
        
        result = _parse_dns(content)
        
        assert result == ["8.8.8.8", "8.8.4.4"]
    
    def test_parse_single_nameserver(self):
        """Parse single nameserver entry."""
        content = "nameserver 1.1.1.1"
        
        result = _parse_dns(content)
        
        assert result == ["1.1.1.1"]
    
    def test_parse_empty_content(self):
        """Handle empty content gracefully."""
        result = _parse_dns("")
        assert result == []
    
    def test_skip_comments(self):
        """Skip comment lines."""
        content = '''# This is a comment
# nameserver 999.999.999.999
nameserver 8.8.8.8'''
        
        result = _parse_dns(content)
        
        assert result == ["8.8.8.8"]


# =============================================================================
# Test: Collect Environment Info
# =============================================================================

class TestCollectEnvironmentInfo:
    """Tests for collect_environment_info."""
    
    def test_collect_with_mock_container(self):
        """Collect env info from mocked container."""
        mock_container = MagicMock()
        
        # Mock exec_run to return different outputs for different commands
        def mock_exec_run(cmd, demux=True):
            mock_result = MagicMock()
            
            if "hostname" in cmd:
                mock_result.output = (b"kali-test", None)
            elif "os-release" in cmd:
                mock_result.output = (b'PRETTY_NAME="Kali"\nVERSION_ID="2024.1"', None)
            elif "uname" in cmd:
                mock_result.output = (b"6.5.0-kali3", None)
            elif "ip addr" in cmd:
                mock_result.output = (b'2: eth0: <UP>\n    inet 10.0.0.5/24', None)
            elif "ip route" in cmd:
                mock_result.output = (b"default via 10.0.0.1 dev eth0", None)
            elif "resolv.conf" in cmd:
                mock_result.output = (b"nameserver 8.8.8.8", None)
            else:
                mock_result.output = (b"", None)
            
            return mock_result
        
        mock_container.exec_run = mock_exec_run
        
        result = collect_environment_info(mock_container)
        
        assert result["hostname"] == "kali-test"
        assert result["os"]["name"] == "Kali"
        assert result["os"]["kernel"] == "6.5.0-kali3"
        assert len(result["network"]["interfaces"]) == 1
        assert result["network"]["default_gateway"] == "10.0.0.1"
        assert result["network"]["dns_servers"] == ["8.8.8.8"]
    
    def test_collect_handles_command_failure(self):
        """Collection continues even if some commands fail."""
        mock_container = MagicMock()
        
        def mock_exec_run(cmd, demux=True):
            mock_result = MagicMock()
            
            if "hostname" in cmd:
                mock_result.output = (b"test-host", None)
            else:
                # Simulate failure - return empty output
                mock_result.output = (b"", None)
            
            return mock_result
        
        mock_container.exec_run = mock_exec_run
        
        result = collect_environment_info(mock_container)
        
        # Should still have hostname
        assert result["hostname"] == "test-host"
        # Should have errors recorded
        assert len(result["collection_errors"]) > 0
    
    def test_collect_handles_exception(self):
        """Collection handles exceptions gracefully."""
        mock_container = MagicMock()
        
        def mock_exec_run(cmd, demux=True):
            if "hostname" in cmd:
                mock_result = MagicMock()
                mock_result.output = (b"test-host", None)
                return mock_result
            raise Exception("Docker error")
        
        mock_container.exec_run = mock_exec_run
        
        result = collect_environment_info(mock_container)
        
        # Should still return partial data
        assert result["hostname"] == "test-host"


# =============================================================================
# Test: Save and Load
# =============================================================================

class TestSaveLoadEnvironmentInfo:
    """Tests for save/load functions."""
    
    def test_save_creates_file(self, mock_workspace_path, sample_env_info):
        """Save creates env_info.json file."""
        task_id = 123
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        result_path = save_environment_info(task_id, sample_env_info)
        
        assert result_path.exists()
        assert result_path.name == ENV_INFO_FILENAME
    
    def test_save_and_load_roundtrip(self, mock_workspace_path, sample_env_info):
        """Save and load produces identical data."""
        task_id = 456
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        save_environment_info(task_id, sample_env_info)
        loaded = load_environment_info(task_id)
        
        assert loaded is not None
        assert loaded["hostname"] == sample_env_info["hostname"]
        assert loaded["os"] == sample_env_info["os"]
        assert loaded["network"] == sample_env_info["network"]
    
    def test_load_nonexistent_returns_none(self, mock_workspace_path):
        """Load returns None for nonexistent file."""
        task_id = 999
        
        result = load_environment_info(task_id)
        
        assert result is None
    
    def test_load_invalid_json_returns_none(self, mock_workspace_path):
        """Load returns None for invalid JSON."""
        task_id = 789
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        # Write invalid JSON
        env_file = workspace / ENV_INFO_FILENAME
        env_file.write_text("not valid json {{{")
        
        result = load_environment_info(task_id)
        
        assert result is None


# =============================================================================
# Test: Format Full
# =============================================================================

class TestFormatEnvironmentForPrompt:
    """Tests for format_environment_for_prompt (full format)."""
    
    def test_format_complete_info(self, sample_env_info):
        """Format complete environment info."""
        result = format_environment_for_prompt(sample_env_info)
        
        # Check key elements are present
        assert "kali-task-123" in result
        assert "Kali GNU/Linux Rolling" in result
        assert "6.5.0-kali3-amd64" in result
        assert "eth0" in result
        assert "172.17.0.2/16" in result
        assert "172.17.0.1" in result
        assert "8.8.8.8" in result
    
    def test_format_includes_sections(self, sample_env_info):
        """Format includes all major sections."""
        result = format_environment_for_prompt(sample_env_info)
        
        assert "Container Environment:" in result
        assert "Network Configuration:" in result
        assert "Routing Table:" in result
    
    def test_format_none_returns_empty(self):
        """Format returns empty string for None input."""
        assert format_environment_for_prompt(None) == ""
    
    def test_format_empty_dict_returns_something(self):
        """Format handles empty dict gracefully."""
        result = format_environment_for_prompt({})
        
        # Should return something with defaults
        assert "unknown" in result or result == ""
    
    def test_format_missing_fields_graceful(self):
        """Format handles missing fields gracefully."""
        partial_info = {
            "hostname": "test-host",
            # Missing os, network, routes
        }
        
        result = format_environment_for_prompt(partial_info)
        
        assert "test-host" in result


# =============================================================================
# Test: Format Compact
# =============================================================================

class TestFormatEnvironmentCompact:
    """Tests for format_environment_compact (one-liner format)."""
    
    def test_compact_format_includes_key_info(self, sample_env_info):
        """Compact format includes key network info."""
        result = format_environment_compact(sample_env_info)
        
        # Should include primary interface (not loopback)
        assert "eth0" in result
        assert "172.17.0.2" in result
        # Should include gateway
        assert "172.17.0.1" in result
        # Should include DNS
        assert "8.8.8.8" in result
    
    def test_compact_format_is_single_line(self, sample_env_info):
        """Compact format is a single line."""
        result = format_environment_compact(sample_env_info)
        
        assert "\n" not in result
    
    def test_compact_format_uses_pipe_separator(self, sample_env_info):
        """Compact format uses pipe separator."""
        result = format_environment_compact(sample_env_info)
        
        assert " | " in result
    
    def test_compact_format_skips_loopback(self, sample_env_info):
        """Compact format skips loopback interface."""
        result = format_environment_compact(sample_env_info)
        
        # Should NOT include loopback IP
        assert "127.0.0.1" not in result
    
    def test_compact_format_none_returns_empty(self):
        """Compact format returns empty string for None."""
        assert format_environment_compact(None) == ""
    
    def test_compact_format_empty_network_returns_empty(self):
        """Compact format returns empty for empty network info."""
        env_info = {
            "network": {
                "interfaces": [],
                "default_gateway": None,
                "dns_servers": [],
            }
        }
        
        result = format_environment_compact(env_info)
        
        assert result == ""
    
    def test_compact_format_only_loopback_returns_empty(self):
        """Compact format returns empty if only loopback exists."""
        env_info = {
            "network": {
                "interfaces": [
                    {"name": "lo", "ipv4": "127.0.0.1/8", "state": "UP"},
                ],
                "default_gateway": None,
                "dns_servers": [],
            }
        }
        
        result = format_environment_compact(env_info)
        
        # No non-loopback interface, no gateway, no DNS = empty
        assert result == ""
    
    def test_compact_format_gateway_only(self):
        """Compact format works with only gateway."""
        env_info = {
            "network": {
                "interfaces": [],
                "default_gateway": "192.168.1.1",
                "dns_servers": [],
            }
        }
        
        result = format_environment_compact(env_info)
        
        assert "gw=192.168.1.1" in result


# =============================================================================
# Test: Module Constants
# =============================================================================

class TestConstants:
    """Tests for module constants."""
    
    def test_env_info_filename(self):
        """Verify filename constant."""
        assert ENV_INFO_FILENAME == "env_info.json"

