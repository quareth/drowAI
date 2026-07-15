"""Dev/test-scope environment collection tests for local UnifiedDockerService.

These tests verify local-provider environment info collection and saving when
containers start. They are not product task execution proof; product task
runtime is expected to use runner placement.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
import json

from backend.services.workspace.environment_collector import ENV_INFO_FILENAME


# =============================================================================
# Test: _collect_and_save_environment_info method
# =============================================================================

class TestCollectAndSaveEnvironmentInfo:
    """Tests for UnifiedDockerService._collect_and_save_environment_info."""
    
    @pytest.fixture
    def mock_workspace_path(self, tmp_path, monkeypatch):
        """Mock WorkspaceConfig to use temp directory."""
        def mock_get_path(task_id):
            return tmp_path / f"task-{task_id}"
        
        monkeypatch.setattr(
            "backend.services.workspace.environment_collector.WorkspaceConfig.get_task_workspace_path",
            mock_get_path
        )
        return tmp_path
    
    @pytest.fixture
    def mock_container(self):
        """Create a mock Docker container with exec_run."""
        container = MagicMock()
        
        def mock_exec_run(cmd, demux=True, **kwargs):
            mock_result = MagicMock()
            
            if "hostname" in cmd:
                mock_result.output = (b"kali-container-test", None)
            elif "os-release" in cmd:
                mock_result.output = (b'PRETTY_NAME="Kali GNU/Linux Rolling"\nVERSION_ID="2024.1"', None)
            elif "uname" in cmd:
                mock_result.output = (b"6.5.0-kali3-amd64", None)
            elif "ip addr" in cmd:
                mock_result.output = (b'1: lo: <LOOPBACK,UP>\n    inet 127.0.0.1/8\n2: eth0: <UP>\n    inet 172.17.0.2/16', None)
            elif "ip route" in cmd:
                mock_result.output = (b"default via 172.17.0.1 dev eth0\n172.17.0.0/16 dev eth0", None)
            elif "resolv.conf" in cmd:
                mock_result.output = (b"nameserver 8.8.8.8\nnameserver 8.8.4.4", None)
            else:
                mock_result.output = (b"", None)
            
            return mock_result
        
        container.exec_run = mock_exec_run
        return container
    
    @pytest.mark.asyncio
    async def test_collect_and_save_creates_file(self, mock_workspace_path, mock_container):
        """Verify collection creates env_info.json file."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 123
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        service = UnifiedDockerService()
        
        # Call the collection method directly
        logs = await service._collect_and_save_environment_info(mock_container, task_id)
        
        # Verify file was created
        env_file = workspace / ENV_INFO_FILENAME
        assert env_file.exists(), "env_info.json should be created"
        
        # Verify content
        with open(env_file) as f:
            env_info = json.load(f)
        
        assert env_info["hostname"] == "kali-container-test"
        assert env_info["os"]["name"] == "Kali GNU/Linux Rolling"
        assert env_info["network"]["default_gateway"] == "172.17.0.1"
    
    @pytest.mark.asyncio
    async def test_collect_logs_success(self, mock_workspace_path, mock_container):
        """Verify collection logs success message."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 456
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        service = UnifiedDockerService()
        logs = await service._collect_and_save_environment_info(mock_container, task_id)
        
        # Check logs contain collection info
        messages = [log["message"] for log in logs]
        
        assert any("Collecting container environment" in msg for msg in messages)
        assert any("Environment info collected" in msg for msg in messages)
        assert any("kali-container-test" in msg for msg in messages)
    
    @pytest.mark.asyncio
    async def test_collect_skips_cli_mode(self, mock_workspace_path):
        """Verify collection skips when container is CLI-mode dict."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 789
        
        # CLI mode returns a dict, not a Container object
        cli_container = {"id": "cli_789", "name": "kali-container-789"}
        
        service = UnifiedDockerService()
        logs = await service._collect_and_save_environment_info(cli_container, task_id)
        
        # Should skip collection
        messages = [log["message"] for log in logs]
        assert any("skipped (CLI mode)" in msg for msg in messages)
    
    @pytest.mark.asyncio
    async def test_collect_handles_partial_failure(self, mock_workspace_path):
        """Verify collection continues even if some commands fail."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 101
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)
        
        # Create container that only returns hostname
        container = MagicMock()
        
        def mock_exec_run(cmd, demux=True, **kwargs):
            mock_result = MagicMock()
            
            if "hostname" in cmd:
                mock_result.output = (b"partial-host", None)
            else:
                mock_result.output = (b"", None)
            
            return mock_result
        
        container.exec_run = mock_exec_run
        
        service = UnifiedDockerService()
        logs = await service._collect_and_save_environment_info(container, task_id)
        
        # File should still be created
        env_file = workspace / ENV_INFO_FILENAME
        assert env_file.exists()
        
        # Check for warning about unavailable data
        messages = [log["message"] for log in logs]
        assert any("Some environment data unavailable" in msg for msg in messages)
    
    @pytest.mark.asyncio
    async def test_collect_handles_save_exception(self, mock_workspace_path):
        """Verify collection handles save exceptions gracefully."""
        from backend.services.unified_docker_service import UnifiedDockerService
        
        task_id = 303
        
        # Create container that works
        container = MagicMock()
        container.exec_run.return_value = MagicMock(output=(b"test", None))
        
        service = UnifiedDockerService()
        
        # Mock save to raise an exception
        with patch("backend.services.unified_docker_service.save_environment_info") as mock_save:
            mock_save.side_effect = OSError("Permission denied")
            
            logs = await service._collect_and_save_environment_info(container, task_id)
        
        # Should log warning, not raise
        messages = [log["message"] for log in logs]
        assert any("collection failed" in msg.lower() for msg in messages)


# =============================================================================
# Test: Integration with create_and_start_container (mocked)
# =============================================================================

class TestCreateAndStartContainerIntegration:
    """Tests verifying env collection is called during container creation."""

    @pytest.mark.asyncio
    async def test_env_collection_called_on_success(self):
        """Verify _collect_and_save_environment_info is called after successful start."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()

        # Track if collection was called
        collection_called = False
        async def mock_collect(container, task_id):
            nonlocal collection_called
            collection_called = True
            return [{"timestamp": "test", "service": "test", "level": "info", "message": "test"}]

        # Mock all the heavy methods
        with patch.object(service, '_validate_workspace_ready', return_value=(True, "OK")), \
             patch.object(service, '_ensure_image_available', new_callable=AsyncMock, return_value=[]), \
             patch.object(service, '_ensure_task_network', create=True, return_value={"name": "task-net", "subnet": "198.18.0.0/29", "action": "created", "created": True}), \
             patch.object(service, '_create_container_sdk', new_callable=AsyncMock, return_value=MagicMock(id="test123")), \
             patch.object(service, '_start_container', new_callable=AsyncMock, return_value={"success": True, "logs": []}), \
             patch.object(service._lifecycle, '_verify_runtime_contract', return_value=(True, None)), \
             patch.object(service, '_initialize_container_environment', new_callable=AsyncMock, return_value=[]), \
             patch.object(service, '_ensure_vpn_ready', new_callable=AsyncMock, return_value=[]), \
             patch.object(service, '_collect_and_save_environment_info', side_effect=mock_collect):

            service.docker_available = True
            service.api_mode = "sdk"

            result = await service.create_and_start_container(task_id=999, target="127.0.0.1")

            assert result["success"] is True
            assert collection_called, "_collect_and_save_environment_info should be called"

    @pytest.mark.asyncio
    async def test_env_collection_does_not_start_vpn_during_container_creation(self):
        """VPN orchestration remains provider-owned after container provisioning."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()

        call_order = []

        async def mock_vpn_ready(container, task_id):
            call_order.append("vpn_ready")
            return []

        async def mock_collect(container, task_id):
            call_order.append("env_collect")
            return []

        with patch.object(service, '_validate_workspace_ready', return_value=(True, "OK")), \
             patch.object(service, '_ensure_image_available', new_callable=AsyncMock, return_value=[]), \
             patch.object(service, '_ensure_task_network', create=True, return_value={"name": "task-net", "subnet": "198.18.0.0/29", "action": "created", "created": True}), \
             patch.object(service, '_create_container_sdk', new_callable=AsyncMock, return_value=MagicMock(id="test123")), \
             patch.object(service, '_start_container', new_callable=AsyncMock, return_value={"success": True, "logs": []}), \
             patch.object(service._lifecycle, '_verify_runtime_contract', return_value=(True, None)), \
             patch.object(service, '_initialize_container_environment', new_callable=AsyncMock, return_value=[]), \
             patch.object(service, '_ensure_vpn_ready', side_effect=mock_vpn_ready), \
             patch.object(service, '_collect_and_save_environment_info', side_effect=mock_collect):

            service.docker_available = True
            service.api_mode = "sdk"

            result = await service.create_and_start_container(task_id=999, target="127.0.0.1")

            assert result["success"] is True
            assert call_order == ["env_collect"]


# =============================================================================
# Test: _ensure_vpn_ready method
# =============================================================================

class TestEnsureVpnReady:
    """Tests for UnifiedDockerService._ensure_vpn_ready."""

    @pytest.mark.asyncio
    async def test_skips_when_vpn_disabled(self):
        """Verify no exec_run call when task has vpn_enabled=False."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()
        container = MagicMock()

        mock_task = MagicMock()
        mock_task.vpn_enabled = False

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_task

        with patch("backend.database.SessionLocal", return_value=mock_db):
            logs = await service._ensure_vpn_ready(container, task_id=1)

        container.exec_run.assert_not_called()
        assert logs == []

    @pytest.mark.asyncio
    async def test_runs_connect_when_vpn_enabled(self):
        """Verify exec_run is called with vpn connect shell when vpn_enabled=True."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()
        container = MagicMock()

        mock_task = MagicMock()
        mock_task.vpn_enabled = True

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_task

        with patch("backend.database.SessionLocal", return_value=mock_db):
            logs = await service._ensure_vpn_ready(container, task_id=42)

        container.exec_run.assert_called_once()
        call_args = container.exec_run.call_args[0][0]
        assert call_args[0] == "bash"
        assert "connect" in call_args[2]

        messages = [log["message"] for log in logs]
        assert any("Ensuring VPN" in msg for msg in messages)
        assert any("completed" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_skips_cli_mode_container(self):
        """Verify no-op when container lacks exec_run (CLI mode)."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()
        container = {"id": "cli_1", "name": "kali-1"}  # dict, no exec_run

        logs = await service._ensure_vpn_ready(container, task_id=1)

        assert logs == []

    @pytest.mark.asyncio
    async def test_swallows_exceptions(self):
        """Verify exceptions are caught and logged, not raised."""
        from backend.services.unified_docker_service import UnifiedDockerService

        service = UnifiedDockerService()
        container = MagicMock()

        with patch("backend.database.SessionLocal", side_effect=RuntimeError("db down")):
            logs = await service._ensure_vpn_ready(container, task_id=1)

        messages = [log["message"] for log in logs]
        assert any("failed" in msg.lower() for msg in messages)


# =============================================================================
# Test: VPN interface captured in environment data
# =============================================================================

class TestVpnInterfaceCapture:
    """Tests verifying tun0 appears in collected environment data."""

    @pytest.fixture
    def mock_workspace_path(self, tmp_path, monkeypatch):
        """Mock WorkspaceConfig to use temp directory."""
        def mock_get_path(task_id):
            return tmp_path / f"task-{task_id}"

        monkeypatch.setattr(
            "backend.services.workspace.environment_collector.WorkspaceConfig.get_task_workspace_path",
            mock_get_path
        )
        return tmp_path

    @pytest.fixture
    def mock_vpn_container(self):
        """Create a mock container with eth0 and VPN tun0 interface."""
        container = MagicMock()

        def mock_exec_run(cmd, demux=True, **kwargs):
            mock_result = MagicMock()

            if "hostname" in cmd:
                mock_result.output = (b"kali-vpn-test", None)
            elif "os-release" in cmd:
                mock_result.output = (b'PRETTY_NAME="Kali GNU/Linux Rolling"\nVERSION_ID="2024.1"', None)
            elif "uname" in cmd:
                mock_result.output = (b"6.5.0-kali3-amd64", None)
            elif "ip addr" in cmd:
                mock_result.output = (
                    b"1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
                    b"    inet 127.0.0.1/8 scope host lo\n"
                    b"2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
                    b"    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0\n"
                    b"3: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500\n"
                    b"    inet 10.8.0.2/24 brd 10.8.0.255 scope global tun0\n",
                    None,
                )
            elif "ip route" in cmd:
                mock_result.output = (
                    b"default via 172.17.0.1 dev eth0\n"
                    b"10.8.0.0/24 dev tun0 proto kernel scope link src 10.8.0.2\n"
                    b"172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2\n",
                    None,
                )
            elif "resolv.conf" in cmd:
                mock_result.output = (b"nameserver 8.8.8.8\nnameserver 8.8.4.4", None)
            else:
                mock_result.output = (b"", None)

            return mock_result

        container.exec_run = mock_exec_run
        return container

    @pytest.mark.asyncio
    async def test_tun0_captured_in_env_info(self, mock_workspace_path, mock_vpn_container):
        """Verify tun0 interface and its IP appear in collected environment info."""
        from backend.services.unified_docker_service import UnifiedDockerService
        from backend.services.workspace.environment_collector import ENV_INFO_FILENAME

        task_id = 500
        workspace = mock_workspace_path / f"task-{task_id}"
        workspace.mkdir(parents=True)

        service = UnifiedDockerService()
        logs = await service._collect_and_save_environment_info(mock_vpn_container, task_id)

        env_file = workspace / ENV_INFO_FILENAME
        assert env_file.exists()

        with open(env_file) as f:
            env_info = json.load(f)

        interfaces = env_info["network"]["interfaces"]
        iface_names = [i["name"] for i in interfaces]

        assert "tun0" in iface_names, f"tun0 missing from interfaces: {iface_names}"

        tun0 = next(i for i in interfaces if i["name"] == "tun0")
        assert tun0["ipv4"] == "10.8.0.2/24"
        assert tun0["state"] == "UP"

    @pytest.mark.asyncio
    async def test_tun0_appears_in_formatted_prompt(self, mock_vpn_container):
        """Verify VPN IP appears in the formatted prompt string."""
        from backend.services.workspace.environment_collector import (
            collect_environment_info,
            format_environment_for_prompt,
        )

        env_info = collect_environment_info(mock_vpn_container)
        formatted = format_environment_for_prompt(env_info)

        assert "tun0" in formatted, f"tun0 missing from prompt:\n{formatted}"
        assert "10.8.0.2" in formatted, f"VPN IP missing from prompt:\n{formatted}"
