"""Process-local subagent-run status and cancellation routes.

The pilot exposes only same-process status/cancel state for authorized tasks.
It does not query historical storage or imply restart recovery.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.services.agent_runs.control import (
    AgentRunControlService,
    AgentRunMissingError,
    AgentRunNotActiveError,
    LocalAgentRunStatusProjection,
)
from backend.services.agent_runs.local_runtime import (
    get_process_local_agent_run_runtime,
)
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    InterruptTicketService,
)
from backend.services.tenant.authorization import ACTION_TASK_CONTROL, ACTION_TASK_READ
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context

from .deps import enforce_tenant_action, get_tenant_task_or_404

router = APIRouter()


class LocalAgentRunListResponse(BaseModel):
    """Process-local subagent run list for one authorized task."""

    model_config = ConfigDict(extra="forbid")

    process_local: Literal[True] = True
    task_id: int
    agent_runs: list[LocalAgentRunStatusProjection]


class LocalAgentRunCancelResponse(BaseModel):
    """Process-local cancellation acknowledgement for one subagent run."""

    model_config = ConfigDict(extra="forbid")

    process_local: Literal[True] = True
    cancelled: bool
    agent_run: LocalAgentRunStatusProjection


def get_agent_run_control_service(
    db: Session = Depends(get_db),
) -> AgentRunControlService:
    """Build the process-local status/cancel service dependency."""
    runtime = get_process_local_agent_run_runtime()
    return AgentRunControlService(
        registry=runtime.registry,
        launcher=runtime.launcher,
        subagent_registry=runtime.subagent_registry,
        interrupt_ticket_service=InterruptTicketService(db),
    )


@router.get(
    "/{task_id}/agent-runs/local",
    response_model=LocalAgentRunListResponse,
)
async def list_local_agent_runs(
    task_id: int,
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
    service: AgentRunControlService = Depends(get_agent_run_control_service),
) -> LocalAgentRunListResponse:
    """List this process' subagent runs for an authorized tenant task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    task = get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )
    agent_runs = await service.list_local_runs(
        tenant_id=int(tenant_context.tenant_id),
        task_id=int(task.id),
    )
    return LocalAgentRunListResponse(task_id=int(task.id), agent_runs=agent_runs)


@router.post(
    "/{task_id}/agent-runs/{agent_run_id}/cancel",
    response_model=LocalAgentRunCancelResponse,
)
async def cancel_local_agent_run(
    task_id: int,
    agent_run_id: str,
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
    service: AgentRunControlService = Depends(get_agent_run_control_service),
) -> LocalAgentRunCancelResponse:
    """Request cancellation for one live subagent run in an authorized task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    task = get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )
    try:
        agent_run = await service.request_cancellation(
            tenant_id=int(tenant_context.tenant_id),
            task_id=int(task.id),
            agent_run_id=agent_run_id,
        )
    except AgentRunMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason_code": "agent_run_not_active_in_process",
                "message": "No process-local subagent run exists for this tenant/task/run.",
            },
        ) from exc
    except AgentRunNotActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "agent_run_not_active",
                "message": "The process-local subagent run is not active.",
            },
        ) from exc
    return LocalAgentRunCancelResponse(cancelled=True, agent_run=agent_run)


__all__ = [
    "LocalAgentRunCancelResponse",
    "LocalAgentRunListResponse",
    "cancel_local_agent_run",
    "get_agent_run_control_service",
    "list_local_agent_runs",
    "router",
]
