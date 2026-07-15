"""Tests for runner recovery classification across job store and Docker state."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.health import RunnerRecoveryHealthService
from drowai_runner.job_store import initialize_runner_job_store


class _FakeContainer:
    def __init__(self, container_id: str, *, name: str, status: str) -> None:
        self.id = container_id
        self.name = name
        self.status = status

    def reload(self) -> None:
        return None


class _FakeContainers:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._by_id = {container.id: container for container in containers}
        self._containers = list(containers)

    def get(self, container_id: str) -> _FakeContainer:
        if container_id not in self._by_id:
            raise KeyError(container_id)
        return self._by_id[container_id]

    def list(self, *, all: bool) -> list[_FakeContainer]:
        assert all is True
        return list(self._containers)


class _FakeDockerClient:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self.containers = _FakeContainers(containers)


def test_recovery_report_classifies_active_missing_stopped_and_orphaned(
    tmp_path: Path,
) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-active",
        tenant_id="tenant-a",
        task_id="1",
        workspace_id="task-1",
        image="runtime:test",
        container_id="cid-active",
    )
    store.start_job(
        runtime_job_id="job-missing",
        tenant_id="tenant-a",
        task_id="2",
        workspace_id="task-2",
        image="runtime:test",
        container_id="cid-missing",
    )
    store.start_job(
        runtime_job_id="job-stopped",
        tenant_id="tenant-a",
        task_id="3",
        workspace_id="task-3",
        image="runtime:test",
        container_id="cid-stopped",
    )
    store.start_job(
        runtime_job_id="job-finished",
        tenant_id="tenant-a",
        task_id="4",
        workspace_id="task-4",
        image="runtime:test",
        container_id="cid-finished",
    )
    store.mark_stopped("job-finished")

    fake_client = _FakeDockerClient(
        [
            _FakeContainer("cid-active", name="drowai-tenant-a-task-1", status="running"),
            _FakeContainer("cid-stopped", name="drowai-tenant-a-task-3", status="exited"),
            _FakeContainer("cid-orphan", name="drowai-tenant-a-task-99", status="running"),
            _FakeContainer("cid-other", name="third-party", status="running"),
            _FakeContainer("cid-finished", name="drowai-tenant-a-task-4", status="exited"),
        ]
    )
    report = RunnerRecoveryHealthService(
        job_store=store,
        docker_client_factory=lambda: fake_client,
    ).build_report()

    assert [item.runtime_job_id for item in report.active] == ["job-active"]
    assert [(item.runtime_job_id, item.reason) for item in report.missing] == [
        ("job-missing", "CONTAINER_NOT_FOUND")
    ]
    assert [item.runtime_job_id for item in report.stopped] == ["job-stopped"]
    assert [(item.container_id, item.container_name) for item in report.orphaned] == [
        ("cid-orphan", "drowai-tenant-a-task-99")
    ]
