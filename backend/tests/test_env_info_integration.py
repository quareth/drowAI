"""Dev/test-scope end-to-end tests for local-provider environment info flow.

Tests the complete flow:
1. Environment info collection from container
2. Save to workspace file
3. Load into graph state
4. Full format injection in planner prompt
5. Compact format injection in PTR scope hints

This is local-provider diagnostic coverage, not product task execution proof;
product task runtime is expected to use runner placement.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from backend.services.workspace.environment_collector import (
    ENV_INFO_FILENAME,
    collect_environment_info,
    save_environment_info,
    load_environment_info,
    format_environment_for_prompt,
    format_environment_compact,
)


# =============================================================================
# Fixtures
# =============================================================================

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


@pytest.fixture
def sample_env_info():
    """Sample environment info matching expected Kali container output."""
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
def mock_container_with_kali_output():
    """Mock Docker container that returns realistic Kali output."""
    container = MagicMock()
    
    def mock_exec_run(cmd, demux=True, **kwargs):
        mock_result = MagicMock()
        
        if "hostname" in cmd:
            mock_result.output = (b"kali-task-123", None)
        elif "os-release" in cmd:
            mock_result.output = (
                b'PRETTY_NAME="Kali GNU/Linux Rolling"\n'
                b'NAME="Kali GNU/Linux"\n'
                b'VERSION_ID="2024.1"\n'
                b'ID=kali',
                None
            )
        elif "uname -r" in cmd:
            mock_result.output = (b"6.5.0-kali3-amd64", None)
        elif "ip addr" in cmd:
            mock_result.output = (
                b'1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n'
                b'    inet 127.0.0.1/8 scope host lo\n'
                b'2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n'
                b'    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0',
                None
            )
        elif "ip route" in cmd:
            mock_result.output = (
                b'default via 172.17.0.1 dev eth0\n'
                b'172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2',
                None
            )
        elif "resolv.conf" in cmd:
            mock_result.output = (b"nameserver 8.8.8.8\nnameserver 8.8.4.4", None)
        else:
            mock_result.output = (b"", None)
        
        return mock_result
    
    container.exec_run = mock_exec_run
    return container


# =============================================================================
# Test: Full Flow Integration
# =============================================================================

class TestFullFlowIntegration:
    """End-to-end tests for complete environment info flow."""
    
    def test_collect_save_load_format_roundtrip(
        self, mock_workspace_path, mock_container_with_kali_output
    ):
        """Test complete flow: collect → save → load → format."""
        task_id = 123
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        # Step 1: Collect from container
        env_info = collect_environment_info(mock_container_with_kali_output)
        
        # Verify collection
        assert env_info["hostname"] == "kali-task-123"
        assert env_info["os"]["name"] == "Kali GNU/Linux Rolling"
        assert env_info["network"]["default_gateway"] == "172.17.0.1"
        
        # Step 2: Save to workspace
        save_environment_info(task_id, env_info)
        
        # Verify file exists
        env_file = workspace / ENV_INFO_FILENAME
        assert env_file.exists()
        
        # Step 3: Load from workspace
        loaded = load_environment_info(task_id)
        assert loaded is not None
        assert loaded["hostname"] == "kali-task-123"
        
        # Step 4: Format for planner (FULL)
        full_format = format_environment_for_prompt(loaded)
        assert "kali-task-123" in full_format
        assert "172.17.0.2" in full_format
        assert "eth0" in full_format
        assert "8.8.8.8" in full_format
        assert "Kali GNU/Linux Rolling" in full_format
        
        # Step 5: Format for PTR (COMPACT)
        compact_format = format_environment_compact(loaded)
        assert "eth0=172.17.0.2" in compact_format
        assert "gw=172.17.0.1" in compact_format
        assert "DNS=8.8.8.8" in compact_format
        # Compact should be single line
        assert "\n" not in compact_format
    
    def test_file_format_is_valid_json(self, mock_workspace_path, sample_env_info):
        """Verify saved file is valid JSON with expected structure."""
        task_id = 456
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        save_environment_info(task_id, sample_env_info)
        
        env_file = workspace / ENV_INFO_FILENAME
        
        # Read and parse JSON directly
        with open(env_file, "r") as f:
            parsed = json.load(f)
        
        # Verify structure
        assert "hostname" in parsed
        assert "os" in parsed
        assert "network" in parsed
        assert "routes" in parsed
        
        # Verify nested structures
        assert "name" in parsed["os"]
        assert "interfaces" in parsed["network"]
        assert "default_gateway" in parsed["network"]
        assert "dns_servers" in parsed["network"]
    
    def test_full_format_token_efficiency(self, sample_env_info):
        """Verify full format stays within token budget (~200 tokens)."""
        full_format = format_environment_for_prompt(sample_env_info)
        
        # Rough token estimation: ~4 chars per token
        estimated_tokens = len(full_format) / 4
        
        # Should be around 200 tokens, definitely under 400
        assert estimated_tokens < 400, f"Full format too large: ~{estimated_tokens} tokens"
        
        # Should have meaningful content
        assert len(full_format) > 100, "Full format too short"
    
    def test_compact_format_token_efficiency(self, sample_env_info):
        """Verify compact format stays within token budget (~50 tokens)."""
        compact_format = format_environment_compact(sample_env_info)
        
        # Rough token estimation: ~4 chars per token
        estimated_tokens = len(compact_format) / 4
        
        # Should be around 50 tokens, definitely under 100
        assert estimated_tokens < 100, f"Compact format too large: ~{estimated_tokens} tokens"
        
        # Should have meaningful content
        assert len(compact_format) > 20, "Compact format too short"


# =============================================================================
# Test: State Persistence Simulation
# =============================================================================

class TestStatePersistenceSimulation:
    """Tests simulating state persistence across graph nodes."""
    
    def test_env_info_persists_in_metadata(self, sample_env_info):
        """Simulate how env_info flows through facts.metadata."""
        # Simulate planner loading env info
        facts_metadata = {}
        facts_metadata["environment_info"] = sample_env_info
        
        # Simulate PTR accessing it later
        env_info_from_state = facts_metadata.get("environment_info")
        
        assert env_info_from_state is not None
        assert env_info_from_state["hostname"] == "kali-task-123"
        
        # Format compact from state (like PTR does)
        compact = format_environment_compact(env_info_from_state)
        assert "eth0" in compact
        assert "172.17.0.1" in compact
    
    def test_missing_env_info_handled_gracefully(self):
        """Verify graceful handling when env_info is not in metadata."""
        facts_metadata = {}  # No environment_info
        
        env_info = facts_metadata.get("environment_info")
        
        # Should be None
        assert env_info is None
        
        # Formatting should handle None gracefully
        full_format = format_environment_for_prompt(env_info)
        compact_format = format_environment_compact(env_info)
        
        assert full_format == ""
        assert compact_format == ""


# =============================================================================
# Test: Docker Service Integration Simulation
# =============================================================================

class TestDockerServiceIntegration:
    """Tests simulating Docker service environment collection."""
    
    @pytest.mark.asyncio
    async def test_collection_method_integration(self, mock_workspace_path, mock_container_with_kali_output):
        """Test _collect_and_save_environment_info method behavior."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 789
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        service = UnifiedDockerService()
        
        # Call the collection method
        logs = await service._collect_and_save_environment_info(
            mock_container_with_kali_output, task_id
        )
        
        # Verify logs indicate success
        messages = [log["message"] for log in logs]
        assert any("Collecting" in msg for msg in messages)
        assert any("collected" in msg.lower() for msg in messages)
        
        # Verify file was created
        env_file = workspace / ENV_INFO_FILENAME
        assert env_file.exists()
        
        # Verify content
        with open(env_file) as f:
            env_info = json.load(f)
        
        assert env_info["hostname"] == "kali-task-123"
        assert env_info["network"]["default_gateway"] == "172.17.0.1"


# =============================================================================
# Test: Format Output Validation
# =============================================================================

class TestFormatOutputValidation:
    """Tests validating format output meets requirements."""
    
    def test_full_format_includes_all_sections(self, sample_env_info):
        """Verify full format includes all required sections."""
        full_format = format_environment_for_prompt(sample_env_info)
        
        # Required sections
        assert "Container Environment:" in full_format
        assert "Network Configuration:" in full_format
        assert "Routing Table:" in full_format
        
        # Required data points
        assert "Hostname:" in full_format
        assert "OS:" in full_format
        assert "Kernel:" in full_format
        assert "Interfaces:" in full_format
        assert "Default Gateway:" in full_format
        assert "DNS Servers:" in full_format
    
    def test_compact_format_structure(self, sample_env_info):
        """Verify compact format has expected structure."""
        compact = format_environment_compact(sample_env_info)
        
        # Should have pipe separators
        assert " | " in compact
        
        # Should have key=value pairs
        assert "eth0=" in compact
        assert "gw=" in compact
        assert "DNS=" in compact
        
        # Should not have loopback
        assert "127.0.0.1" not in compact
        assert "lo=" not in compact
    
    def test_full_format_readable(self, sample_env_info):
        """Verify full format is human-readable."""
        full_format = format_environment_for_prompt(sample_env_info)
        
        # Should have proper line breaks
        lines = full_format.strip().split("\n")
        assert len(lines) > 5, "Full format should have multiple lines"
        
        # Should have bullet points or dashes for lists
        assert any("-" in line for line in lines)
