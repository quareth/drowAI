"""Dev/test-scope tests for LocalDockerRuntimeProvider.

These tests cover the Management-owned local Docker provider implementation as
explicit provider behavior. They are not product task execution proof; product
task runtime is expected to use runner placement.
"""

from __future__ import annotations

import asyncio
import socket

from backend.config.workspace_config import WorkspaceConfig
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)
from backend.services.runtime_provider.local_docker_provider import (
    LocalDockerRuntimeProvider,
)


class _StubWorkspaceManager:
    def __init__(self):
        self.saved_config = None
        self.saved_scope = None

    def create_workspace(self, task_id: int) -> str:
        return f"/tmp/task-{task_id}"

    def save_config_file(self, task_id: int, config_data: dict) -> str:
        self.saved_config = (task_id, config_data)
        return f"/tmp/task-{task_id}/config.json"

    def save_scope_file(self, task_id: int, scope_content: str) -> str:
        self.saved_scope = (task_id, scope_content)
        return f"/tmp/task-{task_id}/scope.md"


class _StubDockerService:
    async def create_and_start_container(
        self,
        task_id: int,
        target: str,
        user_id: int | None,
        tenant_id: str,
    ):
        return {
            "status": "running",
            "task_id": task_id,
            "target": target,
            "user_id": user_id,
            "tenant_id": tenant_id,
        }

    async def get_container_status(self, task_id: int):
        return {"status": "running", "task_id": task_id}

    async def get_container_logs(self, task_id: int, lines: int):
        return {"status": "succeeded", "logs": [f"log-{task_id}"], "lines": lines}

    async def get_container_metrics(self, task_id: int):
        return {"status": "succeeded", "cpu_percent": 3.14}

    async def pause_container(self, task_id: int):
        return {"accepted": True, "status": "succeeded", "task_id": task_id}

    async def unpause_container(self, task_id: int):
        return {"accepted": True, "status": "succeeded", "task_id": task_id}

    async def stop_container(self, task_id: int):
        return {"accepted": True, "status": "succeeded", "task_id": task_id}

    async def remove_container(self, task_id: int, force: bool):
        return {"accepted": True, "status": "succeeded", "task_id": task_id, "force": force}

    async def execute_container_command(self, task_id: int, command: str):
        return {"status": "succeeded", "output": f"executed:{task_id}:{command}"}

    async def start_persistent_pty(self, task_id: int, shell: str, cols: int, rows: int):
        return {
            "accepted": True,
            "status": "running",
            "session_id": f"pty-{task_id}",
            "shell": shell,
            "cols": cols,
            "rows": rows,
        }

    def build_vpn_connect_exec_shell(self, task_id: int, *, reconnect: bool = False) -> str:
        action = "reconnect-vpn" if reconnect else "connect-vpn"
        return f"/usr/local/bin/{action} --task {task_id}"

    def get_runtime_path_diagnostic_fields(self, mount_policy=None):
        return {"mount_policy": mount_policy, "runtime_root": "/workspace"}

    def get_vpn_script_path_for_current_mode(self):
        return "/workspace/scripts/vpn_connect.sh"


def _request(operation: str, **payload):
    return RuntimeOperationRequest(
        tenant_id="tenant-local",
        task_id=123,
        user_id=7,
        actor_type=RuntimeActorType.USER,
        actor_id=7,
        runtime_placement_mode=RuntimePlacementMode.LOCAL,
        runtime_call_scope=RuntimeCallScope.TEST,
        workspace_id="task-123",
        operation=operation,
        payload=payload,
    )


def test_provision_task_runtime_delegates_to_unified_docker_service():
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    request = _request("provision_task_runtime")

    result = asyncio.run(provider.provision_task_runtime(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.RUNNING
    assert result.provider == "local_docker"
    assert result.metadata["delegate_result"]["task_id"] == 123
    assert result.metadata["delegate_result"]["tenant_id"] == "tenant-local"


def test_materialize_workspace_uses_workspace_manager_and_config_paths():
    workspace_manager = _StubWorkspaceManager()
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=workspace_manager,
    )
    request = _request(
        "materialize_runtime_workspace",
        config_data={"task_name": "example"},
        scope_content="network scope",
    )

    result = asyncio.run(provider.materialize_runtime_workspace(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["delegate_result"]["workspace_path"] == "/tmp/task-123"
    assert result.metadata["delegate_result"]["workspace_id"] == "task-123"
    assert result.metadata["delegate_result"]["container_workspace_path"] == "/workspace"
    assert workspace_manager.saved_config == (123, {"task_name": "example"})
    assert workspace_manager.saved_scope == (123, "network scope")


def test_local_provider_normalizes_delegate_exceptions():
    class _FailingDockerService(_StubDockerService):
        async def get_container_status(self, task_id: int):
            raise RuntimeError(f"boom-{task_id}")

    provider = LocalDockerRuntimeProvider(
        docker_service=_FailingDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    request = _request("get_runtime_status")

    result = asyncio.run(provider.get_runtime_status(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "runtime_operation_failed"
    assert "boom-123" in (result.error_message or "")


def test_unconfigured_adapter_operation_fails_closed():
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    request = _request("query_runtime_artifacts")

    result = asyncio.run(provider.query_runtime_artifacts(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "operation_not_configured"


def test_retry_vpn_connection_executes_reconnect_command():
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    request = _request("retry_vpn_connection")

    result = asyncio.run(provider.retry_vpn_connection(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert "reconnect-vpn --task 123" in result.metadata["delegate_result"]["output"]


def test_materialize_vpn_config_writes_local_runtime_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
    )
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_control_path",
        staticmethod(lambda task_id: tmp_path / "control" / f"task-{task_id}"),
    )
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    vpn_config = type("VPNConfig", (), {"config_data": "client\nremote x 1194\ndev tun\n" + ("a" * 60)})()
    request = _request("materialize_vpn_config", vpn_config=vpn_config)

    result = asyncio.run(provider.materialize_vpn_config(request))

    assert result.accepted is True
    ovpn_path = tmp_path / "control" / "task-123" / "vpn" / "task.ovpn"
    assert ovpn_path.read_text(encoding="utf-8") == vpn_config.config_data


def test_materialize_vpn_config_replaces_symlink_without_overwriting_target(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
    )
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_control_path",
        staticmethod(lambda task_id: tmp_path / "control" / f"task-{task_id}"),
    )
    vpn_directory = tmp_path / "control" / "task-123" / "vpn"
    vpn_directory.mkdir(parents=True)
    outside = tmp_path / "outside-canary.txt"
    outside.write_text("HOST_ONLY_CANARY", encoding="utf-8")
    destination = vpn_directory / "task.ovpn"
    destination.symlink_to(outside)
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    vpn_config = type(
        "VPNConfig",
        (),
        {"config_data": "client\nremote safe 1194\ndev tun\n" + ("a" * 60)},
    )()

    result = asyncio.run(
        provider.materialize_vpn_config(
            _request("materialize_vpn_config", vpn_config=vpn_config)
        )
    )

    assert result.accepted is True
    assert outside.read_text(encoding="utf-8") == "HOST_ONLY_CANARY"
    assert destination.is_symlink() is False
    assert destination.read_text(encoding="utf-8") == vpn_config.config_data


def test_append_runtime_input_rejects_symlink_without_appending_to_target(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
    )
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_control_path",
        staticmethod(lambda task_id: tmp_path / "control" / f"task-{task_id}"),
    )
    WorkspaceConfig.ensure_control_structure(123)
    outside = tmp_path / "outside-canary.jsonl"
    outside.write_text("HOST_ONLY_CANARY\n", encoding="utf-8")
    input_file = tmp_path / "control" / "task-123" / "runtime-input" / "user_input.jsonl"
    input_file.unlink()
    input_file.symlink_to(outside)
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )

    result = asyncio.run(
        provider.append_runtime_input(
            _request(
                "append_runtime_input",
                message="attacker-controlled append",
                strict_persistence=True,
            )
        )
    )

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert outside.read_text(encoding="utf-8") == "HOST_ONLY_CANARY\n"


def test_read_runtime_artifact_rejects_symlink_without_returning_target(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
    )
    workspace = tmp_path / "task-123"
    workspace.mkdir()
    outside = tmp_path / "outside-canary.txt"
    outside.write_text("HOST_ONLY_CANARY", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )

    result = asyncio.run(
        provider.read_runtime_artifact_file(
            _request("read_runtime_artifact_file", path="linked.txt")
        )
    )

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert "HOST_ONLY_CANARY" not in str(result.metadata)


def test_runtime_environment_metadata_roundtrip_uses_provider_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
    )
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    write_request = _request(
        "write_runtime_environment_metadata",
        environment={"hostname": "kali-task", "network": {"interfaces": []}},
    )

    write_result = asyncio.run(provider.write_runtime_environment_metadata(write_request))
    read_result = asyncio.run(
        provider.read_runtime_environment_metadata(_request("read_runtime_environment_metadata"))
    )

    assert write_result.accepted is True
    assert read_result.accepted is True
    assert read_result.metadata["delegate_result"]["environment"]["hostname"] == "kali-task"


def test_terminal_read_output_is_provider_mediated():
    provider = LocalDockerRuntimeProvider(
        docker_service=_StubDockerService(),
        workspace_manager=_StubWorkspaceManager(),
    )
    reader, writer = socket.socketpair()
    try:
        writer.sendall(b"ready\n")
        request = _request("read_terminal_output", socket=reader, size=16, timeout=0.5)

        result = asyncio.run(provider.read_terminal_output(request))
    finally:
        reader.close()
        writer.close()

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["delegate_result"]["data"] == b"ready\n"
