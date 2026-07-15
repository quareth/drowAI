"""Runner websocket-channel terminal disconnect cleanup helpers.

Purpose: clean runner-owned terminal frame buffers and terminal sessions when a
runner channel disconnects. Scope boundary: this module owns only disconnect
terminal cleanup and task lookup for that cleanup; it does not own channel
open/close lifecycle, terminal infrastructure, or frame buffering internals.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.core import Task

logger = logging.getLogger("backend.services.runner_control.channel_manager")


def _cleanup_runner_terminal_state(*, db: Session, tenant_id: int, runner_id: UUID) -> None:
    runner_task_ids = [
        int(task_id)
        for task_id in db.execute(
            select(Task.id).where(
                Task.tenant_id == tenant_id,
                Task.runtime_placement_mode == "runner",
                func.lower(Task.runner_id) == str(runner_id).lower(),
            )
        ).scalars().all()
    ]
    if not runner_task_ids:
        return
    try:
        from backend.services.runner_control.terminal_frame_buffer import (
            get_runner_terminal_frame_buffer,
        )

        frame_buffer = get_runner_terminal_frame_buffer()
        for task_id in runner_task_ids:
            frame_buffer.clear_task(tenant_id=tenant_id, task_id=task_id)
    except Exception:
        logger.debug(
            "Failed to cleanup runner terminal frame buffers on disconnect tenant_id=%s runner_id=%s",
            tenant_id,
            runner_id,
            exc_info=True,
        )

    try:
        from backend.services.terminal.manager import terminal_session_manager
    except Exception:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(terminal_session_manager.close_sessions_for_tasks(runner_task_ids))
        except Exception:
            logger.debug(
                "Failed to cleanup runner terminal sessions on disconnect tenant_id=%s runner_id=%s",
                tenant_id,
                runner_id,
                exc_info=True,
            )
        return

    loop.create_task(terminal_session_manager.close_sessions_for_tasks(runner_task_ids))
