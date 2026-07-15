"""Compatibility tests for the decomposed Docker facade class."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.docker import UnifiedDockerService


@pytest.mark.asyncio
async def test_facade_mutable_surface_updates_delegate_state() -> None:
    service = UnifiedDockerService()
    fake_client = MagicMock()
    fake_containers = {}

    service.client = fake_client
    service.docker_available = False
    service.api_mode = "simulation"
    service.image_name = "example/runtime:latest"
    service.containers = fake_containers

    logs = await service.get_container_logs(999)

    assert service.client is fake_client
    assert service.docker_available is False
    assert service.api_mode == "simulation"
    assert service.image_name == "example/runtime:latest"
    assert service.containers is fake_containers
    assert service._logs.client is fake_client
    assert service._logs.docker_available is False
    assert service._logs.api_mode == "simulation"
    assert service._logs.image_name == "example/runtime:latest"
    assert service._logs.containers is fake_containers
    assert logs and logs[0]["service"] == "unified-docker-sim"


@pytest.mark.asyncio
async def test_facade_create_and_start_honors_instance_patched_workspace_validation() -> None:
    service = UnifiedDockerService()
    service._validate_workspace_ready = MagicMock(return_value=(False, "forced by test"))

    result = await service.create_and_start_container(task_id=77, target="127.0.0.1")

    assert result["success"] is False
    assert "Workspace validation failed: forced by test" == result["error"]
    assert result["container_id"] is None


@pytest.mark.asyncio
async def test_facade_ensure_image_available_uses_patched_check_image_exists() -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "sdk"
    service._check_image_exists = AsyncMock(return_value=True)
    service.client = MagicMock()
    service.client.images = MagicMock()
    service.image_name = "example/runtime@sha256:" + "a" * 64

    logs = await service._ensure_image_available()

    service._check_image_exists.assert_awaited_once()
    service.client.images.get.assert_not_called()
    assert any("found locally" in entry["message"] for entry in logs)
