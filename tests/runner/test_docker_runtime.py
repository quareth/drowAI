"""Tests for runner-owned Docker runtime behavior with fake Docker clients."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from drowai_runner.docker_runtime import RunnerDockerRuntime, build_runner_container_config
from runtime_shared.docker_contracts import DEFAULT_RESOURCE_LIMITS
from runtime_shared.runtime_manifest import (
    FILE_COMM_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SEMANTIC_SCHEMA_VERSIONS,
    WORKSPACE_LAYOUT_VERSION,
)


class _FakeContainer:
    def __init__(self, container_id: str, status: str = "created") -> None:
        self.id = container_id
        self.status = status
        self.started = False
        self.stopped = False
        self.removed = False
        self.stop_timeout: int | None = None
        self.exec_commands: list[list[str]] = []
        self.signals: list[str] = []
        self.exec_stdout = json.dumps(
            {
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
                "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
                "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
            }
        )

    def start(self) -> None:
        self.started = True
        self.status = "running"

    def stop(self, timeout: int) -> None:
        self.stopped = True
        self.stop_timeout = timeout
        self.status = "stopped"

    def remove(self, force: bool) -> None:
        self.removed = force
        self.status = "removed"

    def reload(self) -> None:
        return None

    def logs(self, tail: int, timestamps: bool = False) -> bytes:
        assert timestamps is True
        return f"tail={tail}".encode("utf-8")

    def stats(self, stream: bool) -> dict[str, object]:
        assert stream is False
        return {
            "memory_stats": {"usage": 101, "limit": 1000},
            "cpu_stats": {"cpu_usage": {"total_usage": 55}},
        }

    def exec_run(self, command: list[str]) -> tuple[int, str]:
        self.exec_commands.append(command)
        return 0, self.exec_stdout

    def kill(self, signal: str) -> None:
        self.signals.append(signal)


class _FakeContainers:
    def __init__(self) -> None:
        self.by_id: dict[str, _FakeContainer] = {}
        self.created_configs: list[dict[str, object]] = []

    def create(self, **config: object) -> _FakeContainer:
        container_id = f"cid-{len(self.by_id) + 1}"
        container = _FakeContainer(container_id)
        self.by_id[container_id] = container
        self.created_configs.append(dict(config))
        return container

    def get(self, container_id: str) -> _FakeContainer:
        return self.by_id[container_id]


class _FakeImages:
    def __init__(self) -> None:
        self.present: set[str] = set()
        self.pulled: list[str] = []

    def get(self, image_name: str) -> object:
        if image_name not in self.present:
            raise RuntimeError("image not found")
        return {"name": image_name}

    def pull(self, image_name: str) -> object:
        self.present.add(image_name)
        self.pulled.append(image_name)
        return {"name": image_name}


class _FakeDockerClient:
    def __init__(self) -> None:
        self.images = _FakeImages()
        self.containers = _FakeContainers()


def test_runner_container_config_sets_expected_runtime_contract_env() -> None:
    config = build_runner_container_config(
        task_id=7,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-7"),
    )
    env = config["environment"]

    assert env["DROWAI_EXPECTED_RUNTIME_CONTRACT_VERSION"] == RUNTIME_CONTRACT_VERSION
    assert env["DROWAI_EXPECTED_FILE_COMM_SCHEMA_VERSION"] == FILE_COMM_SCHEMA_VERSION
    assert env["DROWAI_EXPECTED_WORKSPACE_LAYOUT_VERSION"] == WORKSPACE_LAYOUT_VERSION
    assert json.loads(env["DROWAI_EXPECTED_SEMANTIC_SCHEMA_VERSIONS"]) == dict(
        SEMANTIC_SCHEMA_VERSIONS
    )
    assert env["PYTHONPATH"] == "/opt/drowai/runtime/python"


def test_runner_container_config_maps_host_bind_root_for_workspace_mount() -> None:
    config = build_runner_container_config(
        task_id=15,
        image_name="runtime:test",
        workspace_path=Path("/container/data/tasks/task-15"),
        runner_root=Path("/container/data"),
        host_bind_root=Path("/host/data"),
    )

    assert config["volumes"]["/host/data/tasks/task-15"]["bind"] == "/workspace"
    assert config["volumes"]["/host/data/control/task-15"] == {
        "bind": "/run/drowai/control",
        "mode": "ro",
    }


def test_runner_container_config_uses_workspace_and_read_only_control_mounts() -> None:
    workspace_path = Path("/tmp/task-15")
    config = build_runner_container_config(
        tenant_id="tenant-A",
        task_id=15,
        image_name="runtime:test",
        workspace_path=workspace_path,
    )

    assert config["name"] == "drowai-tenant-a-task-15"
    assert {(item["bind"], item["mode"]) for item in config["volumes"].values()} == {
        ("/workspace", "rw"),
        ("/run/drowai/control", "ro"),
    }
    bind_source = str(workspace_path.resolve())
    assert config["volumes"][bind_source]["bind"] == "/workspace"
    for key, value in DEFAULT_RESOURCE_LIMITS.items():
        assert config[key] == value


def test_runner_container_config_attaches_only_the_managed_network() -> None:
    config = build_runner_container_config(
        tenant_id="tenant-a",
        task_id=15,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-15"),
        network_name="drowai-tenant-a-task-15-net",
    )

    assert config["network"] == "drowai-tenant-a-task-15-net"
    assert "network_mode" not in config


def test_runner_container_config_preserves_pentest_capabilities_without_privileged_mode() -> None:
    without_vpn = build_runner_container_config(
        task_id=5,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-5"),
        vpn_enabled=False,
    )
    with_vpn = build_runner_container_config(
        task_id=5,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-5"),
        vpn_enabled=True,
    )

    assert without_vpn["user"] == "root"
    assert without_vpn["cap_add"] == ["NET_ADMIN"]
    assert "devices" not in without_vpn
    assert without_vpn["environment"]["VPN_ENABLED"] == "false"
    assert "VPN_CONFIG" not in without_vpn["environment"]
    assert with_vpn["user"] == "root"
    assert with_vpn["cap_add"] == ["NET_ADMIN"]
    assert with_vpn["devices"] == ["/dev/net/tun:/dev/net/tun:rwm"]
    assert with_vpn["environment"]["VPN_ENABLED"] == "true"
    assert with_vpn["environment"]["VPN_CONFIG"] == "/run/drowai/control/vpn/task.ovpn"


def test_runner_container_command_probes_runtime_info_before_start() -> None:
    config = build_runner_container_config(
        task_id=9,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-9"),
    )

    assert config["tty"] is False
    assert config["stdin_open"] is False
    assert config["command"][:2] == ["/bin/bash", "-c"]
    startup_script = config["command"][2]
    assert "--runtime-info" in startup_script
    assert "DROWAI_EXPECTED_RUNTIME_CONTRACT_VERSION" in startup_script
    assert 'if [ -f "${VPN_CONFIG:-/vpn/task.ovpn}" ]' in startup_script
    assert "VPN config pending; waiting for runtime materialization" in startup_script
    assert "workspace_init.py" in startup_script
    assert "executor_daemon.py" in startup_script


def test_runner_docker_runtime_supports_fake_client_lifecycle_and_manifest_probe() -> None:
    fake_client = _FakeDockerClient()
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    pulled = runtime.ensure_runtime_image("runtime:test", pull_if_missing=True)
    assert pulled is True
    assert fake_client.images.pulled == ["runtime:test"]

    config = build_runner_container_config(
        tenant_id="Tenant/Prod",
        task_id=22,
        image_name="runtime:test",
        workspace_path=Path("/tmp/task-22"),
    )
    container_id = runtime.create_container(config)
    runtime.start_container(container_id)

    assert runtime.container_status(container_id) == "running"
    assert runtime.container_logs(container_id, tail=15) == "tail=15"
    assert runtime.container_metrics(container_id)["memory_usage"] == 101

    verification = runtime.verify_runtime_manifest(container_id)
    assert verification.ok is True
    assert verification.mismatch_keys == ()
    assert fake_client.containers.get(container_id).exec_commands == [
        ["python3", "/opt/drowai/runtime/python/executor_daemon.py", "--runtime-info"]
    ]

    runtime.stop_container(container_id, timeout_seconds=7)
    runtime.remove_container(container_id, force=True)
    container = fake_client.containers.get(container_id)
    assert container.stopped is True
    assert container.stop_timeout == 7
    assert container.removed is True


def test_runner_docker_runtime_refreshes_existing_tagged_image() -> None:
    fake_client = _FakeDockerClient()
    fake_client.images.present.add("runtime:latest")
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    pulled = runtime.ensure_runtime_image(
        "runtime:latest",
        pull_if_missing=True,
        refresh_if_tagged=True,
    )

    assert pulled is True
    assert fake_client.images.pulled == ["runtime:latest"]


def test_runner_docker_runtime_does_not_refresh_existing_digest_image() -> None:
    image = "runtime@sha256:" + "a" * 64
    fake_client = _FakeDockerClient()
    fake_client.images.present.add(image)
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    pulled = runtime.ensure_runtime_image(
        image,
        pull_if_missing=True,
        refresh_if_tagged=True,
    )

    assert pulled is False
    assert fake_client.images.pulled == []


def test_runner_docker_runtime_uses_existing_image_when_refresh_fails() -> None:
    fake_client = _FakeDockerClient()
    fake_client.images.present.add("runtime:latest")

    def _fail_pull(image_name: str) -> object:
        raise RuntimeError(f"registry unavailable for {image_name}")

    fake_client.images.pull = _fail_pull
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    pulled = runtime.ensure_runtime_image(
        "runtime:latest",
        pull_if_missing=True,
        refresh_if_tagged=True,
    )

    assert pulled is False


def test_runner_docker_runtime_remove_container_is_idempotent_when_missing() -> None:
    fake_client = _FakeDockerClient()
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    runtime.remove_container("cid-missing", force=True)


def test_runner_docker_runtime_manifest_verification_reports_mismatch() -> None:
    fake_client = _FakeDockerClient()
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)
    container = _FakeContainer("cid-1")
    container.exec_stdout = json.dumps(
        {
            "runtime_contract_version": "wrong",
            "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
            "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
            "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
        }
    )
    fake_client.containers.by_id["cid-1"] = container

    verification = runtime.verify_runtime_manifest("cid-1")
    assert verification.ok is False
    assert verification.mismatch_keys == ("runtime_contract_version",)


def test_runner_docker_runtime_raises_when_image_missing_and_pull_disabled() -> None:
    fake_client = _FakeDockerClient()
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    with pytest.raises(RuntimeError, match="image not found"):
        runtime.ensure_runtime_image("runtime:missing", pull_if_missing=False)


def test_exec_probe_enforces_timeout_bound() -> None:
    class _SlowContainer(_FakeContainer):
        def exec_run(self, command: list[str]) -> tuple[int, str]:
            del command
            time.sleep(2)
            return 0, "late"

    fake_client = _FakeDockerClient()
    fake_client.containers.by_id["cid-slow"] = _SlowContainer("cid-slow")
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)

    started = time.monotonic()
    result = runtime.exec_probe("cid-slow", ["/bin/true"], timeout_seconds=1)
    elapsed = time.monotonic() - started

    assert result.exit_code == 124
    assert "timed out" in result.stderr
    assert elapsed < 1.5


def test_send_signal_returns_transport_result() -> None:
    fake_client = _FakeDockerClient()
    runtime = RunnerDockerRuntime(client_factory=lambda: fake_client)
    fake_client.containers.by_id["cid-signal"] = _FakeContainer("cid-signal")

    sent, error = runtime.send_signal("cid-signal", "SIGUSR1")

    assert sent is True
    assert error is None
    assert fake_client.containers.by_id["cid-signal"].signals == ["SIGUSR1"]
