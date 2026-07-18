"""Deployment baseline tests for residual task model-switch signaling."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import Task, User
from backend.routers import llm as llm_routes
from backend.services.llm_provider import LLMCredentialService
from backend.services.task.runtime_input_service import RuntimeInputResult
from backend.services.tenant.authorization import ACTION_TASK_CONTROL
from backend.services.tenant.context import TenantRequestContext


LEGACY_RESIDUAL_TASK_SWITCH = "legacy_residual"
TASK_SWITCH_REPLACEMENT_EXPECTATION = "phase_2_deprecated_user_global_selection_facade"


def _create_user_task_and_context(db, *, role: str = "owner") -> tuple[User, Task, TenantRequestContext]:
    user = User(
        username=f"deployment-task-switch-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    task = Task(user_id=user.id, tenant_id=1, name="task-switch-baseline")
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
async def test_legacy_residual_task_switch_appends_control_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SessionLocal()
    runtime_inputs: list[dict] = []
    try:
        user, task, tenant_context = _create_user_task_and_context(db)
        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-task-switch",
        )
        db.commit()

        async def fake_append_and_signal(*args, **kwargs):
            runtime_inputs.append(
                {
                    "legacy_residual": LEGACY_RESIDUAL_TASK_SWITCH,
                    "replacement_expectation": TASK_SWITCH_REPLACEMENT_EXPECTATION,
                    "args": args,
                    "kwargs": kwargs,
                }
            )
            return RuntimeInputResult(
                persisted=True,
                signal_attempted=True,
                signal_sent=False,
                detail="signal not available in test environment",
            )

        monkeypatch.setattr(
            llm_routes,
            "_runtime_input_service",
            SimpleNamespace(append_and_signal=fake_append_and_signal),
        )

        response = await llm_routes.switch_task_model(
            task_id=task.id,
            body=llm_routes.TaskSwitchRequest(model="gpt-5.2"),
            current_user=user,
            tenant_context=tenant_context,
            db=db,
        )

        assert response == {
            "success": True,
            "signal_sent": False,
            "detail": "signal not available in test environment",
        }
        assert runtime_inputs == [
            {
                "legacy_residual": LEGACY_RESIDUAL_TASK_SWITCH,
                "replacement_expectation": TASK_SWITCH_REPLACEMENT_EXPECTATION,
                "args": (task.id,),
                "kwargs": {
                    "message": "__switch_model:gpt-5.2",
                    "strict_persistence": True,
                    "user_id": user.id,
                    "metadata": {
                        "type": "switch_llm",
                        "command": "switch_llm_model",
                        "provider": OPENAI_PROVIDER_ID,
                        "model": "gpt-5.2",
                        "credential_ref": {
                            "user_id": user.id,
                            "provider": OPENAI_PROVIDER_ID,
                        },
                    },
                },
            }
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_task_switch_preserves_task_control_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SessionLocal()
    runtime_called = False
    try:
        user, task, tenant_context = _create_user_task_and_context(db, role="viewer")

        async def fake_append_and_signal(*_args, **_kwargs):
            nonlocal runtime_called
            runtime_called = True
            return RuntimeInputResult(
                persisted=True,
                signal_attempted=True,
                signal_sent=True,
            )

        monkeypatch.setattr(
            llm_routes,
            "_runtime_input_service",
            SimpleNamespace(append_and_signal=fake_append_and_signal),
        )

        with pytest.raises(HTTPException) as exc_info:
            await llm_routes.switch_task_model(
                task_id=task.id,
                body=llm_routes.TaskSwitchRequest(model="gpt-5.2"),
                current_user=user,
                tenant_context=tenant_context,
                db=db,
            )

        assert exc_info.value.status_code == 403
        assert ACTION_TASK_CONTROL in str(exc_info.value.detail)
        assert runtime_called is False
    finally:
        db.close()
