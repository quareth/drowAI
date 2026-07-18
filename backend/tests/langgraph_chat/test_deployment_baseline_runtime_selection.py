"""Deployment baseline tests for chat runtime selection snapshots."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

import backend.routers.chat as chat_package
import backend.services.streaming.in_memory_hub as stream_hub_module
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import Task, User
from backend.routers.chat.schemas import ChatRequest
from backend.routers.chat.submit import _submit_chat_request
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMProviderSelectionService,
)


def _create_user_and_task(db) -> tuple[User, Task]:
    user = User(
        username=f"deployment-runtime-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    task = Task(user_id=user.id, tenant_id=1, name="runtime-baseline")
    db.add(task)
    db.commit()
    db.refresh(task)
    return user, task


@pytest.mark.asyncio
async def test_chat_submission_snapshots_selected_runtime_for_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    scheduled_calls: list[dict[str, Any]] = []
    try:
        user, task = _create_user_and_task(db)
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-chat-runtime",
        )
        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()

        class FakeConversationManager:
            def __init__(self, task_id: int) -> None:
                self.task_id = task_id

            def ensure_default_conversation(self) -> str:
                return "conv-runtime"

        def fake_generation(**kwargs: Any) -> object:
            scheduled_calls.append(dict(kwargs))
            return object()

        monkeypatch.setattr(
            chat_package,
            "ConversationManager",
            FakeConversationManager,
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_build_conversation_history",
            lambda *args, **kwargs: [],
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_reserve_chat_turn",
            lambda *args, **kwargs: (101, 102, "task-turn-1", 1),
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_schedule_background_task",
            lambda coro: scheduled_calls.append({"scheduled": coro}),
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "run_langgraph_generation",
            fake_generation,
            raising=False,
        )
        monkeypatch.setattr(
            stream_hub_module,
            "get_in_memory_stream_hub",
            lambda: None,
        )
        monkeypatch.setattr(
            chat_package,
            "validate_reasoning_effort_for_model",
            lambda **kwargs: kwargs.get("effort"),
            raising=False,
        )

        response = await _submit_chat_request(
            task_id=task.id,
            payload=ChatRequest(message="hello"),
            current_user=user,
            db=db,
            task=task,
        )

        generation_call = next(call for call in scheduled_calls if "runtime_selection" in call)
        assert response["success"] is True
        assert generation_call["provider"] == OPENAI_PROVIDER_ID
        assert generation_call["model"] == "gpt-5.2"
        assert generation_call["runtime_selection"] == {
            "provider": OPENAI_PROVIDER_ID,
            "model": "gpt-5.2",
            "credential_ref": {
                "user_id": user.id,
                "provider": OPENAI_PROVIDER_ID,
            },
            "reasoning_effort": None,
        }
        assert "sk-chat-runtime" not in repr(generation_call["runtime_selection"])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_explicit_chat_model_override_does_not_mutate_saved_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    scheduled_calls: list[dict[str, Any]] = []
    try:
        user, task = _create_user_and_task(db)
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-chat-override",
        )
        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()

        monkeypatch.setattr(
            chat_package,
            "ConversationManager",
            lambda _task_id: type(
                "FakeConversationManager",
                (),
                {"ensure_default_conversation": lambda self: "conv-override"},
            )(),
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_build_conversation_history",
            lambda *args, **kwargs: [],
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_reserve_chat_turn",
            lambda *args, **kwargs: (201, 202, "task-turn-2", 2),
            raising=False,
        )
        monkeypatch.setattr(
            chat_package,
            "_schedule_background_task",
            lambda coro: scheduled_calls.append({"scheduled": coro}),
            raising=False,
        )

        def fake_generation(**kwargs: Any) -> object:
            scheduled_calls.append(dict(kwargs))
            return object()

        monkeypatch.setattr(
            chat_package,
            "run_langgraph_generation",
            fake_generation,
            raising=False,
        )
        monkeypatch.setattr(
            stream_hub_module,
            "get_in_memory_stream_hub",
            lambda: None,
        )
        monkeypatch.setattr(
            chat_package,
            "validate_reasoning_effort_for_model",
            lambda **kwargs: kwargs.get("effort"),
            raising=False,
        )

        await _submit_chat_request(
            task_id=task.id,
            payload=ChatRequest(message="hello", model="gpt-5-mini"),
            current_user=user,
            db=db,
            task=task,
        )

        generation_call = next(call for call in scheduled_calls if "runtime_selection" in call)
        saved_selection = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        ).get_selection(user.id)

        assert generation_call["model"] == "gpt-5-mini"
        assert generation_call["runtime_selection"]["model"] == "gpt-5-mini"
        assert saved_selection.model == "gpt-5.2"
    finally:
        db.close()
