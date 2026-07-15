"""Provide a thin async trigger boundary from runtime execution into ingestion.

Scope:
- Queue best-effort ingestion after runtime provenance completion.

Responsibilities:
- Run ingestion in a background worker so tool execution stays non-blocking.
- Isolate DB session lifecycle for the worker path.
- Emit enqueue-failure metric when dispatch cannot be scheduled.

Boundary:
- This module owns trigger mechanics only.
- Ingestion orchestration remains in KnowledgeIngestionService."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from backend.services.metrics.utils import safe_inc
from backend.services.notifications.task_events import schedule_projection_notification_from_result

logger = logging.getLogger(__name__)


def run_execution_ingestion_once(
    *,
    task_id: int,
    execution_id: str,
    tool_name: str,
    compact_output: Mapping[str, Any] | None,
    post_tool_candidate_payload: Mapping[str, Any] | None = None,
    post_tool_candidate_usage: Mapping[str, Any] | None = None,
    publish_loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Execute one best-effort ingestion run in a background worker."""
    db = None
    try:
        from backend.database import SessionLocal
        from .ingestion_service import KnowledgeIngestionService

        db = SessionLocal()
        ingestion_service = KnowledgeIngestionService(db)
        result = ingestion_service.ingest_execution(
            task_id=task_id,
            source_execution_id=execution_id,
            tool_name_hint=tool_name,
            compact_output_hint=dict(compact_output or {}),
            post_tool_candidate_payload=(
                dict(post_tool_candidate_payload)
                if isinstance(post_tool_candidate_payload, Mapping)
                else None
            ),
            post_tool_candidate_usage=(
                dict(post_tool_candidate_usage)
                if isinstance(post_tool_candidate_usage, Mapping)
                else None
            ),
            delete_survival_required=False,
            raise_on_error=False,
        )
        db.commit()
        schedule_projection_notification_from_result(
            task_id=task_id,
            ingestion_result=result,
            tool_name=tool_name,
            publish_loop=publish_loop,
        )
        if not bool(result.get("ok")):
            logger.warning(
                "[KNOWLEDGE_INGESTION] Background ingestion failed "
                "(task_id=%s execution_id=%s error=%s).",
                task_id,
                execution_id,
                result.get("error"),
            )
    except Exception as exc:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        logger.warning(
            "[KNOWLEDGE_INGESTION] Background ingestion worker error "
            "(task_id=%s execution_id=%s): %s",
            task_id,
            execution_id,
            exc,
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def enqueue_execution_ingestion(
    *,
    task_id: int,
    execution_id: str,
    tool_name: str,
    compact_output: Mapping[str, Any] | None,
    post_tool_candidate_payload: Mapping[str, Any] | None = None,
    post_tool_candidate_usage: Mapping[str, Any] | None = None,
) -> None:
    """Queue non-blocking ingestion from the live LangGraph execution seam."""
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: run_execution_ingestion_once(
                task_id=task_id,
                execution_id=execution_id,
                tool_name=tool_name,
                compact_output=compact_output,
                post_tool_candidate_payload=post_tool_candidate_payload,
                post_tool_candidate_usage=post_tool_candidate_usage,
                publish_loop=loop,
            ),
        )
    except Exception as exc:
        logger.warning(
            "[KNOWLEDGE_INGESTION] Failed to enqueue ingestion "
            "(task_id=%s execution_id=%s): %s",
            task_id,
            execution_id,
            exc,
        )
        safe_inc("knowledge_ingestion_enqueue_failures")
