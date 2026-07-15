from dataclasses import asdict, fields

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from backend.services.langgraph_chat.contracts import AgentMode, ChatInputs


def test_agent_mode_serialization() -> None:
    assert AgentMode.FULL_ACCESS.value == "full_access"
    assert AgentMode.AGENT == "agent"


def test_chat_inputs_default_mode() -> None:
    inputs = ChatInputs(
        task_id=1,
        user_id=1,
        message="scan network",
        conversation_id=None,
        history=[],
    )
    assert inputs.agent_mode == AgentMode.FULL_ACCESS


def test_chat_inputs_explicit_mode() -> None:
    inputs = ChatInputs(
        task_id=1,
        user_id=1,
        message="scan network",
        conversation_id=None,
        history=[],
        agent_mode=AgentMode.AGENT,
    )
    assert inputs.agent_mode == AgentMode.AGENT


def test_chat_inputs_accepts_legacy_api_key_without_storing_secret() -> None:
    inputs = ChatInputs(
        task_id=1,
        user_id=1,
        message="scan network",
        conversation_id=None,
        history=[],
        api_key="sk-test-secret",
        provider="openai",
        model="gpt-5.2",
    )

    assert "api_key" not in asdict(inputs)
    assert "api_key" not in {field.name for field in fields(inputs)}
    assert "sk-test-secret" not in repr(inputs)


def test_graph_runtime_context_ignores_legacy_api_key_secret() -> None:
    context = GraphRuntimeContext(
        task_id=1,
        user_id=1,
        api_key="sk-test-secret",
        provider="openai",
        model="gpt-5.2",
    )

    assert "api_key" not in context.model_dump()
    assert "sk-test-secret" not in repr(context)
