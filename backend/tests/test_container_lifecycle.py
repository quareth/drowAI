"""Dev/test-scope lifecycle tests for local UnifiedDockerService simulation mode.

These tests cover Management-owned local Docker service behavior for provider
diagnostics and non-DinD regression coverage. They are not product task
execution proof; product task runtime is expected to use runner placement.
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock

from backend.services.unified_docker_service import UnifiedDockerService
from runtime_shared.runtime_manifest import build_runtime_manifest

pytestmark = pytest.mark.execution_plane_non_dind_regression


def test_local_runtime_contract_rejects_layout_1_image() -> None:
    service = UnifiedDockerService()
    payload = build_runtime_manifest().to_dict()
    payload["workspace_layout_version"] = "1.0"
    container = MagicMock()
    container.exec_run.return_value = MagicMock(
        exit_code=0,
        output=json.dumps(payload).encode("utf-8"),
    )

    accepted, error = service._lifecycle._verify_runtime_contract(container)

    assert accepted is False
    assert error == "Runtime manifest contract mismatch: workspace_layout_version"


@pytest.mark.asyncio
async def test_container_lifecycle_controls_simulated_mode() -> None:
    service = UnifiedDockerService()
    service.docker_available = False
    service.api_mode = "simulation"

    status = await service.get_container_status(1)
    assert status == "simulated"

    ok, _ = await service.pause_container(1)
    assert ok

    ok, _ = await service.unpause_container(1)
    assert ok

    ok, _ = await service.send_signal(1, "SIGTERM")
    assert ok

    ok, _ = await service.stop_container(1)
    assert ok

    ok, _ = await service.remove_container(1, force=True)
    assert ok


@pytest.mark.asyncio
async def test_ensure_image_available_sdk_uses_logs_image_check_single_source() -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "sdk"
    service._check_image_exists = AsyncMock(return_value=True)
    service.client = MagicMock()
    service.client.images = MagicMock()
    service.client.api = MagicMock()
    service.client.api.pull.return_value = iter([])
    service.image_name = "example/runtime:latest"

    logs = await service._ensure_image_available()

    service._check_image_exists.assert_awaited_once()
    service.client.images.get.assert_not_called()
    service.client.api.pull.assert_called_once_with(
        "example/runtime:latest", stream=True, decode=True
    )
    assert any("refreshing tagged image" in entry["message"].lower() for entry in logs)


@pytest.mark.asyncio
async def test_ensure_image_available_sdk_keeps_existing_digest_without_pull() -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "sdk"
    service._check_image_exists = AsyncMock(return_value=True)
    service.client = MagicMock()
    service.client.images = MagicMock()
    service.client.api = MagicMock()
    service.image_name = "example/runtime@sha256:" + "a" * 64

    logs = await service._ensure_image_available()

    service.client.api.pull.assert_not_called()
    assert any("found locally" in entry["message"] for entry in logs)


@pytest.mark.asyncio
async def test_ensure_image_available_sdk_uses_existing_image_when_refresh_fails() -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "sdk"
    service._check_image_exists = AsyncMock(return_value=True)
    service.client = MagicMock()
    service.client.api.pull.side_effect = RuntimeError("registry unavailable")
    service.image_name = "example/runtime:latest"

    logs = await service._ensure_image_available()

    assert any(
        entry["level"] == "warning"
        and "using existing local image" in entry["message"]
        for entry in logs
    )


@pytest.mark.asyncio
async def test_ensure_image_available_cli_pull_path_unchanged_when_image_missing(monkeypatch) -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "cli"
    service._check_image_exists = AsyncMock(return_value=False)

    def _fake_cli_pull(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["docker", "pull"]:
            return MagicMock(returncode=0, stdout="Downloading\nExtracting\n", stderr="")
        raise AssertionError(f"Unexpected command in pull path: {cmd}")

    monkeypatch.setattr(
        "backend.services.docker.lifecycle.subprocess.run",
        _fake_cli_pull,
    )

    logs = await service._ensure_image_available()

    service._check_image_exists.assert_awaited_once()
    messages = [entry["message"] for entry in logs]
    assert any("Pulling image" in message for message in messages)
    assert any("pulled successfully" in message for message in messages)


@pytest.mark.asyncio
async def test_ensure_image_available_cli_refreshes_existing_tagged_image(monkeypatch) -> None:
    service = UnifiedDockerService()
    service.docker_available = True
    service.api_mode = "cli"
    service._check_image_exists = AsyncMock(return_value=True)
    service.image_name = "example/runtime:latest"

    pull_result = MagicMock(returncode=0, stdout="Status: newer image\n", stderr="")
    cli_pull = MagicMock(return_value=pull_result)
    monkeypatch.setattr("backend.services.docker.lifecycle.subprocess.run", cli_pull)

    logs = await service._ensure_image_available()

    cli_pull.assert_called_once_with(
        ["docker", "pull", "example/runtime:latest"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert any("refreshing tagged image" in entry["message"].lower() for entry in logs)
