"""Tests for runner logs/metrics, VPN, metadata, and artifact adapters."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from drowai_runner.logs_metrics import (
    ERROR_UNSUPPORTED_OPERATION,
    ERROR_UNSUPPORTED_ENV_METADATA_FILTER,
    ERROR_UNSUPPORTED_ENV_METADATA_KEY,
    RunnerLogsMetricsAdapter,
    unsupported_operation_response,
)
from drowai_runner.workspace import RunnerWorkspaceManager
from drowai_runner.job_store import initialize_runner_job_store
from runtime_shared.environment_info import ENV_INFO_FILENAME


class _FakeDockerRuntime:
    def __init__(self) -> None:
        self.logs_calls: list[tuple[str, int]] = []
        self.metrics_calls: list[str] = []
        self.probe_calls: list[tuple[str, list[str], int]] = []

    def container_status(self, container_id: str) -> str:
        return "running" if container_id == "cid-1" else "exited"

    def container_logs(self, container_id: str, *, tail: int = 200) -> str:
        self.logs_calls.append((container_id, tail))
        return f"{container_id}:tail={tail}"

    def container_metrics(self, container_id: str) -> dict[str, int]:
        self.metrics_calls.append(container_id)
        return {"memory_usage": 128, "memory_limit": 1024, "cpu_total_usage": 55}

    def exec_probe(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int = 10,
    ):
        self.probe_calls.append((container_id, command, timeout_seconds))

        class _Probe:
            exit_code = 0
            stdout = "ok"
            stderr = ""

        return _Probe()


def _build_artifact_adapter(
    tmp_path: Path,
) -> tuple[RunnerLogsMetricsAdapter, RunnerWorkspaceManager, Path]:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("task-race")
    adapter = RunnerLogsMetricsAdapter(
        job_store=initialize_runner_job_store(tmp_path / "runner-jobs.sqlite"),
        docker_runtime=_FakeDockerRuntime(),  # type: ignore[arg-type]
        workspace_manager=manager,
    )
    return adapter, manager, workspace


def test_artifact_query_size_and_digest_come_from_same_open_file(
    tmp_path: Path,
) -> None:
    adapter, manager, workspace = _build_artifact_adapter(tmp_path)
    artifact = workspace / "artifacts" / "race.txt"
    artifact.write_bytes(b"old")
    filesystem = manager.filesystem("task-race")
    original_list_entries = filesystem.list_entries

    def _list_then_replace(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entries = original_list_entries(*args, **kwargs)
        artifact.write_bytes(b"replacement-content")
        return entries

    filesystem.list_entries = _list_then_replace  # type: ignore[method-assign]
    manager.filesystem = lambda _workspace_id: filesystem  # type: ignore[method-assign]

    result = adapter.query_runtime_artifacts("task-race")

    assert result.accepted is True
    item = next(
        item
        for item in result.metadata["items"]  # type: ignore[index]
        if item["path"] == "artifacts/race.txt"
    )
    assert item["size"] == len(b"replacement-content")
    assert item["content_sha256"] == hashlib.sha256(
        b"replacement-content"
    ).hexdigest()


def test_artifact_query_maps_deletion_race_to_unsafe_response(tmp_path: Path) -> None:
    adapter, manager, workspace = _build_artifact_adapter(tmp_path)
    artifact = workspace / "artifacts" / "race.txt"
    artifact.write_bytes(b"old")
    filesystem = manager.filesystem("task-race")
    original_list_entries = filesystem.list_entries

    def _list_then_delete(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entries = original_list_entries(*args, **kwargs)
        artifact.unlink()
        return entries

    filesystem.list_entries = _list_then_delete  # type: ignore[method-assign]
    manager.filesystem = lambda _workspace_id: filesystem  # type: ignore[method-assign]

    result = adapter.query_runtime_artifacts("task-race")

    assert result.accepted is False
    assert result.error_code == "RUNNER_WORKSPACE_ENTRY_UNSAFE"


def test_logs_metrics_inventory_and_status_are_workspace_safe(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("task-1")
    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-1",
        tenant_id="tenant-a",
        task_id="1",
        workspace_id="task-1",
        image="runtime:test",
        container_id="cid-1",
    )
    store.set_last_command_id("job-1", "cmd-1")
    docker_runtime = _FakeDockerRuntime()
    adapter = RunnerLogsMetricsAdapter(
        job_store=store,
        docker_runtime=docker_runtime,  # type: ignore[arg-type]
        workspace_manager=manager,
    )

    status = adapter.get_runtime_status("job-1")
    logs = adapter.get_runtime_logs("job-1", lines=20)
    metrics = adapter.get_runtime_metrics("job-1")
    inventory = adapter.list_runtime_inventory()

    assert status.accepted is True
    assert status.metadata is not None
    assert status.metadata["workspace_id"] == "task-1"
    assert str(workspace) not in str(status.metadata)
    assert logs.metadata is not None
    assert logs.metadata["logs"][0]["message"] == "cid-1:tail=20"
    assert logs.metadata["logs"][0]["service"] == "kali-container"
    assert logs.metadata["logs"][1]["service"] == "vpn"
    assert metrics.metadata is not None
    assert metrics.metadata["metrics"]["memory_usage"] == 128
    assert inventory.metadata is not None
    assert inventory.metadata["items"] == [
        {
            "runtime_job_id": "job-1",
            "task_id": "1",
            "workspace_id": "task-1",
            "status": "starting",
            "container_id": "cid-1",
        }
    ]


def test_runtime_logs_keep_container_logs_when_vpn_probe_fails(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    manager.initialize_task_workspace("task-logs")
    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-logs",
        tenant_id="tenant-a",
        task_id="10",
        workspace_id="task-logs",
        image="runtime:test",
        container_id="cid-stopped",
    )
    docker_runtime = _FakeDockerRuntime()

    def _reject_exec(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("container is not running")

    docker_runtime.exec_probe = _reject_exec  # type: ignore[method-assign]
    adapter = RunnerLogsMetricsAdapter(
        job_store=store,
        docker_runtime=docker_runtime,  # type: ignore[arg-type]
        workspace_manager=manager,
    )

    result = adapter.get_runtime_logs("job-logs", lines=20)

    assert result.accepted is True
    assert result.metadata is not None
    assert result.metadata["logs"] == [
        {
            "timestamp": "",
            "service": "kali-container",
            "level": "info",
            "message": "cid-stopped:tail=20",
        }
    ]


def test_startup_progress_reports_paused_phase_for_paused_jobs(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    manager.initialize_task_workspace("task-2")
    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-2",
        tenant_id="tenant-a",
        task_id="2",
        workspace_id="task-2",
        image="runtime:test",
        container_id="cid-2",
    )
    store.mark_status("job-2", status="paused")
    adapter = RunnerLogsMetricsAdapter(
        job_store=store,
        docker_runtime=_FakeDockerRuntime(),  # type: ignore[arg-type]
        workspace_manager=manager,
    )

    status = adapter.get_runtime_status("job-2")
    startup = adapter.get_runtime_startup_progress("job-2")

    assert status.accepted is True
    assert status.metadata is not None
    assert status.metadata["job_status"] == "paused"
    assert startup.accepted is True
    assert startup.metadata is not None
    assert startup.metadata["startup_phase"] == "paused"


def test_vpn_env_metadata_and_artifacts_are_task_local(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("task-9")
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / "scan.txt"
    artifact_path.write_text("scan-result\n", encoding="utf-8")

    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-vpn",
        tenant_id="tenant-a",
        task_id="9",
        workspace_id="task-9",
        image="runtime:test",
        container_id="cid-9",
    )

    adapter = RunnerLogsMetricsAdapter(
        job_store=store,
        docker_runtime=_FakeDockerRuntime(),  # type: ignore[arg-type]
        workspace_manager=manager,
    )

    vpn = adapter.materialize_vpn_config("task-9", config_payload="client")
    retry = adapter.retry_vpn_connection("job-vpn")
    status = adapter.check_vpn_status("job-vpn")
    write_env = adapter.write_runtime_environment_metadata("task-9", key="agent.version", value="1.0.0")
    (workspace / ENV_INFO_FILENAME).write_text(
        '{"hostname": "runner-task", "network": {"interfaces": [], "default_gateway": "10.0.0.1"}}',
        encoding="utf-8",
    )
    query_env = adapter.query_runtime_environment_metadata("task-9", runtime_job_id="job-vpn")
    unsupported_write_env = adapter.write_runtime_environment_metadata("task-9", key="LANG", value="C.UTF-8")
    unsupported_read_env = adapter.read_runtime_environment_metadata("task-9", key="LANG")
    unsupported_query_env = adapter.query_runtime_environment_metadata("task-9", key_prefix="LANG")
    read_artifact = adapter.read_runtime_artifact_file("task-9", artifact_path="artifacts/scan.txt")
    write_artifact = adapter.write_runtime_artifact_file(
        "task-9",
        artifact_path="artifacts/generated.bin",
        content_base64=base64.b64encode(b"generated").decode("ascii"),
    )
    append_index = adapter.write_runtime_artifact_file(
        "task-9",
        artifact_path="index/chunks_task-9.jsonl",
        content='{"text":"one"}\n',
        mode="append",
    )
    append_index_again = adapter.write_runtime_artifact_file(
        "task-9",
        artifact_path="index/chunks_task-9.jsonl",
        content='{"text":"two"}\n',
        mode="append",
    )
    rejected_append_artifact = adapter.write_runtime_artifact_file(
        "task-9",
        artifact_path="artifacts/generated.bin",
        content="extra",
        mode="append",
    )
    read_artifact_binary = adapter.read_runtime_artifact_file(
        "task-9",
        artifact_path="artifacts/scan.txt",
        binary=True,
        max_bytes=4,
    )
    query_artifacts = adapter.query_runtime_artifacts("task-9")
    escaped_read = adapter.read_runtime_artifact_file("task-9", artifact_path="../outside.txt")
    vpn_read = adapter.read_runtime_artifact_file("task-9", artifact_path="vpn/task.ovpn")
    scope_read = adapter.read_runtime_artifact_file("task-9", artifact_path="scope.md")
    config_read = adapter.read_runtime_artifact_file("task-9", artifact_path="config.json")
    env_read = adapter.read_runtime_artifact_file("task-9", artifact_path=".runtime-env.json")
    empty_query = adapter.query_runtime_artifacts("task-9", prefix="")
    non_artifact_query = adapter.query_runtime_artifacts("task-9", prefix="vpn")

    assert vpn.accepted is True
    assert vpn.metadata is not None
    assert vpn.metadata["vpn_file"] == "vpn/task.ovpn"
    vpn_file = manager.resolve_task_control("task-9") / str(vpn.metadata["vpn_file"])
    assert oct(os.stat(vpn_file).st_mode & 0o777) == "0o600"

    assert retry.accepted is True
    assert status.accepted is True
    docker_runtime = adapter._docker_runtime  # type: ignore[attr-defined]
    assert docker_runtime.probe_calls  # type: ignore[attr-defined]
    retry_command = docker_runtime.probe_calls[0][1][2]  # type: ignore[index]
    assert "VPN_CONFIG=/run/drowai/control/vpn/task.ovpn" in retry_command

    assert write_env.accepted is True
    assert query_env.metadata is not None
    assert query_env.metadata["items"]["agent.version"] == "1.0.0"
    assert query_env.metadata["environment"]["hostname"] == "runner-task"
    assert query_env.metadata["environment"]["network"]["default_gateway"] == "10.0.0.1"
    assert unsupported_write_env.accepted is False
    assert unsupported_write_env.error_code == ERROR_UNSUPPORTED_ENV_METADATA_KEY
    assert unsupported_read_env.accepted is False
    assert unsupported_read_env.error_code == ERROR_UNSUPPORTED_ENV_METADATA_KEY
    assert unsupported_query_env.accepted is False
    assert unsupported_query_env.error_code == ERROR_UNSUPPORTED_ENV_METADATA_FILTER

    assert read_artifact.accepted is True
    assert read_artifact.metadata is not None
    assert read_artifact.metadata["path"] == "artifacts/scan.txt"
    assert write_artifact.accepted is True
    assert (workspace / "artifacts" / "generated.bin").read_bytes() == b"generated"
    assert append_index.accepted is True
    assert append_index_again.accepted is True
    assert (workspace / "index" / "chunks_task-9.jsonl").read_text(encoding="utf-8") == (
        '{"text":"one"}\n{"text":"two"}\n'
    )
    assert rejected_append_artifact.accepted is False
    assert rejected_append_artifact.error_code == "RUNNER_WORKSPACE_WRITE_MODE_UNSUPPORTED"
    assert read_artifact_binary.accepted is True
    assert read_artifact_binary.metadata is not None
    assert read_artifact_binary.metadata["encoding"] == "base64"
    assert read_artifact_binary.metadata["content_base64"] == base64.b64encode(b"scan").decode("ascii")
    assert query_artifacts.accepted is True
    assert query_artifacts.metadata is not None
    queried_paths = {item["path"] for item in query_artifacts.metadata["items"]}
    assert "artifacts/scan.txt" in queried_paths
    assert "artifacts/generated.bin" in queried_paths
    scan_item = next(item for item in query_artifacts.metadata["items"] if item["path"] == "artifacts/scan.txt")
    assert scan_item["content_sha256"]
    assert escaped_read.accepted is False
    assert vpn_read.accepted is False
    assert scope_read.accepted is True
    assert config_read.accepted is True
    assert env_read.accepted is True
    assert empty_query.accepted is True
    assert non_artifact_query.accepted is False


def test_unsupported_operation_response_is_stable_and_fail_closed() -> None:
    result = unsupported_operation_response(
        operation="list_runtime_inventory",
        owning_domain="runner_control",
        route_behavior="return-501",
    )

    assert result.accepted is False
    assert result.status == "rejected"
    assert result.error_code == ERROR_UNSUPPORTED_OPERATION
    assert result.metadata == {
        "operation": "list_runtime_inventory",
        "owning_domain": "runner_control",
        "route_behavior": "return-501",
    }
