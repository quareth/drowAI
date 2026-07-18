"""Regression tests for the deprecated task model-switch compatibility facade."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.database import SessionLocal
from backend.models import Task, User
from backend.routers import llm as llm_routes
from backend.services.llm_provider import LLMProviderSelectionService
from backend.services.tenant.authorization import ACTION_TASK_CONTROL
from backend.services.tenant.context import TenantRequestContext


def _create_user_task_and_context(
    db,
    *,
    role: str = "owner",
) -> tuple[User, Task, TenantRequestContext]:
    user = User(
        username=f"task-switch-facade-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    task = Task(user_id=user.id, tenant_id=1, name="task-switch-facade")
    db.add(task)
    db.commit()
    db.refresh(task)
    return (
        user,
        task,
        TenantRequestContext(
            tenant_id=int(task.tenant_id),
            user_id=int(user.id),
            role=role,
            membership_id=1,
            is_default_tenant=True,
            source="test",
        ),
    )


@pytest.mark.asyncio
async def test_task_switch_facade_persists_next_turn_selection_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    try:
        user, task, tenant_context = _create_user_task_and_context(db)

        async def fail_if_runtime_input_is_used(*_args, **_kwargs):
            pytest.fail("deprecated task switch must not append or signal runtime input")

        monkeypatch.setattr(
            llm_routes._runtime_input_service,
            "append_and_signal",
            fail_if_runtime_input_is_used,
        )

        response = await llm_routes.switch_task_model(
            task_id=task.id,
            body=llm_routes.TaskSwitchRequest(provider="openai", model="gpt-5.2"),
            current_user=user,
            tenant_context=tenant_context,
            db=db,
        )

        selection = LLMProviderSelectionService(db).get_selection(user.id)
        assert (selection.provider, selection.model) == ("openai", "gpt-5.2")
        assert response == {
            "success": True,
            "deprecated": True,
            "effective_from": "next_submitted_turn",
            "provider": "openai",
            "model": "gpt-5.2",
            "signal_sent": False,
        }
    finally:
        db.close()


@pytest.mark.asyncio
async def test_task_switch_facade_preserves_task_control_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    selection_called = False
    try:
        user, task, tenant_context = _create_user_task_and_context(db, role="viewer")
        original_set_selection = LLMProviderSelectionService.set_selection

        def track_set_selection(self, **kwargs):
            nonlocal selection_called
            selection_called = True
            return original_set_selection(self, **kwargs)

        monkeypatch.setattr(LLMProviderSelectionService, "set_selection", track_set_selection)

        with pytest.raises(HTTPException) as exc_info:
            await llm_routes.switch_task_model(
                task_id=task.id,
                body=llm_routes.TaskSwitchRequest(provider="openai", model="gpt-5.2"),
                current_user=user,
                tenant_context=tenant_context,
                db=db,
            )

        assert exc_info.value.status_code == 403
        assert ACTION_TASK_CONTROL in str(exc_info.value.detail)
        assert selection_called is False
    finally:
        db.close()
