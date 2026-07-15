"""Tests for runner-local SQLite job store semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from drowai_runner.job_store import RunnerJobStore, initialize_runner_job_store


def test_duplicate_start_is_idempotent_for_same_identity(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")

    first = store.start_job(
        runtime_job_id="job-1",
        tenant_id="tenant-a",
        task_id="17",
        workspace_id="task-17",
        image="runner:latest",
    )
    second = store.start_job(
        runtime_job_id="job-1",
        tenant_id="tenant-a",
        task_id="17",
        workspace_id="task-17",
        image="runner:latest",
    )

    assert first.runtime_job_id == second.runtime_job_id
    assert first.created_at == second.created_at


def test_duplicate_start_rejects_conflicting_identity(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-1",
        tenant_id="tenant-a",
        task_id="17",
        workspace_id="task-17",
        image="runner:latest",
    )

    with pytest.raises(ValueError, match="Conflicting runner job identity"):
        store.start_job(
            runtime_job_id="job-1",
            tenant_id="tenant-b",
            task_id="18",
            workspace_id="task-18",
            image="runner:latest",
        )


def test_duplicate_stop_is_idempotent(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-2",
        tenant_id="tenant-a",
        task_id="22",
        workspace_id="task-22",
        image="runner:latest",
    )

    first_stop = store.mark_stopped("job-2")
    second_stop = store.mark_stopped("job-2")

    assert first_stop.status == "stopped"
    assert second_stop.status == "stopped"
    assert first_stop.updated_at == second_stop.updated_at


def test_restart_recovery_loads_active_jobs_only(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite"
    store = initialize_runner_job_store(database_path)
    store.start_job(
        runtime_job_id="job-active",
        tenant_id="tenant-a",
        task_id="31",
        workspace_id="task-31",
        image="runner:latest",
    )
    store.start_job(
        runtime_job_id="job-finished",
        tenant_id="tenant-a",
        task_id="32",
        workspace_id="task-32",
        image="runner:latest",
    )
    store.mark_stopped("job-finished")

    restarted_store = RunnerJobStore(database_path)
    restarted_store.initialize()
    recovered = restarted_store.recover_active_jobs()

    assert [job.runtime_job_id for job in recovered] == ["job-active"]


def test_cleanup_transition_requires_terminal_state(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-cleanup",
        tenant_id="tenant-a",
        task_id="44",
        workspace_id="task-44",
        image="runner:latest",
    )

    with pytest.raises(ValueError, match="Cannot cleanup non-terminal"):
        store.mark_cleaned_up("job-cleanup")

    store.mark_stopped("job-cleanup", status="failed")
    cleaned = store.mark_cleaned_up("job-cleanup")

    assert cleaned.status == "cleaned_up"


def test_mark_stopped_does_not_downgrade_cleaned_up_job(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-final",
        tenant_id="tenant-a",
        task_id="45",
        workspace_id="task-45",
        image="runner:latest",
    )
    store.mark_stopped("job-final")
    store.mark_cleaned_up("job-final")

    stopped = store.mark_stopped("job-final")

    assert stopped.status == "cleaned_up"
    assert store.get_job("job-final").status == "cleaned_up"


def test_new_start_replaces_terminal_task_workspace_identity(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-old",
        tenant_id="tenant-a",
        task_id="55",
        workspace_id="task-55",
        image="runner:latest",
    )
    store.mark_stopped("job-old", status="stopped")

    replacement = store.start_job(
        runtime_job_id="job-new",
        tenant_id="tenant-a",
        task_id="55",
        workspace_id="task-55",
        image="runner:latest",
    )

    assert replacement.runtime_job_id == "job-new"
    assert replacement.status == "starting"
    assert store.find_job("job-old") is None


def test_new_start_rejects_when_existing_task_workspace_job_is_active(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-active",
        tenant_id="tenant-a",
        task_id="56",
        workspace_id="task-56",
        image="runner:latest",
    )

    with pytest.raises(ValueError, match="Conflicting runner job identity"):
        store.start_job(
            runtime_job_id="job-next",
            tenant_id="tenant-a",
            task_id="56",
            workspace_id="task-56",
            image="runner:latest",
        )
