"""Unit tests for LangGraph facade branch selection helpers."""

from __future__ import annotations

from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.routing.selectors import (
    ChatBranch,
    resolve_branch,
    select_branch,
)


def _runtime_config(mode: ExecutionMode) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=1,
            user_id=1,
            message="test",
            conversation_id="conv-1",
            history=[],
        ),
        execution_mode=mode,
        metadata={},
    )


def test_select_branch_normal_chat() -> None:
    assert select_branch(_runtime_config(ExecutionMode.NORMAL_CHAT)) is ChatBranch.NORMAL_CHAT


def test_select_branch_deep_reasoning() -> None:
    assert select_branch(_runtime_config(ExecutionMode.DEEP_REASONING)) is ChatBranch.DEEP_REASONING


def test_select_branch_simple_tool() -> None:
    assert select_branch(_runtime_config(ExecutionMode.SIMPLE_TOOL)) is ChatBranch.SIMPLE_TOOL


def test_resolve_branch_routes_scout_owned_direct_executor_by_default() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning", "service enumeration"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": "scout",
                        "objective": "Scan ports and enumerate services on 10.0.0.10.",
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
        )
        is ChatBranch.RECON_AGENT
    )
    assert config.metadata["subagent_routing"]["capabilities"] == [
        "port_scan",
        "service_enum",
    ]
    assert (
        config.metadata["subagent_routing"]["objective"]
        == "Scan ports and enumerate services on 10.0.0.10."
    )


def test_resolve_branch_uses_handoff_instead_of_capability_vocabulary() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning", "report"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": "scout",
                        "objective": "Scan ports on 10.0.0.10.",
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
        )
        is ChatBranch.RECON_AGENT
    )
    assert config.metadata["subagent_routing"]["reason"] == "scout_owned"


def test_resolve_branch_rejects_active_local_scout_run() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": "scout",
                        "objective": "Scan ports on 10.0.0.10.",
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
            active_recon_run_exists=True,
        )
        is ChatBranch.SIMPLE_TOOL
    )
    assert config.metadata["subagent_routing"]["reason"] == "subagent_unavailable"
