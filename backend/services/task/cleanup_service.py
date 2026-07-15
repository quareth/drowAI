"""Task cleanup orchestration service.

Responsibilities:
- Keep hard-delete semantics separate from stop/retirement lifecycle transitions.
- Delegate runtime artifact teardown to the runtime retirement service.
- Delete related persistence rows in dependency-safe order.
- Enforce durable-knowledge delete preflight before irreversible cleanup.

Contract boundary:
- This service is only for irreversible task deletion.
- Engagement archiving is governed by runtime-active task status checks, not by deleting all tasks.
"""

from __future__ import annotations

import logging
import inspect

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import E2E_DETERMINISTIC_MODE, E2E_RUNTIME_LOCAL_MODE
from backend.services.runtime_provider.contracts import RuntimeCallScope
from ..knowledge.ingestion_service import KnowledgeIngestionService
from .access_service import get_owned_task_or_404
from .graph_state_cleanup_service import TaskGraphStateCleanupService
from .retirement_service import TaskRetirementService

logger = logging.getLogger(__name__)


def resolve_task_delete_runtime_scope(*, deterministic_mode: bool) -> RuntimeCallScope:
    """Keep local fixture retirement test-scoped without changing production policy."""
    return RuntimeCallScope.TEST if deterministic_mode else RuntimeCallScope.PRODUCT_TASK


class TaskCleanupService:
    """Service for irreversible task deletion and durable row cleanup."""

    def __init__(
        self,
        db: Session,
        *,
        knowledge_ingestion_service: KnowledgeIngestionService | None = None,
        graph_state_cleanup_service: TaskGraphStateCleanupService | None = None,
    ):
        self.db = db
        self.knowledge_ingestion_service = knowledge_ingestion_service
        self.graph_state_cleanup_service = graph_state_cleanup_service

    async def delete_task(
        self,
        task_id: int,
        user_id: int,
        *,
        tenant_id: int,
    ) -> dict[str, str]:
        """Hard-delete task rows after runtime retirement succeeds."""
        try:
            resolved_tenant_id = int(tenant_id)
            task = get_owned_task_or_404(
                db=self.db,
                task_id=task_id,
                user_id=int(user_id),
                tenant_id=resolved_tenant_id,
            )
            engagement_id = getattr(task, "engagement_id", None)

            logger.info(
                "Starting deletion of task %s for user %s in tenant %s",
                task_id,
                user_id,
                resolved_tenant_id,
            )
            self._enforce_delete_safety_preflight(task_id=task_id, engagement_id=engagement_id)
            try:
                retirement_service = TaskRetirementService()
                retire_kwargs = {"task_id": task_id, "engagement_id": engagement_id}
                try:
                    signature = inspect.signature(retirement_service.retire_runtime)
                    supports_kwargs = any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                    if "user_id" in signature.parameters:
                        retire_kwargs["user_id"] = user_id
                    if "runtime_call_scope" in signature.parameters or supports_kwargs:
                        retire_kwargs["runtime_call_scope"] = resolve_task_delete_runtime_scope(
                            deterministic_mode=(E2E_DETERMINISTIC_MODE or E2E_RUNTIME_LOCAL_MODE),
                        )
                except (TypeError, ValueError):
                    retire_kwargs["user_id"] = user_id
                    retire_kwargs["runtime_call_scope"] = resolve_task_delete_runtime_scope(
                        deterministic_mode=(E2E_DETERMINISTIC_MODE or E2E_RUNTIME_LOCAL_MODE),
                    )
                retirement_result = await retirement_service.retire_runtime(**retire_kwargs)
                if not retirement_result.success:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=retirement_result.message,
                    )

                graph_cleanup = self.graph_state_cleanup_service or TaskGraphStateCleanupService(self.db)
                await graph_cleanup.cleanup_task_graph_state(
                    task_id=task_id,
                    graph_thread_id=str(getattr(task, "graph_thread_id", "") or ""),
                )

                self.db.execute(text("DELETE FROM agent_logs WHERE task_id = :task_id"), {"task_id": task_id})

                try:
                    self.db.execute(
                        text("DELETE FROM llm_conversations WHERE task_id = :task_id"),
                        {"task_id": task_id},
                    )
                except Exception:
                    logger.debug("LLM conversations table cleanup skipped (table missing?)", exc_info=True)

                self.db.execute(text("DELETE FROM task_history WHERE task_id = :task_id"), {"task_id": task_id})

                delete_result = self.db.execute(
                    text(
                        "DELETE FROM tasks "
                        "WHERE id = :task_id AND tenant_id = :tenant_id AND user_id = :user_id"
                    ),
                    {"task_id": task_id, "tenant_id": resolved_tenant_id, "user_id": int(user_id)},
                )

                if delete_result.rowcount == 0:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Task not found or access denied",
                    )

                self.db.commit()
            except HTTPException:
                self.db.rollback()
                raise
            except Exception as runtime_cleanup_error:
                logger.error(
                    "Runtime cleanup phase failed for task %s: %s",
                    task_id,
                    runtime_cleanup_error,
                )
                self.db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Failed to complete runtime cleanup during task deletion: "
                        f"{runtime_cleanup_error}"
                    ),
                )

            logger.info("Successfully deleted task %s with all associated resources", task_id)
            return {"message": "Task and container deleted successfully"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Unexpected error deleting task %s: %s", task_id, e)
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete task: {str(e)}",
            )

    def _enforce_delete_safety_preflight(
        self,
        *,
        task_id: int,
        engagement_id: int | None,
    ) -> None:
        """Run delete-safety preflight and block deletion when evidence is unsafe."""
        ingestion = self.knowledge_ingestion_service or KnowledgeIngestionService(self.db)
        decision = ingestion.ensure_task_delete_safe(
            task_id=int(task_id),
            engagement_id=engagement_id,
        )
        if bool(decision.get("safe")):
            return

        reason = str(
            decision.get("reason")
            or "Durable knowledge preservation preflight reported unsafe state"
        ).strip()
        catchup_attempted = bool(decision.get("catchup_attempted"))
        unsafe_ids = [
            str(item).strip()
            for item in list(decision.get("unsafe_execution_ids") or [])
            if str(item).strip()
        ]
        logger.warning(
            (
                "Task delete preflight blocked deletion "
                "(task_id=%s engagement_id=%s reason=%s catchup_attempted=%s "
                "unsafe_execution_ids=%s)"
            ),
            task_id,
            engagement_id,
            reason,
            catchup_attempted,
            ",".join(unsafe_ids),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=reason,
        )
