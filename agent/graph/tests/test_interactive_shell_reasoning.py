"""Interactive shell continuation tests for main-agent planning flow.

These tests seed the public running-shell result shape produced by shell.exec
and verify the ordinary main-agent planner can commit shell.write_stdin with
the returned public handle.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import agent.tools.shell  # noqa: F401 - register shell tools for planner specs.
from agent.config import AgentConfig
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution_runtime.planner_service import (
    build_action_for_planner,
    build_planner_context,
    ensure_action_plan,
)
from agent.providers.llm.core.base import ToolCall, ToolCallResult
from agent.tool_runtime import ToolExecutionRequest
from agent.tools.tool_call_specs import make_function_name_for_tool


class _PlannerContinuationLLM:
    """Fake two-step planner LLM that selects and commits shell.write_stdin."""

    def __init__(self, public_session_id: str) -> None:
        self.public_session_id = public_session_id
        self.selection_prompts: list[str] = []
        self.parameter_prompts: list[str] = []

    async def chat_with_usage(self, _system_prompt: str, user_prompt: str, **_kwargs: Any):
        self.selection_prompts.append(user_prompt)
        return SimpleNamespace(
            content="",
            structured_output={
                "selected_tools": ["shell.write_stdin"],
                "execution_strategy": "sequential",
                "reasoning": "Poll the still-running shell session.",
            },
            usage=None,
        )

    async def chat_with_tools_with_usage(
        self,
        _system_prompt: str,
        user_prompt: str,
        **_kwargs: Any,
    ) -> ToolCallResult:
        self.parameter_prompts.append(user_prompt)
        return ToolCallResult(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call-shell-stdin",
                    name=make_function_name_for_tool("shell.write_stdin"),
                    arguments=json.dumps(
                        {
                            "session_id": self.public_session_id,
                            "chars": "",
                            "yield_time_ms": 1000,
                            "max_output_chars": 32000,
                            "_builder_intent": "Poll the running shell session.",
                        }
                    ),
                )
            ],
            raw=None,
            usage=None,
        )


@pytest.mark.asyncio
async def test_main_agent_planner_commits_shell_write_stdin_for_running_shell() -> None:
    public_session_id = "shs_main_continuation_123"
    metadata = {
        "last_tool_result": {
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "process_status": "running",
            "session_id": public_session_id,
            "stdout": "started",
            "stderr": "",
            "exit_code": None,
            "stdin_available": True,
            "truncated": False,
            "summary": f"Command is still running; poll session {public_session_id}.",
            "parameters": {"command": "sleep 1; printf done"},
        },
        "last_tool_result_compact": {
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "process_status": "running",
            "session_id": public_session_id,
            "summary": f"Command is still running; poll session {public_session_id}.",
        },
        "tool_intent": {
            "description": "Continue the running shell command.",
            "target": public_session_id,
            "focus": "poll for completion",
        },
    }
    interactive = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue the running shell command.",
            capability="deep_reasoning",
            current_goal="Observe the delayed shell output.",
            next_tool_hint=f"Poll shell session {public_session_id}.",
            metadata=metadata,
        ),
        trace=TraceState(observations=[f"shell.exec returned {public_session_id}"]),
    )
    request = ToolExecutionRequest(
        capability="deep_reasoning",
        targets=[],
        message="Continue the running shell command.",
        task_id=42,
        metadata=interactive.facts.metadata_copy(),
        workspace_path="/workspace",
    )
    llm = _PlannerContinuationLLM(public_session_id)
    config = AgentConfig(openai_api_key=None)
    config.llm_client_resolver = lambda: llm  # type: ignore[attr-defined]

    await ensure_action_plan(
        interactive,
        request,
        config,
        build_action_for_planner=build_action_for_planner,
        build_planner_context=lambda state, req: build_planner_context(
            state,
            req,
            get_category_filtered_catalog=lambda _categories, _config: [
                "shell.exec",
                "shell.write_stdin",
            ],
            get_full_tool_catalog_for_planner=lambda _config: [
                "shell.exec",
                "shell.write_stdin",
            ],
            working_memory_summary_max_chars=2000,
        ),
    )

    plan = interactive.facts.metadata["planner_plan"]
    call = plan["tool_batch"]["tool_calls"][0]
    assert plan["selected_tools"] == ["shell.write_stdin"]
    assert call["tool_id"] == "shell.write_stdin"
    assert call["parameters"]["session_id"] == public_session_id
    assert call["parameters"]["chars"] == ""
    assert public_session_id in llm.parameter_prompts[0]
