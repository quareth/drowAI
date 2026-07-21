"""Tests that new LangGraph checkpoint payloads use V2 runtime identity."""

from __future__ import annotations

from uuid import uuid4

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from backend.services.langgraph_chat.checkpoint.execution_config import (
    build_checkpoint_execution_config,
)
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade_helpers import build_thread_config
from backend.services.langgraph_chat.handlers.turn_runtime import (
    build_initial_interactive_state,
)


def _runtime_selection(
    *,
    deployment_id: str | None = None,
    legacy: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id or str(uuid4()),
            "expected_revision": 7,
            "endpoint": "https://checkpoint.example.invalid/v1",
        },
        "preferred_route_id": None,
        "reasoning_effort": "medium",
        "api_key": "sk-should-not-survive",
        "credential_ref": {"user_id": 999, "provider": "openai"},
        "resolved_endpoint": "https://checkpoint.example.invalid/v1",
    }
    if legacy:
        payload["legacy_provider"] = "openai"
        payload["legacy_model"] = "gpt-5.2"
    return payload


def _runtime_config(selection: dict[str, object]) -> LangGraphRuntimeConfig:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-no-legacy-runtime",
        turn_id="turn-no-legacy-runtime",
        turn_sequence=3,
        messages=[],
        current_message="hello",
    )
    chat_inputs = ChatInputs(
        task_id=41,
        user_id=17,
        message="hello",
        conversation_id="conv-no-legacy-runtime",
        history=[],
        provider="openai",
        model="gpt-5.2",
        credential_ref={"user_id": 17, "provider": "openai"},
        llm_runtime_selection=selection,
        reasoning_effort="medium",
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.NORMAL_CHAT,
        metadata={
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
            "graph_thread_id": "a" * 32,
            "tenant_id": 5,
            "runtime_placement_mode": "local",
            "workspace_id": "workspace-41",
            "actor_type": "user",
            "actor_id": "17",
        },
        llm_runtime_selection=selection,
    )


def test_new_initial_state_and_thread_config_write_only_v2_runtime_identity() -> None:
    """New graph state/config payloads do not persist legacy credential refs."""

    deployment_id = str(uuid4())
    config = _runtime_config(_runtime_selection(deployment_id=deployment_id))

    initial_state, _ = build_initial_interactive_state(config)
    graph_config = build_thread_config(config, task_id=41)

    metadata = initial_state["facts"]["metadata"]
    selection = metadata["llm_runtime_selection"]
    runtime_context = metadata["graph_runtime_context"]
    configurable = graph_config["configurable"]
    projection = configurable["runtime_projection"]

    assert selection == {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 7,
        },
        "reasoning_effort": "medium",
        "legacy_provider": "openai",
        "legacy_model": "gpt-5.2",
    }
    assert configurable["llm_runtime_selection"] == selection
    assert "llm_runtime_selection" not in projection
    assert "llm_runtime_selection" not in runtime_context

    serialized = repr(initial_state) + repr(graph_config)
    for forbidden in (
        "credential_ref",
        "api_key",
        "sk-should-not-survive",
        "endpoint",
        "resolved_endpoint",
    ):
        assert forbidden not in serialized


def test_checkpoint_execution_config_sanitizes_v2_runtime_selection() -> None:
    """Continuation configs keep V2 identity while dropping live target fields."""

    deployment_id = str(uuid4())

    config = build_checkpoint_execution_config(
        task_id=41,
        graph_name="simple_tool",
        graph_thread_id="b" * 32,
        user_id=17,
        tenant_id=5,
        runtime_placement_mode="local",
        workspace_id="workspace-41",
        actor_type="user",
        actor_id="17",
        llm_runtime_selection=_runtime_selection(
            deployment_id=deployment_id,
            legacy=False,
        ),
    )

    configurable = config["configurable"]
    expected_selection = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 7,
        },
        "reasoning_effort": "medium",
    }
    assert configurable["llm_runtime_selection"] == expected_selection
    assert "llm_runtime_selection" not in configurable["runtime_projection"]
    assert "legacy_provider" not in repr(configurable)
    assert "legacy_model" not in repr(configurable)
    serialized = repr(config)
    for forbidden in (
        "credential_ref",
        "api_key",
        "sk-should-not-survive",
        "endpoint",
        "resolved_endpoint",
    ):
        assert forbidden not in serialized
