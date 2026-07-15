import os
import sys
import asyncio
import json
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from agent.reasoning.enhanced_planner import (
    EnhancedActionPlanner,
    PlannerToolParameterValidationError,
)
from agent.models import Action, ActionType
from agent.tools.tool_registry import available_tools
from core.llm.structured_schemas import TOOL_SELECTOR_STRUCTURED_OUTPUT


class DummyConfig:
    openai_api_key = "test"
    model_name = "gpt-4"
    max_tools_per_action = 3
    default_execution_strategy = "parallel"
    enforce_llm_tool_selection = False
    llm_tool_selection_timeout = 5
    use_llm_tool_calls = True
    max_tools_exposed = 2
    tool_call_timeout = 5


class FakeLLM:
    def __init__(self, tool_id: str):
        self.tool_id = tool_id
        self.exposed_tools = []
        self.selection_structured_output = None

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            # Legacy structured-builder branch. Phase 4 uses native tool
            # calls, so current planner code should not hit this branch.
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": self.tool_id,
                            "parameters": {"target": "127.0.0.1"},
                        }
                    ],
                    "execution_strategy": "sequential",
                },
            )
        self.selection_structured_output = spec
        # Return duplicate tool IDs to verify selector dedupe.
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": [self.tool_id, self.tool_id],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": [self.tool_id, self.tool_id],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(
        self,
        _system_prompt,
        _user_prompt,
        tools,
        **_kwargs,
    ):
        self.exposed_tools.append(list(tools))
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(id="call1", name=fn_name, arguments=json.dumps({"target": "127.0.0.1"})),
            ],
            raw={},
            usage=None,
        )


class FakeBuilderFallbackLLM:
    def __init__(self) -> None:
        self.selection_structured_output = None

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            from agent.providers.llm.core.exceptions import LLMStructuredOutputParseError

            raise LLMStructuredOutputParseError(
                "synthetic builder parse failure",
                provider="test",
                schema_name="commit_tool_batch",
                parse_reason="invalid_json",
                raw_content="",
            )
        self.selection_structured_output = spec
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["shell.exec"],
                    "execution_strategy": "parallel",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["shell.exec"],
                "execution_strategy": "parallel",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="fallback1",
                    name=fn_name,
                    arguments=json.dumps({"command": "echo fallback"}),
                )
            ],
            raw={},
            usage=None,
        )


class FakeShellRepairLLM:
    def __init__(self, initial_arguments: str, retry_arguments: str):
        self.initial_arguments = initial_arguments
        self.retry_arguments = retry_arguments
        self.retry_calls = 0
        self.exposed_tools = []
        self.selection_structured_output = None

    @staticmethod
    def _envelope_from_arguments(arguments: str) -> dict:
        try:
            params = json.loads(arguments)
            if not isinstance(params, dict):
                params = {}
        except Exception:
            params = {}
        return {
            "tool_calls": [{"tool_id": "shell.exec", "parameters": params}],
            "execution_strategy": "sequential",
        }

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output=self._envelope_from_arguments(self.initial_arguments),
            )
        self.selection_structured_output = spec
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["shell.exec"],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["shell.exec"],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        self.exposed_tools.append(list(tools))
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(id="call1", name=fn_name, arguments=self.initial_arguments),
            ],
            raw={},
            usage=None,
        )

    async def chat_with_tools(self, _system_prompt, _user_prompt, tools, **_kwargs):
        self.retry_calls += 1
        fn_name = tools[0]["function"]["name"]
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "retry1",
                    "type": "function",
                    "function": {"name": fn_name, "arguments": self.retry_arguments},
                }
            ],
            "raw": {},
        }


class FakeCveLookupLLM:
    def __init__(self):
        self.selection_structured_output = None

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": "knowledge.cve_lookup",
                            "parameters": {
                                "product": "PostgreSQL",
                                "version": "9.6.0",
                                "max_results": 5,
                            },
                        }
                    ],
                    "execution_strategy": "sequential",
                },
            )
        self.selection_structured_output = spec
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["knowledge.cve_lookup"],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["knowledge.cve_lookup"],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps(
                        {
                            "product": "PostgreSQL",
                            "version": "9.6.0",
                            "max_results": 5,
                        }
                    ),
                )
            ],
            raw={},
            usage=None,
        )


class FakeNmapNoTargetLLM:
    def __init__(self) -> None:
        self.retry_calls = 0

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": "information_gathering.network_discovery.nmap",
                            "parameters": {"scan_types": ["-sV"]},
                        }
                    ],
                    "execution_strategy": "sequential",
                },
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": ["information_gathering.network_discovery.nmap"],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": ["information_gathering.network_discovery.nmap"],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps({"scan_types": ["-sV"]}),
                )
            ],
            raw={},
            usage=None,
        )

    async def chat_with_tools(self, _system_prompt, _user_prompt, **_kwargs):
        self.retry_calls += 1
        return {"content": "", "tool_calls": [], "raw": {}}


class FakeDuplicateNmapBuilderLLM:
    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        tool_id = "information_gathering.network_discovery.nmap"
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": tool_id,
                            "parameters": {
                                "target": "172.17.0.1",
                                "ports": "80",
                                "service_detection": True,
                            },
                        },
                        {
                            "tool_id": tool_id,
                            "parameters": {
                                "target": "172.17.0.1",
                                "ports": "443",
                                "service_detection": True,
                            },
                        },
                    ],
                    "execution_strategy": "parallel",
                },
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": [tool_id],
                    "execution_strategy": "parallel",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": [tool_id],
                "execution_strategy": "parallel",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        fn_name = tools[0]["function"]["name"]
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps(
                        {
                            "target": "172.17.0.1",
                            "ports": "80",
                            "service_detection": True,
                        }
                    ),
                ),
                SimpleNamespace(
                    id="call2",
                    name=fn_name,
                    arguments=json.dumps(
                        {
                            "target": "172.17.0.1",
                            "ports": "443",
                            "service_detection": True,
                        }
                    ),
                ),
            ],
            raw={},
            usage=None,
        )


async def _run(tool_id: str):
    cfg = DummyConfig()
    fake_llm = FakeLLM(tool_id=tool_id)
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")
    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": [tool_id],
            "history": [],
            "user_message": "scan localhost",
        },
    )
    assert plan.selected_tools and isinstance(plan.selected_tools, list)
    assert plan.selected_tools == [tool_id]
    assert plan.tool_batch is not None
    assert plan.tool_batch.tool_calls[0].tool_id == tool_id
    assert plan.tool_parameters[tool_id]["target"] == "127.0.0.1"


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_planner_function_call_flow():
    asyncio.run(_run("information_gathering.network_discovery.nmap"))


@pytest.mark.asyncio
async def test_native_builder_tool_call_uses_selector_strategy():
    cfg = DummyConfig()
    fake_llm = FakeBuilderFallbackLLM()
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run fallback",
        },
    )

    assert plan.selected_tools == ["shell.exec"]
    assert plan.execution_strategy.value == "parallel"
    assert plan.tool_parameters["shell.exec"]["command"] == "echo fallback"


@pytest.mark.asyncio
async def test_planner_does_not_inject_target_for_cve_lookup() -> None:
    cfg = DummyConfig()
    fake_llm = FakeCveLookupLLM()
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["knowledge.cve_lookup"],
            "history": [],
            "user_message": "check cves",
        },
    )

    params = plan.tool_parameters["knowledge.cve_lookup"]
    assert params["product"] == "PostgreSQL"
    assert params["version"] == "9.6.0"
    assert "target" not in params


@pytest.mark.asyncio
async def test_planner_still_injects_target_for_target_based_tools() -> None:
    cfg = DummyConfig()
    fake_llm = FakeNmapNoTargetLLM()
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["information_gathering.network_discovery.nmap"],
            "history": [],
            "user_message": "scan localhost",
        },
    )

    params = plan.tool_parameters["information_gathering.network_discovery.nmap"]
    assert params["target"] == "127.0.0.1"
    assert plan.llm_tool_parameters["information_gathering.network_discovery.nmap"] == {
        "scan_types": ["-sV"]
    }


@pytest.mark.asyncio
async def test_planner_tool_batch_preserves_duplicate_tool_id_parameters() -> None:
    class MultiCallConfig(DummyConfig):
        max_committed_tools_per_batch = 2

    tool_id = "information_gathering.network_discovery.nmap"
    planner = EnhancedActionPlanner(MultiCallConfig(), llm_client=FakeDuplicateNmapBuilderLLM())
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="172.17.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": [tool_id],
            "history": [],
            "user_message": "scan ports 80 and 443 separately",
        },
    )

    assert plan.tool_batch is not None
    calls = list(plan.tool_batch.tool_calls)
    assert [call.tool_id for call in calls] == [tool_id, tool_id]
    assert [call.parameters["ports"] for call in calls] == ["80", "443"]
    assert len({call.tool_call_id for call in calls}) == 2
    assert plan.selected_tools == [tool_id, tool_id]
    assert plan.tool_parameters[tool_id]["ports"] == "80"


@pytest.mark.asyncio
async def test_planner_rejects_missing_target_when_no_target_is_available() -> None:
    cfg = DummyConfig()
    fake_llm = FakeNmapNoTargetLLM()
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="", parameters={}, reasoning="", expected_outcome="")

    with pytest.raises(PlannerToolParameterValidationError) as exc_info:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": ["information_gathering.network_discovery.nmap"],
                "history": [],
                "user_message": "scan network to find online hosts",
            },
        )

    exc = exc_info.value
    assert exc.tool_id == "information_gathering.network_discovery.nmap"
    assert exc.reason == "schema_validation_error"
    assert any(err.get("field") == "target" for err in exc.validation_errors)
    assert fake_llm.retry_calls == 1


@pytest.mark.asyncio
async def test_shell_exec_invalid_json_repaired_once():
    cfg = DummyConfig()
    fake_llm = FakeShellRepairLLM(
        initial_arguments='{"command": ',
        retry_arguments=json.dumps({"command": "echo repaired"}),
    )
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    plan = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run echo",
        },
    )

    assert plan.selected_tools == ["shell.exec"]
    assert plan.tool_parameters["shell.exec"]["command"] == "echo repaired"
    assert fake_llm.selection_structured_output == TOOL_SELECTOR_STRUCTURED_OUTPUT
    assert fake_llm.retry_calls == 1


@pytest.mark.asyncio
async def test_shell_exec_missing_command_raises_typed_planner_validation_error():
    cfg = DummyConfig()
    fake_llm = FakeShellRepairLLM(
        initial_arguments=json.dumps({}),
        retry_arguments=json.dumps({}),
    )
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    with pytest.raises(PlannerToolParameterValidationError) as exc_info:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": ["shell.exec"],
                "history": [],
                "user_message": "run echo",
            },
        )

    exc = exc_info.value
    assert exc.tool_id == "shell.exec"
    assert exc.reason == "schema_validation_error"
    assert any(err.get("field") == "command" for err in exc.validation_errors)
    assert fake_llm.selection_structured_output == TOOL_SELECTOR_STRUCTURED_OUTPUT
    assert fake_llm.retry_calls == 1


@pytest.mark.asyncio
async def test_shell_exec_unparsable_arguments_raise_parse_error():
    cfg = DummyConfig()
    fake_llm = FakeShellRepairLLM(
        initial_arguments='{"command": ',
        retry_arguments='{"command": ',
    )
    planner = EnhancedActionPlanner(cfg, llm_client=fake_llm)
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    with pytest.raises(PlannerToolParameterValidationError) as exc_info:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": ["shell.exec"],
                "history": [],
                "user_message": "run echo",
            },
        )

    exc = exc_info.value
    assert exc.tool_id == "shell.exec"
    assert exc.reason == "parse_error"
    assert any(err.get("field") == "arguments" for err in exc.validation_errors)
    assert fake_llm.selection_structured_output == TOOL_SELECTOR_STRUCTURED_OUTPUT
    assert fake_llm.retry_calls == 1
