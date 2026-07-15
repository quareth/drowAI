"""Task CRUD routes and task bootstrap orchestration.

Responsibilities:
- Expose create/read/update/delete task endpoints.
- Handle initial task bootstrap flow (workspace, queueing, startup trigger).
"""

from typing import List
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import TaskCreateVPN, TaskResponse, TaskUpdate, User
from ...services.engagement.service import EngagementService
from ...services.task.access_service import (
    list_tenant_tasks_for_user,
)
from ...services.tenant.authorization import (
    ACTION_TASK_CREATE,
    ACTION_TASK_DELETE,
    ACTION_TASK_READ,
    ACTION_TASK_UPDATE,
)
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.tenant.context import TenantRequestContext
from ...services.task.lifecycle_service import TaskLifecycleService
from .deps import (
    enforce_tenant_action,
    get_tenant_task_or_404,
    get_tenant_task_with_engagement_or_404,
    map_admission_exception,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=List[TaskResponse])
def get_tasks(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get all tasks for the current user with enhanced error handling."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    try:
        tasks = list_tenant_tasks_for_user(
            db=db,
            user_id=current_user.id,
            tenant_id=tenant_context.tenant_id,
        )
        return [TaskResponse.model_validate(task) for task in tasks]
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving tasks for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve tasks",
        )
    except Exception as e:
        logger.error(f"Unexpected error retrieving tasks: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        )


@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    task_data: TaskCreateVPN,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Create a new task with validation and error handling."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CREATE)
    lifecycle_service = TaskLifecycleService(db)
    try:
        task = lifecycle_service.create_task(
            task_data=task_data,
            user_id=current_user.id,
            tenant_context=tenant_context,
        )
    except HTTPException as exc:
        raise map_admission_exception(exc) from exc
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=task.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get a specific task by ID."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    task = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task)


@router.put("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task_update: TaskUpdate,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Update a specific task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_UPDATE)
    task = get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )

    update_data = task_update.model_dump(exclude_unset=True)
    if "status" in update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task lifecycle status cannot be updated through generic task update routes.",
        )
    if "engagement_id" in update_data:
        requested_engagement_id = update_data.pop("engagement_id")
        if requested_engagement_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="engagement_id cannot be null once a task is created",
            )

        current_engagement_id = getattr(task, "engagement_id", None)
        if current_engagement_id is not None:
            if int(requested_engagement_id) != int(current_engagement_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "engagement_id is immutable after task creation to preserve durable lineage; "
                        "create a new task to move work to another engagement"
                    ),
                )
        else:
            engagement_service = EngagementService(db)
            resolved = engagement_service.resolve_for_task_creation(
                user_id=current_user.id,
                task_name=task.name,
                task_description=task.description,
                requested_engagement_id=requested_engagement_id,
                expected_tenant_id=task.tenant_id,
            )
            task.engagement_id = resolved.id

    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=task.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Delete a task and remove any associated Docker container."""
    from ...services.task.cleanup_service import TaskCleanupService

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_DELETE)
    get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )
    cleanup_service = TaskCleanupService(db)
    return await cleanup_service.delete_task(
        task_id=task_id,
        user_id=current_user.id,
        tenant_id=tenant_context.tenant_id,
    )
