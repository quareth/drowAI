"""Focused unit tests for LLMParameterResolver extraction behavior.

These tests validate parsing, retry, validation, repair, and guardrail logic
in isolation without depending on EnhancedActionPlanner orchestration.
"""

import copy
import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from agent.models import Action, ActionType
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.reasoning.enhanced_planner import (
    _convert_usage_to_dict as convert_planner_usage_to_dict,
)
from agent.reasoning.llm_parameter_resolution import (
    LLMParameterResolver,
    PlannerToolParameterValidationError,
    _convert_usage_to_dict as convert_parameter_usage_to_dict,
)
from agent.reasoning.structured_contract_recovery import StructuredContractViolationError
from agent.tools.tool_call_specs import build_openai_tool_specs_for
from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData
from core.llm import LLMTimeoutError


def _tool_function_name(tool):
    """Return provider-facing function name from neutral or legacy specs."""
    if hasattr(tool, "name"):
        return tool.name
    return tool["function"]["name"]


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
    require_all_selected_tools = False
    retry_missing_tool_calls = 1
    fill_defaults_for_missing = True


class TestUsageProjectionHelpers:
    """Planner usage projection helpers preserve provider-specific metadata."""

    def test_parameter_resolver_dict_fallback_preserves_provider_components(self):
        components = {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 100, "output_tokens": 50},
        }

        result = convert_parameter_usage_to_dict(
            {
                "prompt_tokens": 150,
                "completion_tokens": 50,
                "total_tokens": 200,
                "model": "claude-sonnet-4-5",
                "provider": "anthropic",
                "api_surface": "messages",
                "cache_reporting": "unknown",
                "provider_usage_components": components,
            },
            "planner_parameter_generation",
        )

        assert result is not None
        assert result["api_surface"] == "messages"
        assert result["cache_reporting"] == "unknown"
        assert result["provider_usage_components"] == components

    def test_planner_object_fallback_preserves_provider_components(self):
        class UsageLike:
            prompt_tokens = 150
            completion_tokens = 50
            total_tokens = 200
            model = "claude-sonnet-4-5"
            provider = "anthropic"
            cached_tokens = 0
            reasoning_tokens = 0
            api_surface = "messages"
            cache_reporting = "unknown"
            provider_usage_components = ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={"input_tokens": 100, "output_tokens": 50},
            )

            def to_dict(self, _source):
                raise RuntimeError("force fallback")

        result = convert_planner_usage_to_dict(UsageLike(), "planner")

        assert result is not None
        assert result["api_surface"] == "messages"
        assert result["cache_reporting"] == "unknown"
        assert result["provider_usage_components"] == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_canonical_to_dict_path_already_preserves_components(self):
        usage = UsageData(
            prompt_tokens=150,
            completion_tokens=50,
            total_tokens=200,
            model="claude-sonnet-4-5",
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={"input_tokens": 100, "output_tokens": 50},
            ),
        )

        result = convert_parameter_usage_to_dict(usage, "planner_parameter_generation")

        assert result is not None
        assert result["provider_usage_components"]["provider"] == "anthropic"


class DummyParameterGenerator:
    def __init__(self, defaults_by_tool=None):
        self.defaults_by_tool = defaults_by_tool or {}

    def generate_parameters(self, tool_id, context):  # noqa: ARG002
        return copy.deepcopy(self.defaults_by_tool.get(tool_id, {}))


class FakeParameterLLM:
    """Fake builder LLM exercising the native tool-call builder path.

    ``fn_map`` (function-name → tool-id) is required so the synthesized
    envelope can address tool ids canonically in legacy ``chat_with_usage``
    call sites that still appear in selector/planner parity tests.
    """

    def __init__(
        self,
        *,
        initial_tool_calls,
        retry_tool_calls=None,
        usage=None,
        fn_map=None,
        provider=None,
        model=None,
    ):
        self.initial_tool_calls = list(initial_tool_calls)
        self.retry_tool_calls = [list(calls) for calls in (retry_tool_calls or [])]
        self.usage = usage
        self.retry_call_count = 0
        self._fn_map = dict(fn_map or {})
        self.chat_with_usage_kwargs = []
        self.chat_with_tools_with_usage_kwargs = []
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model

    def _build_envelope(self):
        """Project ``initial_tool_calls`` into the builder envelope shape."""
        tool_calls = []
        for call in self.initial_tool_calls:
            fn_name = call.get("name", "")
            tool_id = self._fn_map.get(fn_name) or fn_name
            args_raw = call.get("arguments")
            if isinstance(args_raw, str):
                try:
                    parameters = json.loads(args_raw)
                except Exception:
                    parameters = {"_invalid_json_arguments": args_raw}
            elif isinstance(args_raw, dict):
                parameters = args_raw
            else:
                parameters = {}
            entry = {"tool_id": tool_id, "parameters": parameters}
            tool_calls.append(entry)
        if not tool_calls:
            return {
                "tool_calls": [],
                "execution_strategy": "sequential",
            }
        return {
            "tool_calls": tool_calls,
            "execution_strategy": "sequential",
        }

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        self.chat_with_usage_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            content="",
            usage=self.usage,
            structured_output=self._build_envelope(),
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **kwargs):
        self.chat_with_tools_with_usage_kwargs.append({"tools": tools, **dict(kwargs)})
        tool_calls = []
        for i, call in enumerate(self.initial_tool_calls, start=1):
            tool_calls.append(
                SimpleNamespace(
                    id=f"call{i}",
                    name=call["name"],
                    arguments=call["arguments"],
                )
            )
        return SimpleNamespace(content="", tool_calls=tool_calls, raw={"tools": tools}, usage=self.usage)

    async def chat_with_tools(self, _system_prompt, _user_prompt, **_kwargs):
        self.retry_call_count += 1
        calls = self.retry_tool_calls[self.retry_call_count - 1] if self.retry_call_count <= len(self.retry_tool_calls) else []
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"retry{idx}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for idx, call in enumerate(calls, start=1)
            ],
            "raw": {},
        }


class NormalizedRepairLLM(FakeParameterLLM):
    """Fake repair response using provider-neutral ToolCallResult shape."""

    async def chat_with_tools(self, _system_prompt, _user_prompt, **_kwargs):
        self.retry_call_count += 1
        calls = self.retry_tool_calls[
            self.retry_call_count - 1
        ] if self.retry_call_count <= len(self.retry_tool_calls) else []
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id=f"retry{idx}",
                    name=call["name"],
                    arguments=call["arguments"],
                )
                for idx, call in enumerate(calls, start=1)
            ],
            raw={},
        )


class TimeoutParameterLLM:
    async def chat_with_usage(self, *_args, **_kwargs):
        await asyncio.sleep(0.05)
        return SimpleNamespace(content="", usage=None, structured_output=None)

    async def chat_with_tools_with_usage(self, *_args, **_kwargs):
        await asyncio.sleep(0.05)
        return SimpleNamespace(content="", tool_calls=[], raw={}, usage=None)


class ScriptedParityLLM:
    """Shared fake LLM used to compare resolver output with planner output.

    Handles three structured-output flavors on a single ``chat_with_usage``:
    the selector envelope (``selected_tools`` schema), a legacy builder
    envelope branch kept to catch accidental old-path calls, and any other
    call that returns plain content. Dispatch is by the requested
    ``structured_output`` spec name.
    """

    def __init__(
        self,
        *,
        selected_tools,
        initial_tool_calls,
        retry_tool_calls=None,
        parameter_usage=None,
    ):
        self.selected_tools = list(selected_tools)
        self.initial_tool_calls = list(initial_tool_calls)
        self.retry_tool_calls = [list(calls) for calls in (retry_tool_calls or [])]
        self.parameter_usage = parameter_usage
        self.retry_call_count = 0
        self.retry_payloads = []
        self.exposed_tools = []

    def _build_builder_envelope(self):
        """Project initial_tool_calls into the builder structured-output envelope."""
        tool_calls = []
        for idx, call in enumerate(self.initial_tool_calls):
            tool_id = self.selected_tools[idx] if idx < len(self.selected_tools) else self.selected_tools[0]
            args_raw = call.get("arguments")
            if isinstance(args_raw, str):
                try:
                    parameters = json.loads(args_raw)
                except Exception:
                    parameters = {}
            elif isinstance(args_raw, dict):
                parameters = args_raw
            else:
                parameters = {}
            tool_calls.append({"tool_id": tool_id, "parameters": parameters})
        return {
            "tool_calls": tool_calls,
            "execution_strategy": "sequential",
        }

    async def chat_with_usage(self, _system_prompt, _user_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=self.parameter_usage,
                structured_output=self._build_builder_envelope(),
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": self.selected_tools,
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": self.selected_tools,
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(
        self, _system_prompt, _user_prompt, tools, **_kwargs
    ):
        self.exposed_tools.append(list(tools))
        tool_calls = []
        for idx, call in enumerate(self.initial_tool_calls, start=1):
            function_name = call.get("name") or _tool_function_name(tools[0])
            tool_calls.append(
                SimpleNamespace(
                    id=f"call{idx}",
                    name=function_name,
                    arguments=call["arguments"],
                )
            )
        return SimpleNamespace(
            content="",
            tool_calls=tool_calls,
            raw={},
            usage=self.parameter_usage,
        )

    async def chat_with_tools(self, _system_prompt, user_prompt, tools, **_kwargs):
        self.retry_call_count += 1
        self.retry_payloads.append(json.loads(user_prompt))
        calls = self.retry_tool_calls[self.retry_call_count - 1] if self.retry_call_count <= len(self.retry_tool_calls) else []
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"retry{idx}",
                    "type": "function",
                    "function": {
                        "name": call.get("name") or _tool_function_name(tools[0]),
                        "arguments": call["arguments"],
                    },
                }
                for idx, call in enumerate(calls, start=1)
            ],
            "raw": {},
        }


def _build_resolver(llm_client, *, config=None, defaults_by_tool=None) -> LLMParameterResolver:
    return LLMParameterResolver(
        llm_client=llm_client,
        config=config or DummyConfig(),
        parameter_generator=DummyParameterGenerator(defaults_by_tool=defaults_by_tool),
        logger=logging.getLogger("test.llm_parameter_resolution"),
    )


def _action(target="127.0.0.1") -> Action:
    return Action(
        type=ActionType.SCAN_PORTS,
        target=target,
        parameters={},
        reasoning="",
        expected_outcome="",
    )


async def _run_resolver_parity_case(
    *,
    config,
    selected_tools,
    initial_tool_calls,
    retry_tool_calls,
    context,
    parameter_usage=None,
):
    resolver_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=initial_tool_calls,
        retry_tool_calls=retry_tool_calls,
        parameter_usage=parameter_usage,
    )
    planner_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=initial_tool_calls,
        retry_tool_calls=retry_tool_calls,
        parameter_usage=parameter_usage,
    )
    planner = EnhancedActionPlanner(config, llm_client=planner_llm)
    resolver = LLMParameterResolver(
        llm_client=resolver_llm,
        config=config,
        parameter_generator=planner.parameter_generator,
        logger=logging.getLogger("test.llm_parameter_resolution.parity"),
    )
    action = _action()

    specs, fn_map = build_openai_tool_specs_for(list(selected_tools))
    resolver_result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=list(selected_tools),
        action=action,
        context=context,
        specs=specs,
        fn_map=fn_map,
        timeout_s=5,
    )
    planner_result = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": list(selected_tools),
            "history": [],
            "user_message": "run parity check",
            **context,
        },
    )
    return resolver_llm, planner_llm, resolver_result, planner_result


@pytest.mark.asyncio
async def test_resolve_parameters_parses_calls_and_ignores_duplicates():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo first"})},
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo duplicate"})},
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert result.tool_parameters["shell.exec"]["command"] == "echo first"
    assert result.llm_tool_parameters["shell.exec"]["command"] == "echo first"
    assert len(result.usage_records) == 1
    assert result.usage_records[0]["source"] == "planner_parameter_generation"


@pytest.mark.asyncio
async def test_resolve_parameters_strips_builder_intent_and_carries_it_on_envelope():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": "shell_exec_fn",
                "arguments": json.dumps(
                    {"command": "echo hi", "_builder_intent": "verify shell access"}
                ),
            },
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert "_builder_intent" not in result.tool_parameters["shell.exec"]
    assert "_builder_intent" not in result.llm_tool_parameters["shell.exec"]
    envelope_call = result.builder_envelope["tool_calls"][0]
    assert envelope_call["intent"] == "verify shell access"
    assert "_builder_intent" not in envelope_call["parameters"]


@pytest.mark.asyncio
async def test_resolve_parameters_sends_candidate_function_schemas_to_builder():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo schema"})},
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
        provider="openai",
        model="gpt-5.2",
    )
    resolver = _build_resolver(llm)
    specs = [
        {
            "type": "function",
            "function": {
                "name": "shell_exec_fn",
                "description": "Execute shell",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ]

    await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=specs,
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert not llm.chat_with_usage_kwargs
    assert llm.chat_with_tools_with_usage_kwargs
    builder_kwargs = llm.chat_with_tools_with_usage_kwargs[0]
    assert builder_kwargs["tools"] == specs
    assert builder_kwargs["tool_choice"] == "required"
    assert builder_kwargs["parallel_tool_calls"] is True
    assert "structured_output" not in builder_kwargs


@pytest.mark.asyncio
async def test_resolve_parameters_omits_parallel_tool_calls_without_model_capability():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo schema"})},
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    resolver = _build_resolver(llm)
    specs = [
        FunctionToolSpec(
            tool_id="shell.exec",
            name="shell_exec_fn",
            description="Execute shell",
            parameters_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        )
    ]

    await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=specs,
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    builder_kwargs = llm.chat_with_tools_with_usage_kwargs[0]
    assert builder_kwargs["tools"] == specs
    assert builder_kwargs["tool_choice"] == "required"
    assert "parallel_tool_calls" not in builder_kwargs


@pytest.mark.asyncio
async def test_resolve_parameters_omits_parallel_tool_calls_without_resolved_model_identity():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo schema"})},
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=[
            FunctionToolSpec(
                tool_id="shell.exec",
                name="shell_exec_fn",
                description="Execute shell",
                parameters_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            )
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    builder_kwargs = llm.chat_with_tools_with_usage_kwargs[0]
    assert builder_kwargs["tool_choice"] == "required"
    assert "parallel_tool_calls" not in builder_kwargs


@pytest.mark.asyncio
async def test_resolve_parameters_validates_only_builder_committed_subset():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo committed"})},
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec", "filesystem.list_dir"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert list(result.tool_parameters) == ["shell.exec"]
    assert result.tool_parameters["shell.exec"]["command"] == "echo committed"
    assert "filesystem.list_dir" not in result.llm_tool_parameters
    assert llm.retry_call_count == 0


@pytest.mark.asyncio
async def test_resolve_parameters_timeout_logs_canonical_event(
    caplog: pytest.LogCaptureFixture,
):
    resolver = _build_resolver(TimeoutParameterLLM())

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMTimeoutError) as exc_info:
            await resolver.resolve_parameters(
                system_prompt="sys",
                parameters_prompt="params",
                selected_tools=["shell.exec"],
                action=_action(),
                context={"task_id": 321},
                specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
                fn_map={"shell_exec_fn": "shell.exec"},
                timeout_s=0.01,
            )

    assert isinstance(exc_info.value, asyncio.TimeoutError)
    assert exc_info.value.error_code == "llm_timeout"
    assert exc_info.value.retryable is True
    assert exc_info.value.diagnostics == {
        "component": "PLANNER",
        "operation": "builder_tool_calls_llm_call",
        "timeout_sec": 0.01,
        "outcome": "builder_tool_calls_timeout",
        "task_id": "321",
    }
    assert (
        "TIMEOUT | Task 321 | PLANNER | builder_tool_calls_llm_call | "
        "timeout_sec=0.01 | outcome=builder_tool_calls_timeout"
    ) in caplog.text


def test_parse_tool_calls_collects_missing_arguments_for_shell():
    resolver = _build_resolver(FakeParameterLLM(initial_tool_calls=[]))
    parsed, parse_errors = resolver.parse_tool_calls(
        [{"function": {"name": "shell_exec_fn"}}],
        {"shell_exec_fn": "shell.exec"},
    )

    assert parsed == {}
    assert parse_errors["shell.exec"]["message"] == "Missing function arguments payload"


def test_parse_tool_calls_collects_missing_arguments_for_non_shell_tool():
    resolver = _build_resolver(FakeParameterLLM(initial_tool_calls=[]))
    parsed, parse_errors = resolver.parse_tool_calls(
        [{"function": {"name": "cve_lookup_fn"}}],
        {"cve_lookup_fn": "knowledge.cve_lookup"},
    )

    assert parsed == {}
    assert parse_errors["knowledge.cve_lookup"]["message"] == "Missing function arguments payload"


def test_parse_tool_calls_collects_invalid_json_for_shell():
    resolver = _build_resolver(FakeParameterLLM(initial_tool_calls=[]))
    parsed, parse_errors = resolver.parse_tool_calls(
        [{"function": {"name": "shell_exec_fn", "arguments": '{"command": '}}],
        {"shell_exec_fn": "shell.exec"},
    )

    assert parsed == {}
    assert "Invalid JSON arguments" in parse_errors["shell.exec"]["message"]


@pytest.mark.asyncio
async def test_resolve_parameters_does_not_repair_uncommitted_candidates():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo ok"})},
        ],
        retry_tool_calls=[
            [
                {
                    "name": "cve_lookup_fn",
                    "arguments": json.dumps(
                        {
                            "product": "PostgreSQL",
                            "version": "9.6.0",
                            "max_results": 5,
                        }
                    ),
                }
            ],
        ],
        fn_map={"shell_exec_fn": "shell.exec", "cve_lookup_fn": "knowledge.cve_lookup"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec", "knowledge.cve_lookup"],
        action=_action(),
        context={},
        specs=[
            {"type": "function", "function": {"name": "shell_exec_fn"}},
            {"type": "function", "function": {"name": "cve_lookup_fn"}},
        ],
        fn_map={"shell_exec_fn": "shell.exec", "cve_lookup_fn": "knowledge.cve_lookup"},
        timeout_s=5,
    )

    assert result.tool_parameters["shell.exec"]["command"] == "echo ok"
    assert "knowledge.cve_lookup" not in result.tool_parameters
    assert llm.retry_call_count == 0


@pytest.mark.asyncio
async def test_resolve_parameters_repairs_shell_validation_failure():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({})},
        ],
        retry_tool_calls=[
            [{"name": "shell_exec_fn", "arguments": json.dumps({"command": "echo repaired"})}],
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert result.tool_parameters["shell.exec"]["command"] == "echo repaired"
    assert llm.retry_call_count == 1


@pytest.mark.asyncio
async def test_resolve_parameters_repairs_non_shell_validation_failure():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": "cve_lookup_fn",
                "arguments": json.dumps({"product": " ", "version": "9.6.0"}),
            },
        ],
        retry_tool_calls=[
            [
                {
                    "name": "cve_lookup_fn",
                    "arguments": json.dumps(
                        {
                            "product": "PostgreSQL",
                            "version": "9.6.0",
                            "max_results": 3,
                        }
                    ),
                }
            ],
        ],
        fn_map={"cve_lookup_fn": "knowledge.cve_lookup"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["knowledge.cve_lookup"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "cve_lookup_fn"}}],
        fn_map={"cve_lookup_fn": "knowledge.cve_lookup"},
        timeout_s=5,
    )

    assert result.tool_parameters["knowledge.cve_lookup"]["product"] == "PostgreSQL"
    assert result.tool_parameters["knowledge.cve_lookup"]["max_results"] == 3
    assert llm.retry_call_count == 1


@pytest.mark.asyncio
async def test_resolve_parameters_raises_when_shell_repair_fails():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {"name": "shell_exec_fn", "arguments": json.dumps({})},
        ],
        retry_tool_calls=[
            [{"name": "shell_exec_fn", "arguments": json.dumps({})}],
        ],
        fn_map={"shell_exec_fn": "shell.exec"},
    )
    resolver = _build_resolver(llm)

    with pytest.raises(PlannerToolParameterValidationError) as exc_info:
        await resolver.resolve_parameters(
            system_prompt="sys",
            parameters_prompt="params",
            selected_tools=["shell.exec"],
            action=_action(),
            context={},
            specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
            fn_map={"shell_exec_fn": "shell.exec"},
            timeout_s=5,
        )

    exc = exc_info.value
    assert exc.tool_id == "shell.exec"
    assert exc.reason == "schema_validation_error"
    assert any(err.get("field") == "command" for err in exc.validation_errors)
    assert llm.retry_call_count == 1


@pytest.mark.asyncio
async def test_resolve_parameters_requires_explicit_tool_call_for_selected_non_shell_tool():
    llm = FakeParameterLLM(
        initial_tool_calls=[],
        retry_tool_calls=[[]],
        fn_map={"cve_lookup_fn": "knowledge.cve_lookup"},
    )
    resolver = _build_resolver(llm)

    with pytest.raises(StructuredContractViolationError) as exc_info:
        await resolver.resolve_parameters(
            system_prompt="sys",
            parameters_prompt="params",
            selected_tools=["knowledge.cve_lookup"],
            action=_action(),
            context={},
            specs=[{"type": "function", "function": {"name": "cve_lookup_fn"}}],
            fn_map={"cve_lookup_fn": "knowledge.cve_lookup"},
            timeout_s=5,
        )

    exc = exc_info.value
    assert exc.error_code == "structured_contract_schema_validation"
    assert exc.stage == "param_resolver"
    assert exc.contract == "native_tool_call_builder"
    assert exc.retryable is True
    assert llm.retry_call_count == 0


@pytest.mark.asyncio
async def test_resolve_parameters_keeps_raw_llm_parameters_separate_from_autofilled_target():
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": "nmap_fn",
                "arguments": json.dumps({"scan_types": ["-sV"]}),
            },
        ],
        fn_map={"nmap_fn": "information_gathering.network_discovery.nmap"},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["information_gathering.network_discovery.nmap"],
        action=_action(target="127.0.0.1"),
        context={},
        specs=[{"type": "function", "function": {"name": "nmap_fn"}}],
        fn_map={"nmap_fn": "information_gathering.network_discovery.nmap"},
        timeout_s=5,
    )

    assert result.llm_tool_parameters["information_gathering.network_discovery.nmap"] == {
        "scan_types": ["-sV"]
    }
    assert result.tool_parameters["information_gathering.network_discovery.nmap"]["target"] == "127.0.0.1"
    assert result.tool_parameters["information_gathering.network_discovery.nmap"]["scan_types"] == ["-sV"]


@pytest.mark.asyncio
async def test_resolve_parameters_preserves_duplicate_tool_calls_by_manifest_order():
    tool_id = "information_gathering.network_discovery.nmap"
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": "nmap_fn",
                "arguments": json.dumps(
                    {"target": "172.17.0.1", "ports": "80", "service_detection": True}
                ),
            },
            {
                "name": "nmap_fn",
                "arguments": json.dumps(
                    {"target": "172.17.0.1", "ports": "443", "service_detection": True}
                ),
            },
        ],
        fn_map={"nmap_fn": tool_id},
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=[tool_id],
        action=_action(target="172.17.0.1"),
        context={},
        specs=[{"type": "function", "function": {"name": "nmap_fn"}}],
        fn_map={"nmap_fn": tool_id},
        timeout_s=5,
    )

    envelope = result.validated_builder_envelope
    assert envelope is not None
    calls = envelope["tool_calls"]
    assert [call["tool_id"] for call in calls] == [tool_id, tool_id]
    assert [call["parameters"]["ports"] for call in calls] == ["80", "443"]
    assert result.tool_parameters[tool_id]["ports"] == "80"


@pytest.mark.asyncio
async def test_duplicate_tool_call_validation_error_is_call_indexed_without_repair():
    tool_id = "information_gathering.network_discovery.nmap"
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": "nmap_fn",
                "arguments": json.dumps({"target": "172.17.0.1", "ports": "80"}),
            },
            {
                "name": "nmap_fn",
                "arguments": json.dumps({"ports": "443"}),
            },
        ],
        retry_tool_calls=[
            [
                {
                    "name": "nmap_fn",
                    "arguments": json.dumps({"target": "172.17.0.1", "ports": "443"}),
                }
            ]
        ],
        fn_map={"nmap_fn": tool_id},
    )
    resolver = _build_resolver(llm)

    with pytest.raises(PlannerToolParameterValidationError) as exc_info:
        await resolver.resolve_parameters(
            system_prompt="sys",
            parameters_prompt="params",
            selected_tools=[tool_id],
            action=_action(target=""),
            context={},
            specs=[{"type": "function", "function": {"name": "nmap_fn"}}],
            fn_map={"nmap_fn": tool_id},
            timeout_s=5,
        )

    assert exc_info.value.tool_id == tool_id
    assert exc_info.value.validation_errors[0]["tool_call_index"] == "1"
    assert llm.retry_call_count == 0


@pytest.mark.asyncio
async def test_resolve_parameters_preserves_raw_ffuf_planner_payload_and_compiles_execution_args():
    specs, fn_map = build_openai_tool_specs_for(["web_applications.web_application_fuzzers.ffuf"])
    llm = FakeParameterLLM(
        initial_tool_calls=[
            {
                "name": specs[0]["function"]["name"],
                "arguments": json.dumps(
                    {
                        "fuzz_surface": "path",
                        "target_template": "http://10.129.34.166/data/FUZZ",
                        "payload_source": {
                            "kind": "generated_sequence",
                            "start": 1,
                            "end": 300,
                        },
                    }
                ),
            },
        ],
        fn_map=fn_map,
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["web_applications.web_application_fuzzers.ffuf"],
        action=_action(target="http://10.129.34.166/data/2"),
        context={},
        specs=specs,
        fn_map=fn_map,
        timeout_s=5,
    )

    assert result.llm_tool_parameters["web_applications.web_application_fuzzers.ffuf"] == {
        "fuzz_surface": "path",
        "target_template": "http://10.129.34.166/data/FUZZ",
        "payload_source": {
            "kind": "generated_sequence",
            "start": 1,
            "end": 300,
        },
    }
    assert result.tool_parameters["web_applications.web_application_fuzzers.ffuf"]["target"] == "http://10.129.34.166/data/FUZZ"
    inline_wordlist = result.tool_parameters["web_applications.web_application_fuzzers.ffuf"]["inline_wordlist"]
    assert inline_wordlist[0] == "1"
    assert inline_wordlist[-1] == "300"
    assert len(inline_wordlist) == 300


@pytest.mark.asyncio
async def test_parameter_resolver_output_matches_planner_for_direct_tool_call():
    config = DummyConfig()
    selected_tools = ["shell.exec"]
    usage = {
        "prompt_tokens": 9,
        "completion_tokens": 5,
        "total_tokens": 14,
        "model": "gpt-4",
        "provider": "openai",
    }
    resolver_llm, planner_llm, resolver_result, planner_result = await _run_resolver_parity_case(
        config=config,
        selected_tools=selected_tools,
        initial_tool_calls=[{"arguments": json.dumps({"command": "echo parity"})}],
        retry_tool_calls=[],
        context={},
        parameter_usage=usage,
    )

    assert resolver_result.tool_parameters == planner_result.tool_parameters
    assert resolver_result.llm_tool_parameters == planner_result.llm_tool_parameters
    assert resolver_result.usage_records == planner_result.usage_records
    assert resolver_llm.retry_payloads == planner_llm.retry_payloads == []


@pytest.mark.asyncio
async def test_enhanced_planner_exposes_neutral_function_specs_to_parameter_resolver():
    config = DummyConfig()
    selected_tools = ["shell.exec"]
    planner_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=[{"arguments": json.dumps({"command": "echo neutral"})}],
    )
    planner = EnhancedActionPlanner(config, llm_client=planner_llm)

    result = await planner.build_action_plan(
        _action(),
        {
            "current_phase": "enumeration",
            "resolved_tools": selected_tools,
            "history": [],
            "user_message": "run neutral spec check",
        },
    )

    assert result.tool_parameters["shell.exec"]["command"] == "echo neutral"
    assert planner_llm.exposed_tools
    assert all(
        type(tool).__name__ == "FunctionToolSpec"
        and hasattr(tool, "tool_id")
        and hasattr(tool, "parameters_schema")
        for tool in planner_llm.exposed_tools[0]
    )


@pytest.mark.asyncio
async def test_parameter_resolver_empty_native_response_matches_planner_contract_error():
    class RequireAllConfig(DummyConfig):
        require_all_selected_tools = True
        fill_defaults_for_missing = False

    config = RequireAllConfig()
    selected_tools = ["shell.exec"]
    resolver_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=[],
        retry_tool_calls=[[{"arguments": json.dumps({"command": "echo retry"})}]],
    )
    planner_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=[],
        retry_tool_calls=[[{"arguments": json.dumps({"command": "echo retry"})}]],
    )
    resolver = _build_resolver(resolver_llm, config=config)
    planner = EnhancedActionPlanner(config, llm_client=planner_llm)
    action = _action()
    specs, fn_map = build_openai_tool_specs_for(selected_tools)

    with pytest.raises(StructuredContractViolationError) as resolver_exc:
        await resolver.resolve_parameters(
            system_prompt="sys",
            parameters_prompt="params",
            selected_tools=selected_tools,
            action=action,
            context={"tool_intent": {"target": "10.10.10.0/24"}},
            specs=specs,
            fn_map=fn_map,
            timeout_s=5,
        )

    with pytest.raises(StructuredContractViolationError) as planner_exc:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": selected_tools,
                "history": [],
                "user_message": "run parity check",
                "tool_intent": {"target": "10.10.10.0/24"},
            },
        )

    assert resolver_exc.value.contract == planner_exc.value.contract == "native_tool_call_builder"
    assert resolver_llm.retry_call_count == planner_llm.retry_call_count == 0
    assert resolver_llm.retry_payloads == planner_llm.retry_payloads == []


@pytest.mark.asyncio
async def test_parameter_resolver_shell_validation_error_matches_planner_structure():
    config = DummyConfig()
    selected_tools = ["shell.exec"]
    resolver_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=[{"arguments": json.dumps({})}],
        retry_tool_calls=[[{"arguments": json.dumps({})}]],
    )
    planner_llm = ScriptedParityLLM(
        selected_tools=selected_tools,
        initial_tool_calls=[{"arguments": json.dumps({})}],
        retry_tool_calls=[[{"arguments": json.dumps({})}]],
    )
    resolver = _build_resolver(resolver_llm, config=config)
    planner = EnhancedActionPlanner(config, llm_client=planner_llm)
    action = _action()
    specs, fn_map = build_openai_tool_specs_for(selected_tools)

    with pytest.raises(PlannerToolParameterValidationError) as resolver_exc:
        await resolver.resolve_parameters(
            system_prompt="sys",
            parameters_prompt="params",
            selected_tools=selected_tools,
            action=action,
            context={},
            specs=specs,
            fn_map=fn_map,
            timeout_s=5,
        )

    with pytest.raises(PlannerToolParameterValidationError) as planner_exc:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": selected_tools,
                "history": [],
                "user_message": "run parity check",
            },
        )

    assert resolver_exc.value.tool_id == planner_exc.value.tool_id
    assert resolver_exc.value.reason == planner_exc.value.reason
    assert resolver_exc.value.validation_errors == planner_exc.value.validation_errors
    assert resolver_llm.retry_call_count == planner_llm.retry_call_count == 1
    assert resolver_llm.retry_payloads == planner_llm.retry_payloads


@pytest.mark.asyncio
async def test_parameter_repair_consumes_normalized_tool_call_result():
    selected_tools = ["shell.exec"]
    specs, fn_map = build_openai_tool_specs_for(selected_tools)
    shell_function_name = next(
        name for name, tool_id in fn_map.items() if tool_id == "shell.exec"
    )
    llm = NormalizedRepairLLM(
        initial_tool_calls=[
            {
                "name": shell_function_name,
                "arguments": json.dumps({}),
            }
        ],
        retry_tool_calls=[
            [
                {
                    "name": shell_function_name,
                    "arguments": json.dumps({"command": "echo repaired"}),
                }
            ]
        ],
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=selected_tools,
        action=_action(),
        context={},
        specs=specs,
        fn_map=fn_map,
        timeout_s=5,
    )

    assert llm.retry_call_count == 1
    assert result.tool_parameters["shell.exec"]["command"] == "echo repaired"


class _StructuredOutputFailingBuilderLLM:
    """Fake that fails loudly if the old structured builder path is invoked."""

    def __init__(self, *, tool_call):
        self.tool_call = dict(tool_call)
        self.primary_call_count = 0
        self.fallback_call_count = 0

    async def chat_with_usage(self, _system_prompt, _user_prompt, **_kwargs):
        from agent.providers.llm.core.exceptions import LLMStructuredOutputParseError

        self.primary_call_count += 1
        raise LLMStructuredOutputParseError(
            "synthetic structured-output failure",
            provider="OpenAI",
            schema_name="commit_tool_batch",
            parse_reason="invalid_json",
            raw_content="",
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        self.fallback_call_count += 1
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="fallback-1",
                    name=self.tool_call["name"],
                    arguments=self.tool_call["arguments"],
                ),
            ],
            raw={"tools": tools},
            usage=None,
        )


class _ShellEchoEnvelopeRegressionLLM:
    """Fake old envelope plus native response for the shell-echo regression."""

    def __init__(self, *, native_function_name: str):
        self.native_function_name = native_function_name
        self.structured_envelope_calls = 0
        self.native_calls = 0
        self.exposed_tools = []

    async def chat_with_usage(self, _system_prompt, _user_prompt, **_kwargs):
        self.structured_envelope_calls += 1
        return SimpleNamespace(
            content="",
            usage=None,
            structured_output={
                "tool_calls": [
                    {
                        "tool_id": "shell.exec",
                        "parameters": {"command": "echo tool execution disabled"},
                    }
                ],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        self.native_calls += 1
        self.exposed_tools.append(list(tools))
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="native-1",
                    name=self.native_function_name,
                    arguments=json.dumps({"command": "printf native-builder"}),
                )
            ],
            raw={"tools": tools},
            usage=None,
        )


@pytest.mark.asyncio
async def test_resolve_parameters_uses_native_tool_calls_as_primary_builder_path():
    """The builder primary path is native tool calling, not structured output."""
    llm = _StructuredOutputFailingBuilderLLM(
        tool_call={
            "name": "shell_exec_fn",
            "arguments": json.dumps({"command": "echo fallback"}),
        },
    )
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=["shell.exec"],
        action=_action(),
        context={},
        specs=[{"type": "function", "function": {"name": "shell_exec_fn"}}],
        fn_map={"shell_exec_fn": "shell.exec"},
        timeout_s=5,
    )

    assert llm.primary_call_count == 0
    assert llm.fallback_call_count == 1
    assert result.tool_parameters["shell.exec"]["command"] == "echo fallback"


@pytest.mark.asyncio
async def test_shell_echo_json_envelope_is_not_accepted_as_primary_builder_path():
    selected_tools = [
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
        "shell.exec",
    ]
    specs, fn_map = build_openai_tool_specs_for(selected_tools)
    shell_function_name = next(
        name for name, tool_id in fn_map.items() if tool_id == "shell.exec"
    )
    llm = _ShellEchoEnvelopeRegressionLLM(native_function_name=shell_function_name)
    resolver = _build_resolver(llm)

    result = await resolver.resolve_parameters(
        system_prompt="sys",
        parameters_prompt="params",
        selected_tools=selected_tools,
        action=_action(target="10.10.10.0/24"),
        context={},
        specs=specs,
        fn_map=fn_map,
        timeout_s=5,
    )

    assert llm.structured_envelope_calls == 0
    assert llm.native_calls == 1
    assert len(llm.exposed_tools[0]) == 3
    assert list(result.tool_parameters) == ["shell.exec"]
    assert result.tool_parameters["shell.exec"]["command"] == "printf native-builder"
