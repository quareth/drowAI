"""Compatibility tests for the split `backend.routers.chat` package surface."""

from __future__ import annotations

from fastapi import APIRouter

from backend.routers import chat as chat_routes


def test_chat_package_exports_existing_router_and_patch_targets() -> None:
    assert isinstance(chat_routes.router, APIRouter)
    assert callable(chat_routes.get_db)
    assert callable(chat_routes.get_current_user)
    assert callable(chat_routes.get_user_openai_key)
    assert callable(chat_routes.get_user_openai_model)
    assert callable(chat_routes.validate_reasoning_effort_for_model)
    assert callable(chat_routes.get_run_lifecycle_service)
    assert callable(chat_routes.run_langgraph_generation)
    assert callable(chat_routes._schedule_background_task)
    assert callable(chat_routes._build_conversation_history)
    assert callable(chat_routes._reserve_chat_turn)
    assert callable(chat_routes._ensure_chat_prewarm)
    assert callable(chat_routes._get_runtime_warmup_status)
    assert callable(chat_routes._build_chat_startup_payload)
    assert chat_routes.ConversationManager is not None
    assert chat_routes.ConversationHistoryReader is not None
    assert chat_routes.ChatTurnOrchestrator is not None
    assert chat_routes.ChatRequest.__name__ == "ChatRequest"
