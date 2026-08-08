"""Tests for runner CLI commands and error-code behavior."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner import app
from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.logs_metrics import RunnerLogsMetricsAdapter
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.runtime_manifest import (
    FILE_COMM_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SEMANTIC_SCHEMA_VERSIONS,
    WORKSPACE_LAYOUT_VERSION,
)


class _FakeRunnerContainer:
    def __init__(
        self,
        container_id: str,
        *,
        status_after_start: str = "running",
        exec_exit_code: int = 0,
        exec_stdout: str | None = None,
    ) -> None:
        self.id = container_id
        self.status = "created"
        self.status_after_start = status_after_start
        self.exec_exit_code = exec_exit_code
        self.exec_stdout = exec_stdout or json.dumps(
            {
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
                "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
                "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
            }
        )
        self.signals: list[str] = []
        self.exec_commands: list[list[str]] = []
        self.environment_outputs = {
            "hostname": "runner-task",
            "cat /etc/os-release": 'PRETTY_NAME="Kali"\nVERSION_ID="2026.1"',
            "uname -r": "6.12-kali",
            "ip addr show": "2: eth0: <UP>\n    inet 10.0.0.5/24",
            "ip route": "default via 10.0.0.1 dev eth0",
            "cat /etc/resolv.conf": "nameserver 192.168.65.7",
        }
        self.remove_force_values: list[bool] = []
        self.require_force_remove_while_running = False

    def start(self) -> None:
        self.status = self.status_after_start

    def stop(self, timeout: int) -> None:
        del timeout
        self.status = "stopped"

    def remove(self, force: bool) -> None:
        self.remove_force_values.append(force)
        if self.require_force_remove_while_running and self.status == "running" and not force:
            raise RuntimeError("container is running")
        self.status = "removed"

    def pause(self) -> None:
        self.status = "paused"

    def unpause(self) -> None:
        self.status = "running"

    def reload(self) -> None:
        return None

    def logs(self, tail: int) -> bytes:
        return f"tail={tail}".encode("utf-8")

    def stats(self, stream: bool) -> dict[str, object]:
        assert stream is False
        return {
            "memory_stats": {"usage": 10, "limit": 100},
            "cpu_stats": {"cpu_usage": {"total_usage": 5}},
        }

    def exec_run(self, command: list[str]) -> tuple[int, str]:
        self.exec_commands.append(command)
        command_text = " ".join(str(part) for part in command)
        if "executor_daemon.py" not in command_text:
            shell_command = str(command[-1])
            return 0, self.environment_outputs.get(shell_command, "")
        return self.exec_exit_code, self.exec_stdout

    def kill(self, signal: str) -> None:
        self.signals.append(signal)


class _FakeRunnerContainers:
    def __init__(self) -> None:
        self.by_id: dict[str, _FakeRunnerContainer] = {}
        self.create_delay_seconds = 0.0
        self.next_start_status = "running"
        self.next_exec_exit_code = 0
        self.next_exec_stdout: str | None = None
        self.run_calls: list[dict[str, object]] = []
        self.next_run_output: bytes | str = json.dumps(
            {
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
                "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
                "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
            }
        )

    def create(self, **_config: object) -> _FakeRunnerContainer:
        if self.create_delay_seconds > 0:
            time.sleep(self.create_delay_seconds)
        container_id = f"cid-{len(self.by_id) + 1}"
        container = _FakeRunnerContainer(
            container_id,
            status_after_start=self.next_start_status,
            exec_exit_code=self.next_exec_exit_code,
            exec_stdout=self.next_exec_stdout,
        )
        self.by_id[container_id] = container
        return container

    def get(self, container_id: str) -> _FakeRunnerContainer:
        return self.by_id[container_id]

    def run(self, image_name: str, **kwargs: object) -> bytes | str:
        self.run_calls.append({"image": image_name, **kwargs})
        return self.next_run_output


class _FakeRunnerImages:
    def __init__(self) -> None:
        self.present: set[str] = set()

    def get(self, image_name: str) -> object:
        if image_name not in self.present:
            raise RuntimeError("image not found")
        return {"name": image_name}

    def pull(self, image_name: str) -> object:
        self.present.add(image_name)
        return {"name": image_name}


class _FakeRunnerNetwork:
    def __init__(self, name: str, config: dict[str, object]) -> None:
        self.name = name
        self.removed = False
        ipam = config.get("ipam") or {}
        self.attrs = {
            "Driver": config.get("driver", "bridge"),
            "Internal": config.get("internal", False),
            "Labels": config.get("labels", {}),
            "Options": config.get("options", {}),
            "IPAM": {"Config": list(ipam.get("Config", []))},
            "Containers": {},
        }

    def reload(self) -> None:
        return None

    def remove(self) -> None:
        self.removed = True


class _FakeRunnerNetworks:
    def __init__(self) -> None:
        self.by_name: dict[str, _FakeRunnerNetwork] = {}

    def get(self, name: str) -> _FakeRunnerNetwork:
        if name not in self.by_name:
            raise KeyError(name)
        return self.by_name[name]

    def list(self) -> list[_FakeRunnerNetwork]:
        return [network for network in self.by_name.values() if not network.removed]

    def create(self, name: str, **config: object) -> _FakeRunnerNetwork:
        network = _FakeRunnerNetwork(name, dict(config))
        self.by_name[name] = network
        return network


class _FakeRunnerDockerClient:
    def __init__(self) -> None:
        self.images = _FakeRunnerImages()
        self.containers = _FakeRunnerContainers()
        self.networks = _FakeRunnerNetworks()
        self.ping_count = 0

    def ping(self) -> bool:
        self.ping_count += 1
        return True


class _RunnerOperationHarness:
    def __init__(
        self,
        *,
        config: app.RunnerConfig,
        docker_client_factory,
    ) -> None:
        self.workspace = RunnerWorkspaceManager(config.runner_root)
        self.workspace.initialize_runner_root()
        self.job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
        self.docker_runtime = RunnerDockerRuntime(client_factory=docker_client_factory)
        self.logs_metrics = RunnerLogsMetricsAdapter(
            job_store=self.job_store,
            docker_runtime=self.docker_runtime,
            workspace_manager=self.workspace,
        )
        self.terminal_proxy = RunnerTerminalProxy(
            job_store=self.job_store,
            pty_adapter=app._RunnerPtyAdapter(docker_runtime=self.docker_runtime),
        )
        self.cleanup = RunnerCleanupService(
            workspace_manager=self.workspace,
            job_store=self.job_store,
            remove_container=lambda container_id: self.docker_runtime.remove_container(
                container_id,
                force=True,
            ),
            cleanup_retention_hours=config.cleanup_retention_hours,
        )
        self.operations = RunnerOperationService(
            config=config,
            workspace=self.workspace,
            job_store=self.job_store,
            docker_runtime=self.docker_runtime,
            logs_metrics=self.logs_metrics,
            terminal_proxy=self.terminal_proxy,
            cleanup=self.cleanup,
        )

    def dispatch(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
        return self.operations.dispatch_operation(operation=operation, params=params)


def _build_runner_config(tmp_path: Path) -> app.RunnerConfig:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
            ]
        ),
        encoding="utf-8",
    )
    return app.load_config(config_path)


def test_append_runtime_input_persists_payload_and_signals_container(tmp_path: Path) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )
    workspace_id = "task-51"
    harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-51",
        tenant_id="tenant-a",
        task_id="51",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id="cid-1",
    )
    fake_client.containers.by_id["cid-1"] = _FakeRunnerContainer("cid-1")

    payload = harness.dispatch(
        operation="append_runtime_input",
        params={
            "runtime_job_id": "job-51",
            "message": "__reset_conversation",
            "strict_persistence": True,
            "metadata": {"command": "reset_conversation"},
        },
    )

    assert payload["accepted"] is True
    assert payload["status"] == "succeeded"
    metadata = payload["metadata"]
    assert metadata["success"] is True
    assert metadata["persisted"] is True
    assert metadata["signal_attempted"] is True
    assert metadata["signal_sent"] is True
    assert "workspace_path" not in metadata
    assert fake_client.containers.by_id["cid-1"].signals == ["SIGUSR1"]
    input_file = (
        tmp_path
        / "runner-root"
        / "control"
        / workspace_id
        / "runtime-input"
        / "user_input.jsonl"
    )
    line = input_file.read_text(encoding="utf-8").strip()
    assert "__reset_conversation" in line


def test_append_runtime_input_honors_strict_persistence_and_missing_job(tmp_path: Path) -> None:
    config = _build_runner_config(tmp_path)
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: _FakeRunnerDockerClient(),
    )

    missing_job = harness.dispatch(
        operation="append_runtime_input",
        params={"runtime_job_id": "missing-job", "message": "continue"},
    )
    assert missing_job["accepted"] is False
    assert missing_job["error_code"] == "RUNNER_JOB_NOT_FOUND"
    assert missing_job["metadata"]["success"] is False
    assert missing_job["metadata"]["signal_attempted"] is False

    workspace_id = "task-52"
    harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-52",
        tenant_id="tenant-a",
        task_id="52",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id=None,
    )
    input_file = harness.workspace.resolve_task_control(workspace_id) / "runtime-input" / "user_input.jsonl"
    canary = tmp_path / "strict-canary.jsonl"
    canary.write_text("unchanged\n", encoding="utf-8")
    input_file.unlink()
    input_file.symlink_to(canary)
    try:
        strict_failure = harness.dispatch(
            operation="append_runtime_input",
            params={
                "runtime_job_id": "job-52",
                "message": "continue",
                "strict_persistence": True,
            },
        )
        assert strict_failure["accepted"] is False
        assert strict_failure["error_code"] == "RUNNER_WORKSPACE_ENTRY_UNSAFE"
        assert strict_failure["metadata"]["success"] is False
        assert strict_failure["metadata"]["persisted"] is False
        assert strict_failure["metadata"]["signal_attempted"] is False
    finally:
        input_file.unlink(missing_ok=True)


def test_append_runtime_input_rejects_symlink_without_appending_to_target(
    tmp_path: Path,
) -> None:
    config = _build_runner_config(tmp_path)
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: _FakeRunnerDockerClient(),
    )
    workspace_id = "task-unsafe-input"
    harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-unsafe-input",
        tenant_id="tenant-a",
        task_id="53",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id=None,
    )
    canary = tmp_path / "outside.jsonl"
    canary.write_text("unchanged\n", encoding="utf-8")
    input_file = harness.workspace.resolve_task_control(workspace_id) / "runtime-input" / "user_input.jsonl"
    input_file.unlink()
    input_file.symlink_to(canary)

    result = harness.dispatch(
        operation="append_runtime_input",
        params={
            "runtime_job_id": "job-unsafe-input",
            "message": "attacker-controlled",
            "strict_persistence": True,
        },
    )

    assert result["accepted"] is False
    assert result["error_code"] == "RUNNER_WORKSPACE_ENTRY_UNSAFE"
    assert canary.read_text(encoding="utf-8") == "unchanged\n"


def test_append_runtime_input_best_effort_returns_signal_detail_when_container_missing(
    tmp_path: Path,
) -> None:
    config = _build_runner_config(tmp_path)
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: _FakeRunnerDockerClient(),
    )
    workspace_id = "task-53"
    harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-53",
        tenant_id="tenant-a",
        task_id="53",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id=None,
    )

    payload = harness.dispatch(
        operation="append_runtime_input",
        params={
            "runtime_job_id": "job-53",
            "message": "continue",
            "strict_persistence": False,
        },
    )

    assert payload["accepted"] is True
    assert payload["metadata"]["success"] is True
    assert payload["metadata"]["persisted"] is True
    assert payload["metadata"]["signal_attempted"] is False
    assert payload["metadata"]["signal_sent"] is False
    assert payload["metadata"]["detail"] == "Runtime container is not assigned."


def test_stop_runtime_preserves_state_and_retire_runtime_cleans_up(tmp_path: Path) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )
    workspace_id = "task-90"
    workspace_path = harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-90",
        tenant_id="tenant-a",
        task_id="90",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id="cid-90",
    )
    fake_client.containers.by_id["cid-90"] = _FakeRunnerContainer("cid-90")

    stop_payload = harness.dispatch(
        operation="stop_runtime",
        params={"runtime_job_id": "job-90"},
    )
    assert stop_payload["accepted"] is True
    assert stop_payload["status"] == "succeeded"
    assert stop_payload["metadata"]["lifecycle_outcome"] == "stopped"
    assert stop_payload["metadata"]["operation"] == "stop_runtime"
    assert workspace_path.exists()
    stopped_job = harness.job_store.get_job("job-90")
    assert stopped_job.status == "stopped"
    assert stopped_job.container_id == "cid-90"

    retire_payload = harness.dispatch(
        operation="retire_runtime",
        params={"runtime_job_id": "job-90"},
    )
    assert retire_payload["accepted"] is True
    assert retire_payload["status"] == "succeeded"
    assert retire_payload["metadata"]["workspace_removed"] is True
    assert retire_payload["metadata"]["container_removed"] is True
    assert not workspace_path.exists()
    cleaned_job = harness.job_store.get_job("job-90")
    assert cleaned_job.status == "cleaned_up"

    retry_payload = harness.dispatch(
        operation="retire_runtime",
        params={"runtime_job_id": "job-90"},
    )
    assert retry_payload["accepted"] is True
    assert retry_payload["status"] == "succeeded"
    assert retry_payload["metadata"]["idempotent"] is True
    assert harness.job_store.get_job("job-90").status == "cleaned_up"


def test_retire_runtime_succeeds_when_container_was_already_removed(
    tmp_path: Path,
) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )
    workspace_id = "task-91"
    workspace_path = harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-91",
        tenant_id="tenant-a",
        task_id="91",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id="cid-missing",
    )

    retire_payload = harness.dispatch(
        operation="retire_runtime",
        params={"runtime_job_id": "job-91"},
    )

    assert retire_payload["accepted"] is True
    assert retire_payload["status"] == "succeeded"
    assert retire_payload["metadata"]["container_removed"] is True
    assert retire_payload["metadata"]["workspace_removed"] is True
    assert not workspace_path.exists()
    assert harness.job_store.get_job("job-91").status == "cleaned_up"


def test_retire_runtime_surfaces_managed_network_removal_failure(
    monkeypatch, tmp_path: Path
) -> None:
    config = _build_runner_config(tmp_path)
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: _FakeRunnerDockerClient(),
    )
    workspace_id = "task-93"
    harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-93",
        tenant_id="tenant-a",
        task_id="93",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id=None,
    )

    def _fail_network_removal(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("managed network ownership mismatch")

    monkeypatch.setattr(
        RunnerDockerRuntime,
        "remove_task_network",
        _fail_network_removal,
    )

    result = harness.dispatch(
        operation="retire_runtime",
        params={"runtime_job_id": "job-93"},
    )

    assert result["accepted"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == "RUNNER_NETWORK_REMOVE_FAILED"
    assert result["metadata"]["network_removed"] is False
    assert "ownership mismatch" in result["error_message"]


def test_retire_runtime_force_removes_when_graceful_stop_does_not_finish(
    tmp_path: Path,
) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )
    workspace_id = "task-92"
    workspace_path = harness.workspace.initialize_task_workspace(workspace_id)
    harness.job_store.start_job(
        runtime_job_id="job-92",
        tenant_id="tenant-a",
        task_id="92",
        workspace_id=workspace_id,
        image="runtime:test",
        container_id="cid-running",
    )
    container = _FakeRunnerContainer("cid-running")
    container.status = "running"
    container.require_force_remove_while_running = True

    def _stop_times_out(timeout: int) -> None:
        del timeout
        raise RuntimeError("stop timed out")

    container.stop = _stop_times_out  # type: ignore[method-assign]
    fake_client.containers.by_id["cid-running"] = container

    retire_payload = harness.dispatch(
        operation="retire_runtime",
        params={"runtime_job_id": "job-92"},
    )

    assert retire_payload["accepted"] is True
    assert retire_payload["status"] == "succeeded"
    assert retire_payload["metadata"]["container_removed"] is True
    assert retire_payload["metadata"]["workspace_removed"] is True
    assert container.remove_force_values == [True]
    assert not workspace_path.exists()
    assert harness.job_store.get_job("job-92").status == "cleaned_up"


def test_health_command_reports_failed_when_docker_unavailable(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'runtime_image_tag = "custom/runtime:dev"',
            ]
        ),
        encoding="utf-8",
    )

    def _unavailable_client() -> object:
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(app, "_docker_client_factory", _unavailable_client)

    exit_code = app.main(["--config", str(config_path), "health"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_HEALTH_FAILED
    assert payload["status"] == "failed"
    assert payload["checks"]["config"] == "ok"
    assert payload["checks"]["workspace_root"] == "ok"
    assert payload["checks"]["docker"] == "failed"


def test_health_command_does_not_require_runtime_image(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'runtime_image_tag = "missing/runtime:dev"',
            ]
        ),
        encoding="utf-8",
    )
    fake_client = _FakeRunnerDockerClient()
    monkeypatch.setattr(app, "_docker_client_factory", lambda: fake_client)

    exit_code = app.main(["--config", str(config_path), "health"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_OK
    assert payload == {
        "status": "ok",
        "checks": {
            "config": "ok",
            "workspace_root": "ok",
            "docker": "ok",
        },
    }
    assert fake_client.ping_count == 1


def test_health_command_requires_registration_for_configured_cloud_runner(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'control_plane_url = "http://backend:8000"',
                "allow_insecure_cloud_endpoint = true",
                'registration_token = "rit_test_token"',
            ]
        ),
        encoding="utf-8",
    )

    fake_client = _FakeRunnerDockerClient()
    monkeypatch.setattr(app, "_docker_client_factory", lambda: fake_client)

    exit_code = app.main(["--config", str(config_path), "health"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_HEALTH_FAILED
    assert payload["status"] == "failed"
    assert payload["checks"]["registration"] == "failed"


def test_health_command_accepts_persisted_enrollment_registration(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    runner_root = tmp_path / "runner-root"
    config_path = tmp_path / "enrollment.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{runner_root}"',
                'control_plane_url = "http://backend:8000"',
                "allow_insecure_cloud_endpoint = true",
                'registration_token = "rit_test_token"',
            ]
        ),
        encoding="utf-8",
    )
    credential_dir = runner_root / "credentials"
    credential_dir.mkdir(parents=True)
    (credential_dir / "runner.secret").write_text("rsec_test\n", encoding="utf-8")
    (credential_dir / "runner.secret.runner_id").write_text("runner-123\n", encoding="utf-8")
    (credential_dir / "runner.secret.tenant_id").write_text("42\n", encoding="utf-8")

    fake_client = _FakeRunnerDockerClient()
    monkeypatch.setattr(app, "_docker_client_factory", lambda: fake_client)

    exit_code = app.main(["--config", str(config_path), "health"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_OK
    assert payload["status"] == "ok"
    assert payload["checks"]["registration"] == "ok"


def test_runtime_info_command_reports_runtime_manifest(capsys, monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'runtime_image_tag = "custom/runtime:dev"',
            ]
        ),
        encoding="utf-8",
    )
    fake_client = _FakeRunnerDockerClient()
    fake_client.images.present.add("custom/runtime:dev")
    fake_client.containers.next_run_output = json.dumps(
        {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
            "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
            "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
        }
    )
    monkeypatch.setattr(app, "_docker_client_factory", lambda: fake_client)

    exit_code = app.main(["--config", str(config_path), "runtime-info"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_OK
    assert payload["status"] == "ok"
    assert payload["runtime_image_tag"] == "custom/runtime:dev"
    assert payload["runtime_info"]["runtime_contract_version"] == RUNTIME_CONTRACT_VERSION
    assert payload["runtime_info"]["workspace_layout_version"] == WORKSPACE_LAYOUT_VERSION
    assert fake_client.containers.run_calls == [
        {
            "image": "custom/runtime:dev",
            "command": ["/opt/drowai/runtime/python/executor_daemon.py", "--runtime-info"],
            "entrypoint": "python3",
            "remove": True,
            "stderr": True,
            "stdout": True,
        }
    ]


def test_materialize_runtime_fails_closed_when_manifest_mismatches(tmp_path: Path) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    fake_client.containers.next_exec_stdout = json.dumps(
        {
            "runtime_contract_version": "wrong-contract-version",
            "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
            "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
            "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
        }
    )
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )

    payload = harness.dispatch(
        operation="materialize_runtime",
        params={
            "tenant_id": "tenant-a",
            "task_id": 88,
            "workspace_id": "task-88",
            "runtime_job_id": "job-88",
        },
    )

    assert payload["accepted"] is False
    assert payload["error_code"] == "RUNNER_MATERIALIZE_FAILED"
    assert "Runtime manifest contract mismatch" in payload["error_message"]
    assert fake_client.containers.by_id["cid-1"].status == "removed"
    assert harness.job_store.get_job("job-88").status == "failed"


def test_materialize_runtime_replaces_existing_container_when_manifest_mismatches(
    tmp_path: Path,
) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )
    harness.workspace.initialize_task_workspace("task-90")
    harness.job_store.start_job(
        runtime_job_id="job-90",
        tenant_id="tenant-a",
        task_id="90",
        workspace_id="task-90",
        image=config.runtime_image_tag,
        container_id="cid-1",
    )
    stale_container = _FakeRunnerContainer(
        "cid-1",
        exec_stdout=json.dumps(
            {
                "runtime_contract_version": "1.0",
                "file_comm_schema_version": FILE_COMM_SCHEMA_VERSION,
                "workspace_layout_version": WORKSPACE_LAYOUT_VERSION,
                "semantic_schema_versions": dict(SEMANTIC_SCHEMA_VERSIONS),
            }
        ),
    )
    stale_container.status = "running"
    fake_client.containers.by_id["cid-1"] = stale_container

    payload = harness.dispatch(
        operation="materialize_runtime",
        params={
            "tenant_id": "tenant-a",
            "task_id": 90,
            "workspace_id": "task-90",
            "runtime_job_id": "job-90",
        },
    )

    assert payload["accepted"] is True
    assert payload["metadata"]["container_id"] == "cid-2"
    assert "reused_existing_runtime" not in payload["metadata"]
    # Environment captured at start must ride on the start result so the control
    # plane can persist it for local, non-blocking prompt/context reads.
    assert isinstance(payload["metadata"].get("environment_info"), dict)
    assert fake_client.containers.by_id["cid-1"].status == "removed"
    assert fake_client.containers.by_id["cid-2"].status == "running"
    assert harness.job_store.get_job("job-90").container_id == "cid-2"


def test_materialize_runtime_fails_closed_when_container_exits_early(tmp_path: Path) -> None:
    config = _build_runner_config(tmp_path)
    fake_client = _FakeRunnerDockerClient()
    fake_client.containers.next_start_status = "exited"
    harness = _RunnerOperationHarness(
        config=config,
        docker_client_factory=lambda: fake_client,
    )

    payload = harness.dispatch(
        operation="materialize_runtime",
        params={
            "tenant_id": "tenant-a",
            "task_id": 89,
            "workspace_id": "task-89",
            "runtime_job_id": "job-89",
        },
    )

    assert payload["accepted"] is False
    assert payload["error_code"] == "RUNNER_MATERIALIZE_FAILED"
    assert "exited before startup completed" in payload["error_message"]
    assert fake_client.containers.by_id["cid-1"].status == "removed"
    assert fake_client.containers.by_id["cid-1"].exec_commands == []
    assert harness.job_store.get_job("job-89").status == "failed"


def test_cli_returns_stable_error_code_on_invalid_config(capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                'cloud_base_url = "not-a-url"',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = app.main(["--config", str(config_path), "run"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)

    assert exit_code == app.EXIT_INVALID_CONFIG
    assert payload["error_code"] == "INVALID_CONFIG"


def test_run_subcommand_accepts_config_flag_after_command(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "runner.toml"
    expected_root = tmp_path / "runner-root"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{expected_root}"',
            ]
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    def _fake_managed_run_command(config: app.RunnerConfig) -> int:
        observed["runner_root"] = config.runner_root
        return app.EXIT_OK

    monkeypatch.setattr(app, "managed_run_command", _fake_managed_run_command)
    exit_code = app.main(["run", "--config", str(config_path)])

    assert exit_code == app.EXIT_OK
    assert observed["runner_root"] == expected_root


def test_configure_command_writes_valid_runner_toml(capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "runner.toml"

    exit_code = app.main(
        [
            "configure",
            "--config",
            str(config_path),
            "--control-plane-url",
            "http://localhost:8000",
            "--install-token",
            "rit_test_token",
            "--tenant-id",
            "42",
            "--no-tls-verify",
            "--non-interactive",
        ]
    )
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    config = app.RunnerConfig.from_toml(config_path)

    assert exit_code == app.EXIT_OK
    assert payload["status"] == "ok"
    assert config.cloud_base_url == "http://localhost:8000"
    assert config.registration_token == "rit_test_token"
    assert config.tenant_id == 42
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_configure_command_does_not_require_tenant_id(capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "enrollment.toml"

    exit_code = app.main(
        [
            "configure",
            "--config",
            str(config_path),
            "--control-plane-url",
            "http://localhost:8000",
            "--install-token",
            "rit_test_token",
            "--no-tls-verify",
            "--non-interactive",
        ]
    )
    capsys.readouterr()
    config = app.RunnerConfig.from_toml(config_path)

    assert exit_code == app.EXIT_OK
    assert config.registration_token == "rit_test_token"
    assert config.tenant_id is None
    assert "tenant_id" not in config_path.read_text(encoding="utf-8")


def test_run_dispatches_to_managed_control_plane_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'cloud_base_url = "http://localhost:8080"',
                "allow_insecure_cloud_endpoint = true",
                'registration_token = "rit_local_dev"',
            ]
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    def _fake_managed_run_command(config: app.RunnerConfig) -> int:
        calls = observed.setdefault("calls", [])
        assert isinstance(calls, list)
        calls.append(config.cloud_base_url)
        return app.EXIT_OK

    monkeypatch.setattr(app, "managed_run_command", _fake_managed_run_command)

    exit_code_run = app.main(["--config", str(config_path), "run"])

    assert exit_code_run == app.EXIT_OK
    assert observed["calls"] == ["http://localhost:8080"]
