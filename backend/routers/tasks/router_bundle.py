"""Compose all /api/tasks sub-routers and compatibility re-exports for backend.main.

Loaded lazily via ``backend.routers.tasks`` ``__getattr__`` so tests can import
``backend.routers.tasks.crud`` without pulling interrupts/LangGraph.
"""

import asyncio

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from .container import router as container_router
from .crud import router as crud_router
from .files import router as files_router
from .interrupts import (
    ResumeRequest,
    RetryRequest,
    _resolve_tenant_context,
    router as interrupts_router,
)
from .interrupt_inbox import router as interrupt_inbox_router
from .logs import router as logs_router
from .metrics import router as metrics_router
from .runtime import router as runtime_router
from .scope import router as scope_router
from .vpn import router as vpn_router

router = APIRouter()

router.include_router(crud_router)
router.include_router(runtime_router)
router.include_router(interrupts_router)
router.include_router(interrupt_inbox_router)
router.include_router(files_router)
router.include_router(scope_router)
router.include_router(logs_router)
router.include_router(container_router)
router.include_router(metrics_router)
router.include_router(vpn_router)


def _schedule_background_task(coro):
    """Create a background task via a patchable seam for tests."""
    return asyncio.create_task(coro)


def get_interrupt_state_service():
    """Lazy-load interrupt state factory so importing this package stays lightweight."""
    from ...services.langgraph_chat.checkpoint.interrupt_state_service import (
        get_interrupt_state_service as _factory,
    )

    return _factory()


async def get_task_interrupt(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Compatibility wrapper for direct imports from backend.routers.tasks."""
    from ...services.task.interrupt_service import TaskInterruptService

    tenant_context = _resolve_tenant_context(
        tenant_context=None,
        db=db,
        current_user=current_user,
    )
    interrupt_service = TaskInterruptService(db)
    return await interrupt_service.get_task_interrupt(
        task_id=task_id,
        user_id=current_user.id,
        interrupt_service=get_interrupt_state_service(),
        tenant_id=tenant_context.tenant_id,
    )


async def resume_graph_execution(
    task_id: int,
    request: ResumeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Compatibility wrapper for direct imports from backend.routers.tasks."""
    import time
    from backend.services.langgraph_chat.execution.turn_service import run_resume_generation

    if request.interrupt_type == "clarify_request":
        if request.response.action != "answer":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="clarify_request resumes must use action='answer'.",
            )
        answers = request.response.answers
        has_valid_answer = isinstance(answers, dict) and any(
            str(key).strip() and str(value).strip()
            for key, value in answers.items()
        )
        if not has_valid_answer:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="clarify_request resumes require non-empty answers.",
            )

    approval_received_at = time.perf_counter()
    from ...services.task.interrupt_service import TaskInterruptService

    tenant_context = _resolve_tenant_context(
        tenant_context=None,
        db=db,
        current_user=current_user,
    )
    interrupt_service = TaskInterruptService(db)
    return await interrupt_service.resume_graph_execution(
        task_id=task_id,
        user_id=current_user.id,
        interrupt_id=request.interrupt_id,
        graph_name=request.graph_name,
        response_payload=request.response.model_dump(),
        create_task_fn=_schedule_background_task,
        run_resume_generation=run_resume_generation,
        approval_received_at=approval_received_at,
        tenant_id=tenant_context.tenant_id,
    )


async def retry_graph_execution(
    task_id: int,
    request: RetryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Compatibility wrapper for checkpoint retry from backend.routers.tasks."""
    from backend.services.langgraph_chat.execution.turn_service import (
        run_checkpoint_retry_generation,
    )
    from ...services.task.graph_retry_service import TaskGraphRetryService

    retry_service = TaskGraphRetryService(db)
    return await retry_service.retry_graph_execution(
        task_id=task_id,
        user_id=current_user.id,
        turn_id=request.turn_id,
        retry_mode=request.retry_mode,
        graph_name=request.graph_name,
        create_task_fn=_schedule_background_task,
        run_checkpoint_retry_generation=run_checkpoint_retry_generation,
    )


__all__ = [
    "router",
    "ResumeRequest",
    "RetryRequest",
    "get_task_interrupt",
    "resume_graph_execution",
    "retry_graph_execution",
    "get_interrupt_state_service",
    "_schedule_background_task",
]
