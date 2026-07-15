"""Runner-local cleanup and orphan-retirement operations.

This module owns task-scoped container/workspace cleanup, retention snapshot
copying, and policy-controlled orphan container retirement after restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from drowai_runner.health import RunnerRecoveryReport
from drowai_runner.job_store import RunnerJobStore
from drowai_runner.workspace import RunnerWorkspaceManager


@dataclass(frozen=True, slots=True)
class CleanupErrorDetail:
    """Stable cleanup error payload surfaced to management callers."""

    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class TaskCleanupResult:
    """Result for one task cleanup run."""

    runtime_job_id: str
    status: str
    container_removed: bool
    workspace_removed: bool
    retained_paths: tuple[str, ...]
    errors: tuple[CleanupErrorDetail, ...]


@dataclass(frozen=True, slots=True)
class OrphanCleanupResult:
    """Result of orphan container retirement under a policy gate."""

    policy_enabled: bool
    removed_container_ids: tuple[str, ...]
    skipped_container_ids: tuple[str, ...]
    errors: tuple[CleanupErrorDetail, ...]


@dataclass(frozen=True, slots=True)
class RunnerCleanupService:
    """Coordinate task-local cleanup and policy-driven orphan retirement."""

    workspace_manager: RunnerWorkspaceManager
    job_store: RunnerJobStore
    remove_container: Callable[[str], None]
    cleanup_retention_hours: int
    remove_orphan_network: Callable[[str], None] | None = None

    def cleanup_task(self, runtime_job_id: str) -> TaskCleanupResult:
        """Remove one task container/workspace and mark job cleaned up on success."""
        job = self.job_store.get_job(runtime_job_id)
        if job.status == "cleaned_up":
            return TaskCleanupResult(
                runtime_job_id=runtime_job_id,
                status="ok",
                container_removed=False,
                workspace_removed=False,
                retained_paths=(),
                errors=(),
            )

        errors: list[CleanupErrorDetail] = []
        container_removed = False
        workspace_removed = False

        if job.container_id:
            try:
                self.remove_container(job.container_id)
                container_removed = True
            except Exception as exc:
                if _is_container_not_found_error(exc):
                    container_removed = True
                else:
                    errors.append(
                        CleanupErrorDetail(
                            error_code="CONTAINER_REMOVE_FAILED",
                            message=f"container_id={job.container_id} reason={exc}",
                        )
                    )

        retained_paths: tuple[str, ...] = ()
        try:
            retained_paths, workspace_removed = self._cleanup_workspace(job.workspace_id)
        except ValueError as exc:
            errors.append(
                CleanupErrorDetail(
                    error_code="WORKSPACE_SCOPE_VIOLATION",
                    message=str(exc),
                )
            )
        except OSError as exc:
            errors.append(
                CleanupErrorDetail(
                    error_code="WORKSPACE_REMOVE_FAILED",
                    message=f"workspace_id={job.workspace_id} reason={exc}",
                )
            )

        if errors:
            return TaskCleanupResult(
                runtime_job_id=runtime_job_id,
                status="failed",
                container_removed=container_removed,
                workspace_removed=workspace_removed,
                retained_paths=retained_paths,
                errors=tuple(errors),
            )

        try:
            self.job_store.mark_cleaned_up(runtime_job_id)
        except ValueError as exc:
            return TaskCleanupResult(
                runtime_job_id=runtime_job_id,
                status="failed",
                container_removed=container_removed,
                workspace_removed=workspace_removed,
                retained_paths=retained_paths,
                errors=(
                    CleanupErrorDetail(
                        error_code="JOB_STATE_INVALID",
                        message=str(exc),
                    ),
                ),
            )

        return TaskCleanupResult(
            runtime_job_id=runtime_job_id,
            status="ok",
            container_removed=container_removed,
            workspace_removed=workspace_removed,
            retained_paths=retained_paths,
            errors=(),
        )

    def cleanup_orphaned_containers(
        self,
        report: RunnerRecoveryReport,
        *,
        allow_orphan_cleanup: bool,
    ) -> OrphanCleanupResult:
        """Retire orphaned containers only when policy allows."""
        orphan_ids = tuple(entry.container_id for entry in report.orphaned)
        if not allow_orphan_cleanup:
            return OrphanCleanupResult(
                policy_enabled=False,
                removed_container_ids=(),
                skipped_container_ids=orphan_ids,
                errors=(),
            )

        removed: list[str] = []
        errors: list[CleanupErrorDetail] = []
        for container_id in orphan_ids:
            try:
                self.remove_container(container_id)
                if self.remove_orphan_network is not None:
                    orphan = next(
                        item for item in report.orphaned if item.container_id == container_id
                    )
                    self.remove_orphan_network(orphan.container_name)
                removed.append(container_id)
            except Exception as exc:
                if _is_container_not_found_error(exc):
                    removed.append(container_id)
                    continue
                errors.append(
                    CleanupErrorDetail(
                        error_code="ORPHAN_REMOVE_FAILED",
                        message=f"container_id={container_id} reason={exc}",
                    )
                )
        skipped = tuple(
            container_id for container_id in orphan_ids if container_id not in set(removed)
        )
        return OrphanCleanupResult(
            policy_enabled=True,
            removed_container_ids=tuple(removed),
            skipped_container_ids=skipped,
            errors=tuple(errors),
        )

    def _cleanup_workspace(self, workspace_id: str) -> tuple[tuple[str, ...], bool]:
        filesystem = self.workspace_manager.filesystem(workspace_id)
        try:
            filesystem.list_entries(None)
        except FileNotFoundError:
            self.workspace_manager.cleanup_task_workspace(workspace_id)
            return (), False

        retained_paths = self._retain_recent_files(workspace_id)
        self.workspace_manager.cleanup_task_workspace(workspace_id)
        return tuple(retained_paths), True

    def _retain_recent_files(self, workspace_id: str) -> list[str]:
        filesystem = self.workspace_manager.filesystem(workspace_id)
        retention_root = self.workspace_manager.runner_root / "retained" / workspace_id
        cutoff = datetime.now(tz=UTC) - timedelta(hours=self.cleanup_retention_hours)
        retained_relative_paths: list[str] = []
        for subdirectory in ("reports", "artifacts"):
            try:
                entries = filesystem.list_entries(subdirectory, recursive=True)
            except FileNotFoundError:
                continue
            for entry in entries:
                if entry.kind != "file":
                    continue
                modified_at = datetime.fromtimestamp(entry.modified_at, tz=UTC)
                if modified_at < cutoff:
                    continue
                relative_path = entry.relative_path
                destination = retention_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(filesystem.read_bytes(relative_path))
                retained_relative_paths.append(relative_path)
        return sorted(retained_relative_paths)


def _is_container_not_found_error(exc: Exception) -> bool:
    """Return true for Docker/client missing-container errors that make delete idempotent."""
    class_name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return (
        isinstance(exc, KeyError)
        or class_name == "notfound"
        or "no such container" in message
        or "not found" in message
    )
