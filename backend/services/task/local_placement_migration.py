"""Startup reconciliation for product tasks left on local runtime placement.

This module owns the fail-closed safety transition for active tasks that were
created before product profiles required runner placement. It only updates task
metadata and audit history; runtime/workspace cleanup remains out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, TaskHistory
from backend.services.runtime_provider.contracts import RuntimePlacementMode

PRODUCT_PROFILES = frozenset({"single_host", "distributed"})
PRODUCT_LOCAL_RUNTIME_REJECTED = "PRODUCT_LOCAL_RUNTIME_REJECTED"


@dataclass(frozen=True, slots=True)
class LocalPlacementStartupResult:
    """Summary of startup reconciliation for active local-placement tasks."""

    changed_count: int
    task_ids: tuple[int, ...]
    message: str


def fail_closed_active_local_placement_tasks(
    db: Session,
    *,
    deployment_profile: str,
) -> LocalPlacementStartupResult:
    """Mark active local-placement tasks failed in product deployment profiles."""
    normalized_profile = str(deployment_profile or "").strip().lower()
    if normalized_profile not in PRODUCT_PROFILES:
        return LocalPlacementStartupResult(
            changed_count=0,
            task_ids=(),
            message=(
                "Local-placement startup reconciliation skipped because "
                f"DROWAI_DEPLOYMENT_PROFILE={normalized_profile or '<unset>'} is not a product profile."
            ),
        )

    tasks = (
        db.query(Task)
        .filter(
            Task.status.in_(tuple(TaskStatus.active_task_statuses())),
            func.lower(func.trim(Task.runtime_placement_mode)) == RuntimePlacementMode.LOCAL.value,
        )
        .all()
    )
    if not tasks:
        return LocalPlacementStartupResult(
            changed_count=0,
            task_ids=(),
            message=(
                "No active local-placement tasks found for product profile "
                f"{normalized_profile}."
            ),
        )

    task_ids: list[int] = []
    now = utc_now()
    for task in tasks:
        task_id = int(task.id)
        old_status = str(task.status)
        task_ids.append(task_id)
        message = _rejection_message(
            deployment_profile=normalized_profile,
            task_id=task_id,
        )
        db.add(
            TaskHistory(
                task_id=task_id,
                tenant_id=int(task.tenant_id),
                user_id=None,
                old_status=old_status,
                new_status=TaskStatus.FAILED.value,
                transition_reason=message,
                change_source="system",
                change_metadata={
                    "reason_code": PRODUCT_LOCAL_RUNTIME_REJECTED,
                    "deployment_profile": normalized_profile,
                    "runtime_placement_mode": RuntimePlacementMode.LOCAL.value,
                    "startup_reconciliation": True,
                    "workspace_deleted": False,
                    "runtime_files_deleted": False,
                },
            )
        )
        task.status = TaskStatus.FAILED.value
        task.error_message = message
        task.failure_reason = PRODUCT_LOCAL_RUNTIME_REJECTED
        task.stopped_at = now

    return LocalPlacementStartupResult(
        changed_count=len(task_ids),
        task_ids=tuple(task_ids),
        message=(
            f"Marked {len(task_ids)} active local-placement task(s) failed for "
            f"DROWAI_DEPLOYMENT_PROFILE={normalized_profile}. "
            f"reason_code={PRODUCT_LOCAL_RUNTIME_REJECTED}; task_ids={task_ids}. "
            "Recreate or restart affected work through runner placement after a healthy runner is enrolled."
        ),
    )


def _rejection_message(*, deployment_profile: str, task_id: int) -> str:
    return (
        f"{PRODUCT_LOCAL_RUNTIME_REJECTED}: task {task_id} used local runtime placement, "
        f"which is not executable in DROWAI_DEPLOYMENT_PROFILE={deployment_profile}. "
        "Recreate or restart the task with runner placement after a healthy runner is enrolled."
    )


__all__ = [
    "LocalPlacementStartupResult",
    "PRODUCT_LOCAL_RUNTIME_REJECTED",
    "fail_closed_active_local_placement_tasks",
]
