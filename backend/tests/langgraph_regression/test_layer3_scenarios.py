"""Layer 3 deterministic scenario tests across branch/history/tool/HITL flows."""

from __future__ import annotations

from typing import Any, Dict, Sequence

import pytest

from backend.services.langgraph_chat.contracts import ExecutionMode, LangGraphChatResult, LangGraphRuntimeConfig
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.routing.selectors import ChatBranch

pytestmark = [
    pytest.mark.regression_layer3,
    pytest.mark.regression_main,
    pytest.mark.regression_nightly,
]


def _mode_from_branch(branch: str) -> ExecutionMode:
    if branch == ChatBranch.SIMPLE_TOOL.value:
        return ExecutionMode.SIMPLE_TOOL
    if branch == ChatBranch.DEEP_REASONING.value:
        return ExecutionMode.DEEP_REASONING
    return ExecutionMode.NORMAL_CHAT


class _StubBranchHandler:
    """Facade handler stub that records selected branch and emits deterministic events."""

    def __init__(self, branch: str, event_types: Sequence[str]) -> None:
        self.branch = branch
        self.event_types = list(event_types)

    async def handle(self, runtime_config: LangGraphRuntimeConfig) -> LangGraphChatResult:  # noqa: ARG002
        async def _iter_events():
            for event_type in self.event_types:
                yield {"type": event_type, "content": "", "metadata": {}}

        return LangGraphChatResult(
            final_text=f"{self.branch} complete",
            conversation_id="conv-regression",
            metadata={"handled_branch": self.branch},
            _event_iterator=_iter_events,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_id",
    [
        pytest.param("branch_normal_chat", marks=pytest.mark.regression_quick),
        pytest.param("branch_simple_tool", marks=pytest.mark.regression_quick),
        pytest.param("branch_deep_reasoning", marks=pytest.mark.regression_quick),
    ],
)
async def test_branch_scenarios_route_through_facade(
    scenario_id: str,
    regression_harness,
    regression_scenario_index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = regression_scenario_index[scenario_id]
    mode = _mode_from_branch(scenario.expected_branch)
    chat_inputs = regression_harness.make_chat_inputs(
        task_id=150,
        user_id=2,
        message=scenario.message,
        history=list(scenario.history),
    )
    runtime_config = regression_harness.make_runtime_config(
        chat_inputs=chat_inputs,
        execution_mode=mode,
        metadata=scenario.metadata,
    )

    facade = LangGraphChatFacade()
    monkeypatch.setattr(
        facade._context_builder,
        "build_runtime_config",
        lambda **kwargs: runtime_config,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_SIMPLE_TOOL",
        True,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_DEEP_REASONING",
        True,
    )

    async def _skip_intent_classifier(config: LangGraphRuntimeConfig) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(facade._intent_classifier, "enrich_runtime_config", _skip_intent_classifier)
    facade._handlers = {
        ChatBranch.NORMAL_CHAT: _StubBranchHandler("normal_chat", ("assistant_final",)),
        ChatBranch.SIMPLE_TOOL: _StubBranchHandler(
            "simple_tool_execution",
            ("tool_start", "tool_delta", "tool_end", "assistant_final"),
        ),
        ChatBranch.DEEP_REASONING: _StubBranchHandler("deep_reasoning", ("assistant_final",)),
    }

    result = await facade.handle_turn(chat_inputs, metadata={})
    observed_event_types = [event["type"] async for event in result.iter_events()]

    assert result.metadata["handled_branch"] == scenario.expected_branch
    assert observed_event_types == list(scenario.expected_event_types)


@pytest.mark.parametrize(
    "scenario_id",
    [
        pytest.param("history_empty", marks=pytest.mark.regression_quick),
        "history_short",
        "history_truncated",
    ],
)
def test_history_scenarios_preserve_prompt_and_metadata_contracts(
    scenario_id: str,
    regression_harness,
    regression_scenario_index,
) -> None:
    scenario = regression_scenario_index[scenario_id]
    metadata = regression_harness.build_history_metadata(
        message=scenario.message,
        history=list(scenario.history),
    )
    planner_prompt = regression_harness.build_planner_prompt(
        user_message=scenario.message,
        history=list(scenario.history),
    )

    assert metadata["history_turns"] == len(scenario.history)
    assert metadata["conversation_history"] == list(scenario.history)

    assert "DR Planner Input Brief" in planner_prompt
    assert "Previous Conversation" not in planner_prompt
    for turn in scenario.history:
        assert turn["content"] not in planner_prompt


@pytest.mark.parametrize(
    ("scenario_id", "expected_simple_route"),
    [
        ("tool_success_finalize", "format_results"),
        ("tool_retry_call_tool", "select_tool_categories"),
        ("tool_failure_malformed", "format_results"),
    ],
)
def test_tool_scenarios_map_to_expected_routes(
    scenario_id: str,
    expected_simple_route: str,
    regression_harness,
    regression_scenario_index,
) -> None:
    scenario = regression_scenario_index[scenario_id]
    decision_history = list(scenario.metadata.get("decision_history", ()))
    simple_route = regression_harness.route_simple_tool_decision(
        decision_history=decision_history,
        metadata=scenario.metadata,
    )
    deep_route = regression_harness.route_deep_reasoning_decision(
        decision_history=decision_history,
        metadata=scenario.metadata,
    )

    assert simple_route == expected_simple_route
    if scenario_id == "tool_failure_malformed":
        assert deep_route == "finalize"


@pytest.mark.parametrize(
    "scenario_id",
    [
        "hitl_resume_approve",
        "hitl_resume_edit",
        "hitl_resume_skip",
    ],
)
def test_hitl_scenarios_preserve_threading_and_checkpoint_contracts(
    scenario_id: str,
    regression_harness,
    regression_scenario_index,
) -> None:
    scenario = regression_scenario_index[scenario_id]
    conversation_id = scenario.metadata.get("conversation_id")
    anchor_sequence = scenario.metadata.get("anchor_sequence")
    thread_config = regression_harness.make_thread_config(
        conversation_id=conversation_id,
        anchor_sequence=anchor_sequence,
    )

    configurable = thread_config["configurable"]
    assert configurable["thread_id"] == "graph-" + ("a" * 32)

    if scenario_id == "hitl_resume_skip":
        assert configurable["checkpoint_id"] == "17"
    else:
        assert "checkpoint_id" not in configurable
