"""Regression tests for enhanced planner execution and inventory updates."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from pydantic import BaseModel

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.config import AgentConfig
from agent.models import Action, ActionType
from agent.reasoning import EnhancedActionPlanner
from agent.tools import (
    BaseTool,
    ToolResult,
    register_tool,
    register_enhanced_tool_metadata,
)
from agent.tools.enhanced_metadata import (
    EnhancedToolMetadata,
    ToolCapability,
)
from agent.tools.categories import ToolCategory, PentestPhase
from agent.tools.service_matcher import ServiceInfo


def _tool_function_name(tool):
    """Return provider-facing function name from neutral or legacy specs."""
    if hasattr(tool, "name"):
        return tool.name
    return tool["function"]["name"]


class DummyArgs(BaseModel):
    sleep: float = 0.0
    label: str = "done"


class DummyTool(BaseTool):
    args_model = DummyArgs

    def run(self, args: DummyArgs) -> ToolResult:  # pragma: no cover - simple stub
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=args.label,
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=args.sleep,
        )


class FakePlannerLLM:
    async def chat_with_usage(self, _system_prompt, _user_prompt, **_kwargs):
        spec_name = getattr(_kwargs.get("structured_output"), "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": "tests.dummy",
                            "parameters": {"sleep": 0.0},
                        }
                    ],
                    "execution_strategy": "sequential",
                },
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["tests.dummy"],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["tests.dummy"],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(
        self, _system_prompt, _user_prompt, tools, **_kwargs
    ):
        fn_name = _tool_function_name(tools[0])
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps({"sleep": 0.0}),
                )
            ],
            raw={},
            usage=None,
        )


class FakeDuplicatePlannerLLM:
    async def chat_with_usage(self, _system_prompt, _user_prompt, **_kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["tests.dummy"],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["tests.dummy"],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(
        self, _system_prompt, _user_prompt, tools, **_kwargs
    ):
        fn_name = _tool_function_name(tools[0])
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps({"sleep": 0.0, "label": "first"}),
                ),
                SimpleNamespace(
                    id="call2",
                    name=fn_name,
                    arguments=json.dumps({"sleep": 0.0, "label": "second"}),
                ),
            ],
            raw={},
            usage=None,
        )


def test_enhanced_planner_executes_selected_tool():
    register_tool("tests.dummy", DummyTool)
    metadata = EnhancedToolMetadata(
        tool_id="tests.dummy",
        display_name="Dummy",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[ToolCapability(name="sleep", description="")],
        execution_priority=1,
    )
    register_enhanced_tool_metadata(metadata)

    planner = EnhancedActionPlanner(AgentConfig(), llm_client=FakePlannerLLM())

    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )
    context = {
        "current_phase": "reconnaissance",
        "resolved_tools": ["tests.dummy"],
        "history": [],
        "user_message": "run dummy tool",
    }

    result = asyncio.run(planner.plan_and_execute_action(action, context))

    assert result.success
    assert "[tests.dummy] done" in result.stdout


def test_enhanced_planner_executes_duplicate_native_calls_with_distinct_parameters():
    register_tool("tests.dummy", DummyTool)
    metadata = EnhancedToolMetadata(
        tool_id="tests.dummy",
        display_name="Dummy",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[ToolCapability(name="label", description="")],
        execution_priority=1,
    )
    register_enhanced_tool_metadata(metadata)

    config = AgentConfig()
    config.max_committed_tools_per_batch = 2
    planner = EnhancedActionPlanner(config, llm_client=FakeDuplicatePlannerLLM())

    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )
    context = {
        "current_phase": "reconnaissance",
        "resolved_tools": ["tests.dummy"],
        "history": [],
        "user_message": "run dummy tool twice",
    }

    result = asyncio.run(planner.plan_and_execute_action(action, context))

    assert result.success
    assert "[tests.dummy] first" in result.stdout
    assert "[tests.dummy] second" in result.stdout
    assert result.stdout.count("[tests.dummy] second") == 1


def test_update_service_inventory_parses_findings():
    planner = EnhancedActionPlanner(AgentConfig(), llm_client=FakePlannerLLM())
    findings = [
        {"service": "http", "port": 80, "protocol": "tcp", "version": "Apache"}
    ]
    planner._update_service_inventory(findings)
    key = "http_80_tcp"
    assert key in planner.service_inventory.services
    svc = planner.service_inventory.services[key]
    assert svc.name == "http"
    assert svc.port == 80
    assert svc.version == "Apache"


def test_context_enriched_with_discovered_services():
    planner = EnhancedActionPlanner(AgentConfig(), llm_client=FakePlannerLLM())
    planner.service_inventory.add_service(ServiceInfo(name="http", port=80))
    context = {"current_phase": "reconnaissance"}
    enriched = planner._build_context(context)
    assert "discovered_services" in enriched
    assert "http" in enriched["discovered_services"]
