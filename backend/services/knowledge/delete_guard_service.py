"""Enforce task-delete durability preconditions for knowledge ingestion.

Scope:
- Verify whether a task can be safely deleted without losing durable evidence.

Responsibilities:
- Detect executions missing successful durable ingestion/archive state.
- Run best-effort catch-up ingestion for unsafe executions.
- Re-check safety after catch-up and return a clear decision payload.

Boundary:
- This service only owns delete-safety checks and catch-up orchestration.
- It does not own ingestion run creation or archive policy decisions."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store

from ...models import (
    ExecutionArtifact,
    KnowledgeEvidenceArchive,
    KnowledgeIngestionRun,
    ToolExecution,
)
from .contracts import IngestionRunStatus
from .evidence_storage_service import is_runner_backed_artifact


class KnowledgeDeleteGuardService:
    """Validate delete safety and run catch-up ingestion when required."""

    def __init__(
        self,
        db: Session,
        *,
        ingest_execution: Callable[..., dict[str, Any]],
        object_store: ObjectStore | None = None,
    ) -> None:
        self.db = db
        self.ingest_execution = ingest_execution
        self.object_store = object_store or get_object_store()

    def ensure_task_delete_safe(
        self,
        *,
        task_id: int,
        engagement_id: int | None,
    ) -> dict[str, object]:
        """
        Ensure durable ingestion/archive state exists before task runtime deletion.

        Returns:
        - safe: bool
        - catchup_attempted: bool
        - unsafe_execution_ids: list[str]
        - reason: str
        """
        execution_ids = [
            str(execution_id)
            for execution_id in self.db.execute(
                select(ToolExecution.id).where(ToolExecution.task_id == int(task_id))
            ).scalars()
        ]
        if not execution_ids:
            return {
                "safe": True,
                "catchup_attempted": False,
                "unsafe_execution_ids": [],
                "reason": "No executions to protect",
            }

        if engagement_id is None:
            return {
                "safe": False,
                "catchup_attempted": False,
                "unsafe_execution_ids": execution_ids,
                "reason": "Task has no engagement_id; cannot preserve durable knowledge ownership",
            }

        initial_unsafe = self._find_unsafe_execution_ids(
            task_id=task_id,
            engagement_id=int(engagement_id),
            execution_ids=execution_ids,
        )
        if not initial_unsafe:
            return {
                "safe": True,
                "catchup_attempted": False,
                "unsafe_execution_ids": [],
                "reason": "Durable ingestion state already complete",
            }

        self._run_delete_guard_catchup(
            task_id=task_id,
            engagement_id=int(engagement_id),
            execution_ids=initial_unsafe,
        )
        remaining_unsafe = self._find_unsafe_execution_ids(
            task_id=task_id,
            engagement_id=int(engagement_id),
            execution_ids=execution_ids,
        )
        if not remaining_unsafe:
            return {
                "safe": True,
                "catchup_attempted": True,
                "unsafe_execution_ids": [],
                "reason": "Catch-up ingestion completed successfully",
            }
        return {
            "safe": False,
            "catchup_attempted": True,
            "unsafe_execution_ids": remaining_unsafe,
            "reason": (
                "Delete blocked: durable evidence ingestion/archive is incomplete for executions: "
                + ", ".join(remaining_unsafe)
            ),
        }

    def _run_delete_guard_catchup(
        self,
        *,
        task_id: int,
        engagement_id: int,
        execution_ids: list[str],
    ) -> None:
        for execution_id in execution_ids:
            self.ingest_execution(
                task_id=task_id,
                engagement_id=engagement_id,
                source_execution_id=execution_id,
                extractor_family="knowledge.delete_guard",
                extractor_version="1.0",
                delete_survival_required=True,
                raise_on_error=False,
            )

    def _find_unsafe_execution_ids(
        self,
        *,
        task_id: int,
        engagement_id: int,
        execution_ids: list[str],
    ) -> list[str]:
        unsafe: list[str] = []
        for execution_id in execution_ids:
            if not self._has_succeeded_run(
                engagement_id=engagement_id,
                source_execution_id=execution_id,
            ):
                unsafe.append(execution_id)
                continue

            artifact_rows = self.db.execute(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.task_id == int(task_id),
                    ExecutionArtifact.execution_id == execution_id,
                )
            ).scalars().all()
            if not artifact_rows:
                continue

            archived_rows = self.db.execute(
                select(KnowledgeEvidenceArchive).where(
                    KnowledgeEvidenceArchive.engagement_id == int(engagement_id),
                    KnowledgeEvidenceArchive.source_execution_id == execution_id,
                )
            ).scalars().all()
            archived_by_artifact = {
                str(row.source_artifact_id): row for row in archived_rows if row.source_artifact_id is not None
            }

            execution_safe = True
            for artifact in artifact_rows:
                artifact_id = str(artifact.id)
                row = archived_by_artifact.get(artifact_id)
                if row is None:
                    execution_safe = False
                    break
                if not self._is_delete_safe_archive(
                    artifact=artifact,
                    row=row,
                ):
                    execution_safe = False
                    break
            if not execution_safe:
                unsafe.append(execution_id)
        return unsafe

    def _has_succeeded_run(self, *, engagement_id: int, source_execution_id: str) -> bool:
        row = self.db.execute(
            select(KnowledgeIngestionRun.id).where(
                KnowledgeIngestionRun.engagement_id == int(engagement_id),
                KnowledgeIngestionRun.source_execution_id == str(source_execution_id),
                KnowledgeIngestionRun.status == IngestionRunStatus.SUCCEEDED.value,
            )
        ).first()
        return row is not None

    def _is_delete_safe_archive(
        self,
        *,
        artifact: ExecutionArtifact,
        row: KnowledgeEvidenceArchive,
    ) -> bool:
        storage_mode = str(row.storage_mode or "").strip().lower()
        if storage_mode == "inline_excerpt":
            return row.inline_excerpt is not None
        if storage_mode == "object_ref":
            return self._is_safe_object_ref_archive(artifact=artifact, row=row)
        if storage_mode != "archived_file":
            return False

        ref = str(row.archived_file_ref or "").strip()
        if not ref or ref.startswith("pending://"):
            return False
        if "://" in ref:
            return True
        try:
            archived_path = Path(ref)
        except Exception:
            return False
        return archived_path.exists() and archived_path.is_file()

    def _is_safe_object_ref_archive(
        self,
        *,
        artifact: ExecutionArtifact,
        row: KnowledgeEvidenceArchive,
    ) -> bool:
        if str(artifact.upload_status or "").strip().lower() == "upload_pending":
            return False

        if is_runner_backed_artifact(
            upload_status=artifact.upload_status,
            object_key=artifact.object_key,
        ) and not str(row.object_key or "").strip():
            return False

        object_key = str(row.object_key or "").strip()
        if not object_key:
            return False

        try:
            object_head = self.object_store.head_object(object_key)
        except Exception:
            return False
        if object_head is None:
            return False

        expected_hash = (
            str(row.content_sha256 or "").strip().lower()
            or str(artifact.content_sha256 or "").strip().lower()
        )
        if not expected_hash:
            return True

        actual_hash = str(object_head.content_sha256 or "").strip().lower()
        if not actual_hash:
            try:
                actual_hash = sha256(self.object_store.read_bytes(object_key)).hexdigest().lower()
            except Exception:
                return False
        return actual_hash == expected_hash
