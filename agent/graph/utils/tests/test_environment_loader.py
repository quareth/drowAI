"""Tests for environment_loader utility module."""

import pytest
from unittest.mock import patch, MagicMock


class TestLoadAndFormatEnvironment:
    """Tests for load_and_format_environment function."""
    
    def test_returns_none_for_none_task_id(self):
        """Should return (None, '') when task_id is None."""
        from agent.graph.utils.environment_loader import load_and_format_environment
        
        env_info, formatted = load_and_format_environment(None)
        
        assert env_info is None
        assert formatted == ""
    
    def test_loads_env_info_from_backend(self):
        """Should load and format environment info when available."""
        mock_env_info = {
            "hostname": "kali-test",
            "os": {"name": "Kali Linux", "version": "2024.1", "kernel": "6.5.0"},
            "network": {
                "interfaces": [{"name": "eth0", "ipv4": "172.17.0.2/16", "state": "UP"}],
                "default_gateway": "172.17.0.1",
                "dns_servers": ["8.8.8.8"],
            },
            "routes": [],
        }
        
        with patch("backend.services.runtime_provider.environment_metadata.resolve_local_runtime_environment_info") as mock_load, \
             patch("backend.services.workspace.environment_collector.format_environment_for_prompt") as mock_format:
            
            mock_load.return_value = mock_env_info
            mock_format.return_value = "**Container Environment:**\n- Hostname: kali-test"
            
            from agent.graph.utils.environment_loader import load_and_format_environment
            env_info, formatted = load_and_format_environment(123)
            
            assert env_info == mock_env_info
            assert "kali-test" in formatted
            mock_load.assert_called_once_with(task_id=123)
            mock_format.assert_called_once_with(mock_env_info)
    
    def test_returns_none_when_no_env_file(self):
        """Should return (None, '') when env_info.json doesn't exist."""
        with patch("backend.services.runtime_provider.environment_metadata.resolve_local_runtime_environment_info") as mock_load:
            mock_load.return_value = None
            
            from agent.graph.utils.environment_loader import load_and_format_environment
            env_info, formatted = load_and_format_environment(456)
            
            assert env_info is None
            assert formatted == ""
    
    def test_handles_exception(self):
        """Should handle exceptions gracefully."""
        with patch("backend.services.runtime_provider.environment_metadata.resolve_local_runtime_environment_info") as mock_load:
            mock_load.side_effect = Exception("Unexpected error")
            
            from agent.graph.utils.environment_loader import load_and_format_environment
            env_info, formatted = load_and_format_environment(101)
            
            assert env_info is None
            assert formatted == ""


class TestGetEnvironmentCompact:
    """Tests for get_environment_compact function."""
    
    def test_returns_empty_for_none(self):
        """Should return empty string for None input."""
        from agent.graph.utils.environment_loader import get_environment_compact
        
        result = get_environment_compact(None)
        
        assert result == ""
    
    def test_formats_compact_output(self):
        """Should format environment as compact string."""
        mock_env_info = {
            "network": {
                "interfaces": [{"name": "eth0", "ipv4": "10.0.0.5/24"}],
                "default_gateway": "10.0.0.1",
                "dns_servers": ["8.8.8.8"],
            }
        }
        
        with patch("backend.services.workspace.environment_collector.format_environment_compact") as mock_format:
            mock_format.return_value = "eth0=10.0.0.5/24 | gw=10.0.0.1 | DNS=8.8.8.8"
            
            from agent.graph.utils.environment_loader import get_environment_compact
            result = get_environment_compact(mock_env_info)
            
            assert "eth0" in result
            assert "10.0.0.1" in result
            mock_format.assert_called_once_with(mock_env_info)


class TestBuildPlannerSystemPrompt:
    """Tests for build_planner_system_prompt function."""
    
    def test_prompt_without_env_info(self):
        """System prompt should mention unknown network config when no env info."""
        from agent.graph.nodes.planner_prompting import build_planner_system_prompt
        
        prompt = build_planner_system_prompt("")
        
        assert "penetration-testing workflows" in prompt
        assert "Kali Linux" in prompt
        assert "do not know your network configuration" in prompt
    
    def test_prompt_with_env_info(self):
        """System prompt should include env info when available."""
        from agent.graph.nodes.planner_prompting import build_planner_system_prompt
        
        env_prompt = """
**Container Environment:**
- Hostname: kali-test
- OS: Kali GNU/Linux Rolling

**Network Configuration:**
- Interfaces:
  - eth0: 172.17.0.2/16 (UP)
- Default Gateway: 172.17.0.1
"""
        
        prompt = build_planner_system_prompt(env_prompt)
        
        assert "penetration-testing workflows" in prompt
        assert "Kali Linux" in prompt
        assert "kali-test" in prompt
        assert "172.17.0.2" in prompt
        assert "know your container's network position" in prompt
        # Should NOT have the "do not know" message
        assert "do not know your network configuration" not in prompt
