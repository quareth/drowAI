"""Tests for artifact file metadata wiring into planner parameter prompts."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from agent.models import Action, ActionType, ExecutionStrategy
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.reasoning.llm_parameter_resolution import ParameterResolutionResult
from agent.reasoning.llm_tool_selection import ToolSelectionResult
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


class _Config:
    """Minimal planner config for filesystem metadata wiring tests."""

    openai_api_key = "test"
    model_name = "gpt-4"
    max_tools_per_action = 1
    max_committed_tools_per_batch = 1
    default_execution_strategy = "sequential"
    llm_tool_selection_timeout = 5
    tool_call_timeout = 5
    max_tools_exposed = 2


class _Selector:
    async def select_tools(self, **_kwargs: Any) -> ToolSelectionResult:
        return ToolSelectionResult(
            selected_tools=["filesystem.read_file"],
            execution_strategy=ExecutionStrategy.SEQUENTIAL,
            usage_record=None,
        )


class _Resolver:
    async def resolve_parameters(self, **_kwargs: Any) -> ParameterResolutionResult:
        parameters = {
            "filesystem.read_file": {
                "path": "artifacts/scan.xml",
                "read_mode": "head",
                "num_lines": 20,
            }
        }
        return ParameterResolutionResult(
            tool_parameters=parameters,
            llm_tool_parameters=parameters,
            usage_records=[],
        )


def test_try_llm_action_plan_passes_artifact_file_metadata_after_filesystem_selection(
    monkeypatch,
    tmp_path,
) -> None:
    artifact = tmp_path / "artifacts" / "scan.xml"
    artifact.parent.mkdir()
    artifact.write_text("<host />\n", encoding="utf-8")

    captured: List[Dict[str, Any]] = []
    original_select = ToolPlanningPromptBuilder.build_select_tools_prompt
    original_params = ToolPlanningPromptBuilder.build_tool_parameters_prompt

    def _capture_select(self, *args: Any, **kwargs: Any) -> str:
        captured.append({"method": "build_select_tools_prompt", "kwargs": dict(kwargs)})
        return original_select(self, *args, **kwargs)

    def _capture_params(self, *args: Any, **kwargs: Any) -> str:
        captured.append({"method": "build_tool_parameters_prompt", "kwargs": dict(kwargs)})
        return original_params(self, *args, **kwargs)

    monkeypatch.setattr(ToolPlanningPromptBuilder, "build_select_tools_prompt", _capture_select)
    monkeypatch.setattr(ToolPlanningPromptBuilder, "build_tool_parameters_prompt", _capture_params)

    planner = EnhancedActionPlanner(_Config(), llm_client=object())
    planner._tool_selector = _Selector()
    planner._param_resolver = _Resolver()

    action = Action(
        type=ActionType.GATHER_INFO,
        target="localhost",
        parameters={},
        reasoning="",
        expected_outcome="",
    )
    context = {
        "current_phase": "enumeration",
        "resolved_tools": ["filesystem.read_file"],
        "user_message": "inspect saved scan xml",
        "workspace_path": str(tmp_path),
        "artifact_file_refs": [{"path": "artifacts/scan.xml"}],
    }

    asyncio.run(planner.build_action_plan(action, context))

    select_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_select_tools_prompt"
    )
    params_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_tool_parameters_prompt"
    )

    assert "artifact_file_metadata" not in select_kwargs
    assert params_kwargs["artifact_file_metadata"] == [
        {
            "path": "artifacts/scan.xml",
            "status": "ready",
            "size_bytes": artifact.stat().st_size,
            "line_count": 1,
        }
    ]
