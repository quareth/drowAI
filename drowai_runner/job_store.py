"""Runner-local SQLite job store for restart recovery and idempotency.

This module persists minimal runtime job metadata for the managed runner.
It intentionally stores only non-secret identity/operational fields so the
runner can recover active jobs after restart without becoming a system of
record for control-plane state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Iterator

ACTIVE_JOB_STATUSES: frozenset[str] = frozenset({"starting", "running", "paused", "stopping"})
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {"stopped", "failed", "completed", "cancelled", "cleaned_up"}
)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class RunnerJobRecord:
    """Serialized runner job record backed by `runner_jobs`."""

    runtime_job_id: str
    tenant_id: str
    task_id: str
    workspace_id: str
    status: str
    container_id: str | None
    image: str | None
    created_at: str
    updated_at: str
    last_command_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunnerJobRecord":
        return cls(
            runtime_job_id=row["runtime_job_id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            workspace_id=row["workspace_id"],
            status=row["status"],
            container_id=row["container_id"],
            image=row["image"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_command_id=row["last_command_id"],
        )


class RunnerJobStore:
    """Manage runner-local job lifecycle state in SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def initialize(self) -> None:
        """Create schema if missing."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runner_jobs (
                    runtime_job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    container_id TEXT,
                    image TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_command_id TEXT,
                    UNIQUE(tenant_id, task_id),
                    UNIQUE(workspace_id)
                )
                """
            )

    def start_job(
        self,
        *,
        runtime_job_id: str,
        tenant_id: str,
        task_id: str,
        workspace_id: str,
        image: str | None,
        container_id: str | None = None,
    ) -> RunnerJobRecord:
        """Insert a new job, or return existing record for idempotent duplicate start."""
        self._require_non_empty("runtime_job_id", runtime_job_id)
        self._require_non_empty("tenant_id", tenant_id)
        self._require_non_empty("task_id", task_id)
        self._require_non_empty("workspace_id", workspace_id)
        now = _utc_now()
        with self._connect() as connection:
            existing = self._find_job_with_connection(connection, runtime_job_id=runtime_job_id)
            if existing is not None:
                if (
                    existing.tenant_id == tenant_id
                    and existing.task_id == task_id
                    and existing.workspace_id == workspace_id
                ):
                    return existing
                raise ValueError(
                    "Conflicting runner job identity. runtime_job_id/task/workspace must be unique."
                )

            conflicting_rows = self._find_task_workspace_conflicts_with_connection(
                connection,
                tenant_id=tenant_id,
                task_id=task_id,
                workspace_id=workspace_id,
            )
            active_conflicts = [
                row
                for row in conflicting_rows
                if row.runtime_job_id != runtime_job_id and row.status in ACTIVE_JOB_STATUSES
            ]
            if active_conflicts:
                raise ValueError(
                    "Conflicting runner job identity. runtime_job_id/task/workspace must be unique."
                )

            for row in conflicting_rows:
                connection.execute(
                    "DELETE FROM runner_jobs WHERE runtime_job_id = ?",
                    (row.runtime_job_id,),
                )

            try:
                connection.execute(
                    """
                    INSERT INTO runner_jobs (
                        runtime_job_id,
                        tenant_id,
                        task_id,
                        workspace_id,
                        status,
                        container_id,
                        image,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        runtime_job_id,
                        tenant_id,
                        task_id,
                        workspace_id,
                        "starting",
                        container_id,
                        image,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "Conflicting runner job identity. runtime_job_id/task/workspace must be unique."
                ) from exc
        return self.get_job(runtime_job_id)

    def get_job(self, runtime_job_id: str) -> RunnerJobRecord:
        """Return one job or fail when missing."""
        job = self.find_job(runtime_job_id)
        if job is None:
            raise KeyError(f"Unknown runtime_job_id: {runtime_job_id}")
        return job

    def find_job(self, runtime_job_id: str) -> RunnerJobRecord | None:
        """Find one job by runtime job id."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runner_jobs WHERE runtime_job_id = ?",
                (runtime_job_id,),
            ).fetchone()
        if row is None:
            return None
        return RunnerJobRecord.from_row(row)

    def recover_active_jobs(self) -> list[RunnerJobRecord]:
        """Load jobs that may still own runtime resources after restart."""
        statuses = tuple(sorted(ACTIVE_JOB_STATUSES))
        placeholders = ", ".join(["?"] * len(statuses))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runner_jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                statuses,
            ).fetchall()
        return [RunnerJobRecord.from_row(row) for row in rows]

    def list_jobs(self) -> list[RunnerJobRecord]:
        """Return all jobs ordered by creation time for reconciliation."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runner_jobs ORDER BY created_at ASC"
            ).fetchall()
        return [RunnerJobRecord.from_row(row) for row in rows]

    def mark_stopped(self, runtime_job_id: str, *, status: str = "stopped") -> RunnerJobRecord:
        """Transition a job to a terminal status idempotently."""
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"Invalid terminal status: {status}")
        current = self.get_job(runtime_job_id)
        if current.status == "cleaned_up":
            return current
        if current.status in TERMINAL_JOB_STATUSES and current.status == status:
            return current
        return self._update_fields(runtime_job_id, status=status)

    def mark_running(self, runtime_job_id: str, *, container_id: str) -> RunnerJobRecord:
        """Mark a started job as running and persist assigned container id."""
        self._require_non_empty("container_id", container_id)
        return self._update_fields(
            runtime_job_id,
            status="running",
            container_id=container_id,
        )

    def mark_status(self, runtime_job_id: str, *, status: str) -> RunnerJobRecord:
        """Update status for non-terminal lifecycle transitions."""
        if status in TERMINAL_JOB_STATUSES:
            raise ValueError("Use mark_stopped for terminal status transitions.")
        return self._update_fields(runtime_job_id, status=status)

    def mark_cleaned_up(self, runtime_job_id: str) -> RunnerJobRecord:
        """Mark a terminal job as cleaned up after runtime resources are removed."""
        current = self.get_job(runtime_job_id)
        if current.status not in TERMINAL_JOB_STATUSES:
            raise ValueError("Cannot cleanup non-terminal job.")
        if current.status == "cleaned_up":
            return current
        return self._update_fields(runtime_job_id, status="cleaned_up")

    def set_last_command_id(self, runtime_job_id: str, command_id: str) -> RunnerJobRecord:
        """Persist last dispatched command id for idempotent replay protection."""
        self._require_non_empty("command_id", command_id)
        return self._update_fields(runtime_job_id, last_command_id=command_id)

    def _update_fields(
        self,
        runtime_job_id: str,
        *,
        status: str | None = None,
        last_command_id: str | None = None,
        container_id: str | None = None,
    ) -> RunnerJobRecord:
        assignments: list[str] = ["updated_at = ?"]
        values: list[str | None] = [_utc_now()]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if last_command_id is not None:
            assignments.append("last_command_id = ?")
            values.append(last_command_id)
        if container_id is not None:
            assignments.append("container_id = ?")
            values.append(container_id)
        values.append(runtime_job_id)

        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runner_jobs SET {', '.join(assignments)} WHERE runtime_job_id = ?",
                tuple(values),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown runtime_job_id: {runtime_job_id}")
        return self.get_job(runtime_job_id)

    def _find_job_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        runtime_job_id: str,
    ) -> RunnerJobRecord | None:
        row = connection.execute(
            "SELECT * FROM runner_jobs WHERE runtime_job_id = ?",
            (runtime_job_id,),
        ).fetchone()
        if row is None:
            return None
        return RunnerJobRecord.from_row(row)

    def _find_task_workspace_conflicts_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        task_id: str,
        workspace_id: str,
    ) -> list[RunnerJobRecord]:
        rows = connection.execute(
            """
            SELECT * FROM runner_jobs
            WHERE (tenant_id = ? AND task_id = ?)
               OR workspace_id = ?
            ORDER BY created_at ASC
            """,
            (tenant_id, task_id, workspace_id),
        ).fetchall()
        return [RunnerJobRecord.from_row(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self._database_path))
        try:
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _require_non_empty(field_name: str, value: str) -> None:
        if not value.strip():
            raise ValueError(f"{field_name} must not be empty.")


def initialize_runner_job_store(database_path: str | Path) -> RunnerJobStore:
    """Create and initialize a runner job store."""
    store = RunnerJobStore(database_path)
    store.initialize()
    return store
