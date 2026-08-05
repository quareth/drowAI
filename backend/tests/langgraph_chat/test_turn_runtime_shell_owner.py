"""Tests for main-turn shell-session owner projection into graph metadata."""

from types import SimpleNamespace

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.handlers.turn_runtime import (
    build_initial_interactive_state,
    ensure_turn_identity,
)


def _chat_inputs() -> ChatInputs:
    return ChatInputs(
        task_id=42,
        user_id=7,
        message="run a command",
        conversation_id="conv-42",
        history=[],
    )


def _runtime_config(metadata: dict) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=_chat_inputs(),
        metadata=metadata,
    )


def _quiet_logger() -> SimpleNamespace:
    return SimpleNamespace(info=lambda *_args, **_kwargs: None)


def test_main_turn_owner_survives_initial_state_metadata_projection() -> None:
    metadata = {
        "turn_id": "task-42-turn-3",
        "turn_number": 3,
        "turn_sequence": 3,
        "tenant_id": 11,
        "runtime_placement_mode": "local",
        "workspace_id": "task-42",
        "actor_type": "agent",
        "actor_id": "langgraph",
        METADATA_CONTEXT_BUNDLE_KEY: {},
    }
    runtime_config = _runtime_config(metadata)

    turn = ensure_turn_identity(runtime_config, logger_=_quiet_logger())
    initial_state, _ = build_initial_interactive_state(runtime_config)

    graph_context = initial_state["facts"]["metadata"]["graph_runtime_context"]
    assert turn.turn_id == "task-42-turn-3"
    assert runtime_config.metadata["execution_owner_id"] == "main:task-42-turn-3"
    assert graph_context["execution_owner_id"] == "main:task-42-turn-3"
    assert runtime_config.metadata["graph_runtime_context"]["execution_owner_id"] == (
        "main:task-42-turn-3"
    )
    assert graph_context["turn_id"] == "task-42-turn-3"
    assert graph_context["turn_sequence"] == 3
    assert initial_state["facts"]["metadata"]["turn_id"] == "task-42-turn-3"
    assert initial_state["facts"]["metadata"]["turn_sequence"] == 3


def test_main_turn_owner_is_derived_from_canonical_turn_not_stale_metadata() -> None:
    runtime_config = _runtime_config(
        {
            "turn_id": "task-42-turn-8",
            "turn_number": 8,
            "turn_sequence": 8,
            "execution_owner_id": "main:stale-turn",
        }
    )

    ensure_turn_identity(runtime_config, logger_=_quiet_logger())

    assert runtime_config.metadata["execution_owner_id"] == "main:task-42-turn-8"
