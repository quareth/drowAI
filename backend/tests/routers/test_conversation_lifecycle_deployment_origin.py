"""Verify conversation routes persist and reuse remote lifecycle origins."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.database import SessionLocal
from backend.models import LLMConversation, Task, User, UserLLMSelection
from backend.routers import llm as llm_routes
from backend.services.llm_provider.conversation_lifecycle_service import (
    RemoteConversationOrigin,
)
from backend.services.tenant.context import TenantRequestContext


def _identity(db) -> tuple[User, Task, TenantRequestContext]:
    user = User(
        username=f"conversation-origin-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    task = Task(user_id=user.id, tenant_id=1, name="Conversation Origin")
    db.add(task)
    db.commit()
    db.refresh(task)
    return (
        user,
        task,
        TenantRequestContext(
            tenant_id=int(task.tenant_id),
            user_id=int(user.id),
            role="owner",
            membership_id=1,
            is_default_tenant=True,
            source="test",
        ),
    )


def _origin() -> RemoteConversationOrigin:
    return RemoteConversationOrigin(
        connection_id=str(uuid4()),
        deployment_id=str(uuid4()),
        route_id=str(uuid4()),
        origin_revision=7,
        deployment_revision=3,
        provider="openai",
        model="gpt-5.2",
        remote_resource_id="remote-snapshotted-origin",
    )


class _ConversationManager:
    def __init__(self, _task_id: int) -> None:
        pass

    def get_active_conversation_id(self):
        return "local-origin"

    def create_conversation(self, title: str):
        return "local-origin"

    def set_openai_conversation_id(self, *_args) -> None:
        pass

    def reset_openai_conversation(self) -> None:
        pass


@pytest.mark.asyncio
async def test_create_route_persists_complete_remote_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SessionLocal()
    origin = _origin()
    try:
        user, task, tenant_context = _identity(db)

        class _Lifecycle:
            def create_remote_conversation(self, **_kwargs):
                return origin

        monkeypatch.setattr(llm_routes, "LLMConversationLifecycleService", lambda _db: _Lifecycle())
        monkeypatch.setattr(
            "agent.chat.conversation_manager.ConversationManager",
            _ConversationManager,
        )

        await llm_routes.create_task_conversation(
            task_id=task.id,
            body=llm_routes.ConversationCreateBody(title="Origin", model="gpt-5.2"),
            current_user=user,
            tenant_context=tenant_context,
            db=db,
        )

        row = db.query(LLMConversation).filter(LLMConversation.task_id == task.id).one()
        assert str(row.connection_id) == origin.connection_id
        assert str(row.deployment_id) == origin.deployment_id
        assert str(row.route_id) == origin.route_id
        assert row.origin_revision == origin.origin_revision
        assert row.origin_deployment_revision == origin.deployment_revision
        assert row.remote_resource_id == origin.remote_resource_id
        assert row.conversation_id == origin.remote_resource_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_route_uses_row_origin_after_selection_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    origin = _origin()
    calls: list[dict[str, object]] = []
    try:
        user, task, tenant_context = _identity(db)
        row = LLMConversation(
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=user.id,
            provider=origin.provider,
            model=origin.model,
            connection_id=origin.connection_id,
            deployment_id=origin.deployment_id,
            route_id=origin.route_id,
            conversation_id=origin.remote_resource_id,
            status="active",
            is_active=True,
        )
        row.origin_revision = origin.origin_revision
        row.origin_deployment_revision = origin.deployment_revision
        row.remote_resource_id = origin.remote_resource_id
        db.add_all(
            [
                row,
                UserLLMSelection(
                    user_id=user.id,
                    provider="openai",
                    model="gpt-5-mini",
                    deployment_id=uuid4(),
                ),
            ]
        )
        db.commit()
        db.refresh(row)

        class _Lifecycle:
            def require_remote_conversation_lifecycle(self, _provider: str) -> None:
                pass

            def delete_remote_conversation(self, **kwargs) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(llm_routes, "LLMConversationLifecycleService", lambda _db: _Lifecycle())
        monkeypatch.setattr(
            "agent.chat.conversation_manager.ConversationManager",
            _ConversationManager,
        )

        response = await llm_routes.delete_task_conversation(
            task_id=task.id,
            row_id=row.id,
            current_user=user,
            tenant_context=tenant_context,
            db=db,
        )

        assert response == {"success": True}
        assert calls == [
            {
                "origin": origin,
                "runtime_user_id": user.id,
                "task_id": task.id,
                "tenant_id": int(task.tenant_id),
            }
        ]
    finally:
        db.close()
