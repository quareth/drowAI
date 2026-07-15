from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional

from agent.models import Action, ActionType
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.tools.action_mapper import ContextualToolSelector
from agent.tools.tool_call_specs import build_openai_tool_specs_for
from agent.tools.tool_registry import get_tool

from .mock_executor import MockExecutor
from .mock_llm_client import MockLLMClient


@dataclass
class SimulationResult:
    tool_id: str
    parameters: Dict[str, Any]
    command: list[str]
    metadata: Dict[str, Any]
    success: bool


class PipelineSimulator:
    """Simulate agent tool execution pipeline without real LLM calls."""

    def __init__(
        self,
        *,
        executor: Optional[MockExecutor] = None,
    ) -> None:
        self.executor = executor or MockExecutor()

    async def simulate_action(
        self,
        action_type: ActionType,
        target: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> SimulationResult:
        context = context or {}
        selector = ContextualToolSelector()
        tool_ids = selector.select_tools_for_action(action_type, context)
        specs, fn_map = build_openai_tool_specs_for(tool_ids)

        planner = EnhancedActionPlanner(SimpleNamespace())
        planner.llm_client = MockLLMClient(fn_map)

        action = Action(type=action_type, target=target, reasoning="simulated")
        plan = await planner.build_action_plan(action, context)

        if not plan.selected_tools:
            raise RuntimeError("No tools selected for simulation.")

        tool_id = list(plan.selected_tools)[0]
        params = dict(plan.tool_parameters.get(tool_id, {}))
        params.setdefault("target", target)

        tool_cls = get_tool(tool_id)
        args_instance = tool_cls.args_model(**params)
        tool = tool_cls()
        command = tool.build_command(args_instance)

        stdout, stderr, exit_code = self.executor.run(tool_id, command)
        metadata = tool.parse_output(stdout, stderr, exit_code, args_instance)

        return SimulationResult(
            tool_id=tool_id,
            parameters=params,
            command=command,
            metadata=metadata,
            success=exit_code == 0,
        )
