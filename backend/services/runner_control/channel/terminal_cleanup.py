"""Runner websocket-channel terminal reset and disconnect cleanup helpers.

Purpose: clean runner-owned terminal frame buffers and sessions before channel
replacement or after final disconnect. Scope boundary: this module owns terminal
cleanup and task lookup only; it does not own channel lifecycle, terminal
infrastructure, or frame buffering internals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.core import Task

logger = logging.getLogger("backend.services.runner_control.channel_manager")


def _run_or_schedule_disconnect_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(cleanup)
        except Exception:
            logger.debug(
                "Failed to cleanup runner terminal sessions on disconnect",
                exc_info=True,
            )
        return

    task = loop.create_task(cleanup)

    def _log_cleanup_failure(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "Failed to cleanup runner terminal sessions on disconnect",
                exc_info=True,
            )

    task.add_done_callback(_log_cleanup_failure)


async def _cleanup_runner_shell_session_handles(
    *,
    tenant_id: int,
    task_ids: list[int],
) -> None:
    try:
        from runtime_shared.shell_session_port import get_shell_session_service
    except Exception:
        logger.debug(
            "Failed to resolve shell session service for runner disconnect cleanup tenant_id=%s",
            tenant_id,
            exc_info=True,
        )
        return

    shell_session_service = get_shell_session_service()
    for task_id in task_ids:
        try:
            await shell_session_service.close_task_sessions(
                tenant_id=tenant_id,
                task_id=task_id,
            )
        except Exception:
            logger.debug(
                "Failed to cleanup runner shell sessions on disconnect tenant_id=%s task_id=%s",
                tenant_id,
                task_id,
                exc_info=True,
            )


async def _cleanup_runner_terminal_sessions(task_ids: list[int]) -> None:
    try:
        from backend.services.terminal.manager import terminal_session_manager
    except Exception:
        return

    try:
        await terminal_session_manager.close_sessions_for_tasks(task_ids)
    except Exception:
        logger.debug(
            "Failed to cleanup runner terminal sessions on disconnect",
            exc_info=True,
        )


async def _cleanup_runner_session_state(
    *,
    tenant_id: int,
    task_ids: list[int],
) -> None:
    await _cleanup_runner_shell_session_handles(
        tenant_id=tenant_id,
        task_ids=task_ids,
    )
    await _cleanup_runner_terminal_sessions(task_ids)


def _runner_task_ids(*, db: Session, tenant_id: int, runner_id: UUID) -> list[int]:
    return [
        int(task_id)
        for task_id in db.execute(
            select(Task.id).where(
                Task.tenant_id == tenant_id,
                Task.runtime_placement_mode == "runner",
                func.lower(Task.runner_id) == str(runner_id).lower(),
            )
        ).scalars().all()
    ]


def _clear_runner_terminal_frames(
    *,
    tenant_id: int,
    runner_id: UUID,
    task_ids: list[int],
) -> None:
    if not task_ids:
        return
    try:
        from backend.services.runner_control.terminal_frame_buffer import (
            get_runner_terminal_frame_buffer,
        )

        frame_buffer = get_runner_terminal_frame_buffer()
        for task_id in task_ids:
            frame_buffer.clear_task(tenant_id=tenant_id, task_id=task_id)
    except Exception:
        logger.debug(
            "Failed to cleanup runner terminal frame buffers on disconnect tenant_id=%s runner_id=%s",
            tenant_id,
            runner_id,
            exc_info=True,
        )


def _prepare_runner_terminal_cleanup(
    *,
    db: Session,
    tenant_id: int,
    runner_id: UUID,
) -> list[int]:
    task_ids = _runner_task_ids(db=db, tenant_id=tenant_id, runner_id=runner_id)
    _clear_runner_terminal_frames(
        tenant_id=tenant_id,
        runner_id=runner_id,
        task_ids=task_ids,
    )
    return task_ids


async def _reset_runner_terminal_state(
    *,
    db: Session,
    tenant_id: int,
    runner_id: UUID,
) -> None:
    """Clear stale runner terminal state before a replacement channel is usable."""
    task_ids = _prepare_runner_terminal_cleanup(
        db=db,
        tenant_id=tenant_id,
        runner_id=runner_id,
    )
    if task_ids:
        await _cleanup_runner_session_state(
            tenant_id=tenant_id,
            task_ids=task_ids,
        )


def _cleanup_runner_terminal_state(*, db: Session, tenant_id: int, runner_id: UUID) -> None:
    task_ids = _prepare_runner_terminal_cleanup(
        db=db,
        tenant_id=tenant_id,
        runner_id=runner_id,
    )
    if not task_ids:
        return

    _run_or_schedule_disconnect_cleanup(
        _cleanup_runner_session_state(
            tenant_id=tenant_id,
            task_ids=task_ids,
        )
    )
