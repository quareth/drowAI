"""Runner recovery health reporting for cleanup/restart workflows.

This module compares runner job-store records with Docker container state and
reports active, missing, stopped, and orphaned runtime containers so restart
recovery can be policy-driven and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from drowai_runner.job_store import RunnerJobRecord, RunnerJobStore

_ACTIVE_CONTAINER_STATES = frozenset({"created", "running", "restarting"})


@dataclass(frozen=True, slots=True)
class RecoveryJobStatus:
    """Container status projection for one active runner job."""

    runtime_job_id: str
    task_id: str
    container_id: str
    container_status: str


@dataclass(frozen=True, slots=True)
class MissingContainerStatus:
    """Missing container projection for one active runner job."""

    runtime_job_id: str
    task_id: str
    container_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class OrphanContainerStatus:
    """Container discovered outside current job-store ownership."""

    container_id: str
    container_name: str


@dataclass(frozen=True, slots=True)
class RunnerRecoveryReport:
    """Structured recovery classification used by cleanup/reconciliation flows."""

    active: tuple[RecoveryJobStatus, ...]
    missing: tuple[MissingContainerStatus, ...]
    stopped: tuple[RecoveryJobStatus, ...]
    orphaned: tuple[OrphanContainerStatus, ...]


@dataclass(frozen=True, slots=True)
class RunnerRecoveryHealthService:
    """Build recovery state by reconciling job-store and Docker inventories."""

    job_store: RunnerJobStore
    docker_client_factory: Callable[[], Any]
    container_name_prefix: str = "drowai-"

    def build_report(self) -> RunnerRecoveryReport:
        """Return active/missing/stopped/orphaned container classifications."""
        active_jobs = self.job_store.recover_active_jobs()
        inventory = self._list_runner_container_inventory()
        tracked_container_ids = {
            job.container_id for job in self.job_store.list_jobs() if job.container_id
        }

        active: list[RecoveryJobStatus] = []
        missing: list[MissingContainerStatus] = []
        stopped: list[RecoveryJobStatus] = []

        for job in active_jobs:
            classification = self._classify_job(job)
            if isinstance(classification, MissingContainerStatus):
                missing.append(classification)
            elif classification.container_status in _ACTIVE_CONTAINER_STATES:
                active.append(classification)
            else:
                stopped.append(classification)

        orphaned = [
            OrphanContainerStatus(container_id=container_id, container_name=container_name)
            for container_id, container_name in sorted(inventory.items())
            if container_id not in tracked_container_ids
        ]

        return RunnerRecoveryReport(
            active=tuple(sorted(active, key=lambda item: item.runtime_job_id)),
            missing=tuple(sorted(missing, key=lambda item: item.runtime_job_id)),
            stopped=tuple(sorted(stopped, key=lambda item: item.runtime_job_id)),
            orphaned=tuple(orphaned),
        )

    def _classify_job(self, job: RunnerJobRecord) -> RecoveryJobStatus | MissingContainerStatus:
        if not job.container_id:
            return MissingContainerStatus(
                runtime_job_id=job.runtime_job_id,
                task_id=job.task_id,
                container_id=None,
                reason="CONTAINER_ID_MISSING",
            )

        status = self._safe_container_status(job.container_id)
        if status is None:
            return MissingContainerStatus(
                runtime_job_id=job.runtime_job_id,
                task_id=job.task_id,
                container_id=job.container_id,
                reason="CONTAINER_NOT_FOUND",
            )

        return RecoveryJobStatus(
            runtime_job_id=job.runtime_job_id,
            task_id=job.task_id,
            container_id=job.container_id,
            container_status=status,
        )

    def _safe_container_status(self, container_id: str) -> str | None:
        try:
            container = self.docker_client_factory().containers.get(container_id)
            container.reload()
        except Exception:
            return None
        return str(getattr(container, "status", "unknown"))

    def _list_runner_container_inventory(self) -> dict[str, str]:
        try:
            containers = self.docker_client_factory().containers.list(all=True)
        except Exception:
            return {}

        inventory: dict[str, str] = {}
        for container in containers:
            container_name = _container_name(container)
            if not container_name.startswith(self.container_name_prefix):
                continue
            container_id = str(getattr(container, "id", "")).strip()
            if container_id:
                inventory[container_id] = container_name
        return inventory


def _container_name(container: Any) -> str:
    name = str(getattr(container, "name", "")).strip()
    if name:
        return name.lstrip("/")

    attrs = getattr(container, "attrs", {})
    if isinstance(attrs, dict):
        name_value = attrs.get("Name")
        if isinstance(name_value, str):
            return name_value.lstrip("/")
    return ""
