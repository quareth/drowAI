"""Tests for runner cleanup safety, failure reporting, and orphan policy."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from drowai_runner.app import cleanup_runtime_command
from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.config import RunnerConfig
from drowai_runner.health import OrphanContainerStatus, RunnerRecoveryReport
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.workspace import RunnerWorkspaceManager


class _FakeContainer:
    def __init__(self, container_id: str) -> None:
        self.id = container_id
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 10) -> None:
        del timeout
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        del force
        self.removed = True


class _FakeContainers:
    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        self._containers = containers

    def get(self, container_id: str) -> _FakeContainer:
        if container_id not in self._containers:
            raise KeyError(container_id)
        return self._containers[container_id]


class _FakeNetworks:
    """Represent an empty Docker network inventory for cleanup tests."""

    def get(self, network_name: str):
        raise KeyError(network_name)


class _FakeDockerClient:
    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        self.containers = _FakeContainers(containers)
        self.networks = _FakeNetworks()


def test_cleanup_stays_task_local_and_rejects_outside_runner_root(tmp_path: Path) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-1",
        tenant_id="tenant-a",
        task_id="1",
        workspace_id="../outside",
        image="runtime:test",
        container_id="cid-1",
    )
    store.mark_stopped("job-1")

    outside_file = tmp_path / "outside" / "do-not-delete.txt"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_text("keep", encoding="utf-8")

    removed_container_ids: list[str] = []
    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=lambda container_id: removed_container_ids.append(container_id),
        cleanup_retention_hours=24,
    )
    result = service.cleanup_task("job-1")

    assert result.status == "failed"
    assert result.container_removed is True
    assert result.workspace_removed is False
    assert result.errors[0].error_code == "WORKSPACE_SCOPE_VIOLATION"
    assert outside_file.read_text(encoding="utf-8") == "keep"
    assert store.get_job("job-1").status == "stopped"


def test_cleanup_failure_returns_stable_error_and_does_not_corrupt_job_store(
    tmp_path: Path,
) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace_manager.initialize_task_workspace("task-5")

    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-2",
        tenant_id="tenant-a",
        task_id="5",
        workspace_id="task-5",
        image="runtime:test",
        container_id="cid-fail",
    )
    store.mark_stopped("job-2")

    def _remove_container(_container_id: str) -> None:
        raise RuntimeError("docker remove failed")

    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=_remove_container,
        cleanup_retention_hours=24,
    )
    result = service.cleanup_task("job-2")

    assert result.status == "failed"
    assert result.errors[0].error_code == "CONTAINER_REMOVE_FAILED"
    assert result.workspace_removed is True
    assert store.get_job("job-2").status == "stopped"
    assert not (workspace_manager.tasks_root / "task-5").exists()


def test_cleanup_treats_missing_container_as_already_removed(tmp_path: Path) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace_manager.initialize_task_workspace("task-6")
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-missing-container",
        tenant_id="tenant-a",
        task_id="6",
        workspace_id="task-6",
        image="runtime:test",
        container_id="cid-missing",
    )
    store.mark_stopped("job-missing-container")

    def _remove_container(_container_id: str) -> None:
        raise KeyError("No such container: cid-missing")

    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=_remove_container,
        cleanup_retention_hours=24,
    )

    result = service.cleanup_task("job-missing-container")

    assert result.status == "ok"
    assert result.container_removed is True
    assert result.workspace_removed is True
    assert result.errors == ()
    assert store.get_job("job-missing-container").status == "cleaned_up"


def test_cleanup_removes_orphaned_control_when_data_workspace_is_missing(
    tmp_path: Path,
) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace_manager.initialize_task_workspace("task-orphan-control")
    sibling_control = workspace_manager.initialize_task_control("task-sibling")
    orphan_control = workspace_manager.resolve_task_control("task-orphan-control")
    workspace_manager.filesystem("task-orphan-control").remove(
        "scope.md", missing_ok=True
    )
    workspace_manager.cleanup_task_workspace("task-orphan-control")
    workspace_manager.initialize_task_control("task-orphan-control")
    assert not (
        workspace_manager.tasks_root / "task-orphan-control"
    ).exists()

    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-orphan-control",
        tenant_id="tenant-a",
        task_id="8",
        workspace_id="task-orphan-control",
        image="runtime:test",
        container_id=None,
    )
    store.mark_stopped("job-orphan-control")
    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=lambda _container_id: None,
        cleanup_retention_hours=24,
    )

    result = service.cleanup_task("job-orphan-control")

    assert result.status == "ok"
    assert not orphan_control.exists()
    assert sibling_control.exists()


def test_cleanup_runtime_command_retires_active_job(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runner_root = tmp_path / "runner-root"
    workspace_manager = RunnerWorkspaceManager(runner_root)
    workspace_manager.initialize_task_workspace("task-5")
    store = initialize_runner_job_store(runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-active",
        tenant_id="1",
        task_id="5",
        workspace_id="task-5",
        image="runtime:test",
        container_id="cid-active",
    )
    store.mark_running("job-active", container_id="cid-active")
    fake_container = _FakeContainer("cid-active")

    monkeypatch.setattr(
        "drowai_runner.app._docker_client_factory",
        lambda: _FakeDockerClient({"cid-active": fake_container}),
    )
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_DEV_MODE": "true",
            "DROWAI_RUNNER_ROOT": str(runner_root),
            "DROWAI_RUNNER_RUNTIME_IMAGE": "runtime:test",
        }
    )

    exit_code = cleanup_runtime_command(
        config,
        SimpleNamespace(runtime_job_id=None, task_id=5, tenant_id="1"),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"status": "ok"' in output
    assert fake_container.stopped is True
    assert fake_container.removed is True
    assert store.get_job("job-active").status == "cleaned_up"
    assert not (workspace_manager.tasks_root / "task-5").exists()


def test_cleanup_is_idempotent_for_already_cleaned_job(tmp_path: Path) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-cleaned",
        tenant_id="tenant-a",
        task_id="7",
        workspace_id="task-7",
        image="runtime:test",
        container_id="cid-cleaned",
    )
    store.mark_stopped("job-cleaned")
    store.mark_cleaned_up("job-cleaned")

    def _remove_container(_container_id: str) -> None:
        raise AssertionError("cleanup should not retry container removal")

    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=_remove_container,
        cleanup_retention_hours=24,
    )

    result = service.cleanup_task("job-cleaned")

    assert result.status == "ok"
    assert result.container_removed is False
    assert result.workspace_removed is False
    assert result.errors == ()
    assert store.get_job("job-cleaned").status == "cleaned_up"


def test_orphan_cleanup_respects_policy_gate(tmp_path: Path) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    removed_container_ids: list[str] = []
    service = RunnerCleanupService(
        workspace_manager=workspace_manager,
        job_store=store,
        remove_container=lambda container_id: removed_container_ids.append(container_id),
        cleanup_retention_hours=24,
    )
    report = RunnerRecoveryReport(
        active=(),
        missing=(),
        stopped=(),
        orphaned=(OrphanContainerStatus("cid-orphan", "drowai-tenant-a-task-9"),),
    )

    skipped = service.cleanup_orphaned_containers(report, allow_orphan_cleanup=False)
    removed = service.cleanup_orphaned_containers(report, allow_orphan_cleanup=True)

    assert skipped.policy_enabled is False
    assert skipped.skipped_container_ids == ("cid-orphan",)
    assert removed.policy_enabled is True
    assert removed.removed_container_ids == ("cid-orphan",)
    assert removed_container_ids == ["cid-orphan"]
