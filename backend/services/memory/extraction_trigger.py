"""Provide non-blocking trigger mechanics for background memory extraction.

This module owns enqueue/worker boundaries and DB session lifecycle only. It
delegates live LLM and embedding construction to the memory runtime service.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from sqlalchemy.orm import Session
from backend.config.feature_flags import is_semantic_memory_runtime_enabled
from backend.services.metrics.utils import safe_inc

logger = logging.getLogger(__name__)


def _build_memory_runtime_service(db: Session) -> Any:
    """Build the backend-owned live memory runtime service for one worker run."""

    from backend.services.llm_provider.runtime_config_service import LLMRuntimeConfigService

    runtime_services = LLMRuntimeConfigService(db).build_runtime_services()
    return runtime_services.memory_runtime_service


def _coerce_runtime_selection(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a JSON-safe runtime selection snapshot when it is complete."""

    if not isinstance(value, Mapping):
        return None
    credential_ref = value.get("credential_ref")
    if not isinstance(credential_ref, Mapping):
        return None
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or "").strip()
    if not provider or not model:
        return None
    return {
        "provider": provider,
        "model": model,
        "credential_ref": dict(credential_ref),
        "reasoning_effort": value.get("reasoning_effort"),
    }


def run_memory_extraction_once(
    *,
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    task_id: int | None,
    conversation_id: str | None,
    turn_id: str | None,
    llm_runtime_selection: Mapping[str, Any] | None = None,
) -> None:
    """Execute one best-effort memory extraction in a background worker."""
    if not is_semantic_memory_runtime_enabled():
        return
    if user_id is None:
        return
    runtime_selection = _coerce_runtime_selection(llm_runtime_selection)
    if runtime_selection is None:
        logger.info(
            "[MEMORY_EXTRACTION] Skipping background extraction: missing runtime selection snapshot"
        )
        return

    db = None
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        memory_runtime_service = _build_memory_runtime_service(db)
        asyncio.run(
            memory_runtime_service.run_extraction(
                db=db,
                selection=runtime_selection,
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=int(user_id),
                task_id=task_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        )
        db.commit()
    except Exception as exc:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        logger.warning(
            "[MEMORY_EXTRACTION] Background extraction worker error (task_id=%s): %s",
            task_id,
            exc,
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def enqueue_memory_extraction(
    *,
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    task_id: int | None,
    conversation_id: str | None,
    turn_id: str | None,
    llm_runtime_selection: Mapping[str, Any] | None = None,
) -> None:
    """Queue non-blocking memory extraction from finalize nodes."""
    if not is_semantic_memory_runtime_enabled():
        return
    runtime_selection = _coerce_runtime_selection(llm_runtime_selection)
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: run_memory_extraction_once(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                task_id=task_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                llm_runtime_selection=runtime_selection,
            ),
        )
    except Exception as exc:
        logger.warning(
            "[MEMORY_EXTRACTION] Failed to enqueue extraction (task_id=%s): %s",
            task_id,
            exc,
        )
        safe_inc("memory_extraction_enqueue_failures")
