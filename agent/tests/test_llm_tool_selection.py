"""Focused unit tests for LLMToolSelector extraction behavior.

These tests validate tool selection parsing and normalization in isolation
without depending on EnhancedActionPlanner orchestration.
"""

import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from agent.models import ExecutionStrategy
from agent.providers.llm.core.exceptions import LLMStructuredOutputParseError
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.reasoning.llm_tool_selection import LLMToolSelector
from agent.reasoning.structured_contract_recovery import StructuredContractViolationError
from agent.reasoning.tool_selection_sentinel import UNAVAILABLE_CAPABILITY_TOOL
from agent.models import Action, ActionType
from core.llm import LLMTimeoutError


def _tool_function_name(tool) -> str:
    """Return the provider function name from neutral or legacy tool specs."""
    if isinstance(tool, dict):
        return tool["function"]["name"]
    return tool.name


class StructuredSelectionLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **_kwargs):
        return SimpleNamespace(
            content=json.dumps(self.payload),
            usage=None,
            structured_output=self.payload,
        )


class PlainTextSelectionLLM:
    def __init__(self, content: str):
        self.content = content

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **_kwargs):
        return SimpleNamespace(content=self.content, usage=None, structured_output=None)


class RecoverySelectionLLM:
    def __init__(self, fallback_content: str):
        self.fallback_content = fallback_content
        self.calls = []

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **kwargs):
        self.calls.append(kwargs)
        if "structured_output" in kwargs:
            raise LLMStructuredOutputParseError(
                "failed structured parse",
                provider="test",
                schema_name="TOOL_SELECTOR_STRUCTURED_OUTPUT",
                parse_reason="schema_mismatch",
                raw_content="{}",
                diagnostics={"path": "$.selected_tools"},
            )
        return SimpleNamespace(content=self.fallback_content, usage=None, structured_output=None)


class SelectorPlannerParityLLM:
    """Drives both the selector + builder calls used by parity tests.

    The selector and the builder both run via ``chat_with_usage`` (Phase 3
    Task 3.0). Dispatch is by the structured-output spec name: the selector
    expects ``tool_selector`` and the builder expects ``commit_tool_batch``.
    """

    def __init__(
        self,
        *,
        selection_content: str,
        structured_output: dict | None = None,
        selection_usage: dict | None = None,
    ) -> None:
        self.selection_content = selection_content
        self.structured_output = structured_output
        self.selection_usage = selection_usage
        self.chat_with_usage_system_prompts: list[str] = []
        self.chat_with_tools_system_prompts: list[str] = []

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **kwargs):
        self.chat_with_usage_system_prompts.append(_system_prompt)
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            # Builder primary call — return a single shell.exec commit. The
            # structured builder schema still carries execution_strategy until
            # the native builder cutover; use a deliberately conflicting
            # default so planner parity tests prove selector ownership.
            strategy = "parallel"
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": "shell.exec",
                            "parameters": {"command": "echo parity"},
                        }
                    ],
                    "execution_strategy": strategy,
                },
            )
        return SimpleNamespace(
            content=self.selection_content,
            usage=self.selection_usage,
            structured_output=self.structured_output,
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        self.chat_with_tools_system_prompts.append(_system_prompt)
        # Repair fallback path (only invoked if the structured-output builder
        # call raises). Mirrors the provider tool-call response shape.
        fn_name = _tool_function_name(tools[0])
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments='{"command":"echo parity"}',
                )
            ],
            raw={},
            usage=None,
        )


class RetryThenSuccessSelectionLLM:
    def __init__(self) -> None:
        self.selection_calls = 0

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return SimpleNamespace(
                content="",
                usage=None,
                structured_output={
                    "tool_calls": [
                        {
                            "tool_id": "shell.exec",
                            "parameters": {"command": "echo retried"},
                        }
                    ],
                    "execution_strategy": "sequential",
                },
            )
        self.selection_calls += 1
        if self.selection_calls == 1:
            return SimpleNamespace(
                content='{"selected_tools":[],"execution_strategy":"sequential"}',
                usage=None,
                structured_output={"selected_tools": [], "execution_strategy": "sequential"},
            )
        return SimpleNamespace(
            content='{"selected_tools":["shell.exec"],"execution_strategy":"sequential"}',
            usage=None,
            structured_output={"selected_tools": ["shell.exec"], "execution_strategy": "sequential"},
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        fn_name = _tool_function_name(tools[0])
        return SimpleNamespace(
            content="",
            tool_calls=[SimpleNamespace(id="call1", name=fn_name, arguments='{"command":"echo retried"}')],
            raw={},
            usage=None,
        )


class TimeoutThenSuccessSelectionLLM(RetryThenSuccessSelectionLLM):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_raised = False

    async def chat_with_usage(self, _system_prompt, _selection_prompt, **kwargs):
        spec = kwargs.get("structured_output")
        spec_name = getattr(spec, "name", None)
        if spec_name == "commit_tool_batch":
            return await super().chat_with_usage(_system_prompt, _selection_prompt, **kwargs)
        if not self.timeout_raised:
            self.timeout_raised = True
            raise LLMTimeoutError(
                task_id=55,
                component="PLANNER",
                operation="tool_selection_llm_call",
                timeout_sec=120,
                outcome="selection_timeout",
            )
        return SimpleNamespace(
            content='{"selected_tools":["shell.exec"],"execution_strategy":"sequential"}',
            usage=None,
            structured_output={"selected_tools": ["shell.exec"], "execution_strategy": "sequential"},
        )


class PlannerParityConfig:
    openai_api_key = "test"
    model_name = "gpt-4"
    max_tools_per_action = 3
    default_execution_strategy = "parallel"
    enforce_llm_tool_selection = False
    llm_tool_selection_timeout = 5
    use_llm_tool_calls = True
    max_tools_exposed = 1
    tool_call_timeout = 5


def _build_selector(llm_client, *, logger_name: str = "test.llm_tool_selector") -> LLMToolSelector:
    return LLMToolSelector(
        llm_client=llm_client,
        config=SimpleNamespace(),
        logger=logging.getLogger(logger_name),
    )


@pytest.fixture(autouse=True)
def _planner_tool_visibility_for_tests(monkeypatch):
    """Keep planner parity tests independent from runtime catalog hiding."""
    monkeypatch.setattr(
        "agent.reasoning.enhanced_planner_impl.filter_visible_tool_ids",
        lambda tool_ids, **_kwargs: [str(tool_id) for tool_id in tool_ids if tool_id],
    )


@pytest.mark.asyncio
async def test_select_tools_uses_structured_output_payload():
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["net.nmap"],
            "execution_strategy": "sequential",
            "reasoning": "net.nmap matches the requested network scan.",
        }
    )
    selector = _build_selector(llm)

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}],
        limited_tool_list=["net.nmap"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )

    assert result.selected_tools == ["net.nmap"]
    assert result.execution_strategy == ExecutionStrategy.SEQUENTIAL
    assert result.reasoning == "net.nmap matches the requested network scan."


@pytest.mark.asyncio
async def test_select_tools_accepts_unavailable_capability_sentinel():
    llm = StructuredSelectionLLM(
        {
            "selected_tools": [UNAVAILABLE_CAPABILITY_TOOL],
            "execution_strategy": "sequential",
        }
    )
    selector = _build_selector(llm)

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}],
        limited_tool_list=["net.nmap"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )

    assert result.selected_tools == [UNAVAILABLE_CAPABILITY_TOOL]
    assert result.candidate_tools == [UNAVAILABLE_CAPABILITY_TOOL]


@pytest.mark.asyncio
async def test_select_tools_rejects_unavailable_capability_mixed_with_real_tool():
    llm = StructuredSelectionLLM(
        {
            "selected_tools": [UNAVAILABLE_CAPABILITY_TOOL, "net.nmap"],
            "execution_strategy": "sequential",
        }
    )
    selector = _build_selector(llm)

    with pytest.raises(StructuredContractViolationError) as exc_info:
        await selector.select_tools(
            system_prompt="sys",
            selection_prompt="select",
            catalog=[{"id": "net.nmap"}],
            limited_tool_list=["net.nmap"],
            default_strategy=ExecutionStrategy.PARALLEL,
            max_tools=3,
            timeout_s=5,
        )

    assert exc_info.value.error_code == "structured_contract_semantic_validation"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_select_tools_recovers_to_plain_text_json_after_structured_error():
    llm = RecoverySelectionLLM(
        fallback_content='{"selected_tools":["net.nmap"],"execution_strategy":"parallel"}'
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.recovery")

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}],
        limited_tool_list=["net.nmap"],
        default_strategy=ExecutionStrategy.SEQUENTIAL,
        max_tools=3,
        timeout_s=5,
    )

    assert len(llm.calls) == 2
    assert "structured_output" in llm.calls[0]
    assert "structured_output" not in llm.calls[1]
    assert result.selected_tools == ["net.nmap"]
    assert result.execution_strategy == ExecutionStrategy.PARALLEL


@pytest.mark.asyncio
async def test_select_tools_parses_text_json_when_structured_output_missing():
    llm = PlainTextSelectionLLM(
        content='```json\n{"selected_tools":["net.nmap"],"execution_strategy":"sequential"}\n```'
    )
    selector = _build_selector(llm)

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}],
        limited_tool_list=["net.nmap"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=2,
        timeout_s=5,
    )

    assert result.selected_tools == ["net.nmap"]
    assert result.execution_strategy == ExecutionStrategy.SEQUENTIAL


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_strategy", [None, "", "batched"])
async def test_select_tools_rejects_missing_or_invalid_execution_strategy(raw_strategy):
    payload = {"selected_tools": ["net.nmap"]}
    if raw_strategy is not None:
        payload["execution_strategy"] = raw_strategy
    llm = StructuredSelectionLLM(payload)
    selector = _build_selector(llm)

    with pytest.raises(StructuredContractViolationError) as exc_info:
        await selector.select_tools(
            system_prompt="sys",
            selection_prompt="select",
            catalog=[{"id": "net.nmap"}],
            limited_tool_list=["net.nmap"],
            default_strategy=ExecutionStrategy.PARALLEL,
            max_tools=2,
            timeout_s=5,
        )

    assert exc_info.value.error_code == "structured_contract_schema_validation"
    assert exc_info.value.stage == "tool_selector"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_select_tools_filters_unknown_tools_with_warning(caplog):
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["unknown.tool", "NET.NMAP"],
            "execution_strategy": "sequential",
        }
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.filter")

    with caplog.at_level(logging.WARNING):
        result = await selector.select_tools(
            system_prompt="sys",
            selection_prompt="select",
            catalog=[{"id": "net.nmap"}],
            limited_tool_list=["net.nmap"],
            default_strategy=ExecutionStrategy.PARALLEL,
            max_tools=3,
            timeout_s=5,
        )

    assert result.selected_tools == ["net.nmap"]
    assert any("Tools filtered out (not in catalog)" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_select_tools_deduplicates_and_honors_max_tools_cap():
    """After Phase 3 Task 3.1.5 the selector honors ``max_tools`` instead of
    hard-trimming to a single tool. Duplicates are still collapsed."""
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["net.nmap", "net.nmap", "web.fetch"],
            "execution_strategy": "parallel",
        }
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.dedupe")

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}, {"id": "web.fetch"}],
        limited_tool_list=["net.nmap", "web.fetch"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )

    assert result.selected_tools == ["net.nmap", "web.fetch"]


@pytest.mark.asyncio
async def test_select_tools_legacy_single_tool_cap_still_honored():
    """``max_tools=1`` (legacy override) still trims to one candidate."""
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["net.nmap", "web.fetch"],
            "execution_strategy": "sequential",
        }
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.cap1")

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}, {"id": "web.fetch"}],
        limited_tool_list=["net.nmap", "web.fetch"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=1,
        timeout_s=5,
    )

    assert result.selected_tools == ["net.nmap"]


@pytest.mark.asyncio
async def test_select_tools_raises_when_selection_is_empty():
    llm = StructuredSelectionLLM({"selected_tools": [], "execution_strategy": "sequential"})
    selector = _build_selector(llm)

    with pytest.raises(StructuredContractViolationError) as exc_info:
        await selector.select_tools(
            system_prompt="sys",
            selection_prompt="select",
            catalog=[{"id": "net.nmap"}],
            limited_tool_list=["net.nmap"],
            default_strategy=ExecutionStrategy.PARALLEL,
            max_tools=3,
            timeout_s=5,
        )
    assert exc_info.value.error_code == "structured_contract_semantic_validation"
    assert exc_info.value.stage == "tool_selector"


async def _run_selector_parity(
    *,
    selection_content: str,
    structured_output: dict | None = None,
    selection_usage: dict | None = None,
):
    selector_llm = SelectorPlannerParityLLM(
        selection_content=selection_content,
        structured_output=structured_output,
        selection_usage=selection_usage,
    )
    planner_llm = SelectorPlannerParityLLM(
        selection_content=selection_content,
        structured_output=structured_output,
        selection_usage=selection_usage,
    )

    selector = _build_selector(selector_llm, logger_name="test.llm_tool_selector.parity.selector")
    planner = EnhancedActionPlanner(PlannerParityConfig(), llm_client=planner_llm)
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    selector_result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "shell.exec"}],
        limited_tool_list=["shell.exec"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )
    planner_result = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run shell command",
        },
    )

    return selector_result, planner_result


@pytest.mark.asyncio
async def test_selector_output_matches_planner_for_structured_payload():
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "model": "gpt-4",
        "provider": "openai",
    }
    selector_result, planner_result = await _run_selector_parity(
        selection_content='{"selected_tools":["shell.exec"],"execution_strategy":"sequential"}',
        structured_output={
            "selected_tools": ["shell.exec", "shell.exec"],
            "execution_strategy": "sequential",
        },
        selection_usage=usage,
    )

    assert selector_result.selected_tools == planner_result.selected_tools
    assert selector_result.execution_strategy == ExecutionStrategy.SEQUENTIAL
    assert planner_result.execution_strategy == ExecutionStrategy.SEQUENTIAL
    assert selector_result.usage_record == planner_result.usage_records[0]


@pytest.mark.asyncio
async def test_selector_output_matches_planner_for_text_json_payload():
    selector_result, planner_result = await _run_selector_parity(
        selection_content='```json\n{"selected_tools":["shell.exec"],"execution_strategy":"parallel"}\n```',
    )

    assert selector_result.selected_tools == planner_result.selected_tools
    assert selector_result.execution_strategy == ExecutionStrategy.PARALLEL
    assert planner_result.execution_strategy == ExecutionStrategy.PARALLEL


@pytest.mark.asyncio
async def test_selector_output_matches_planner_for_invalid_catalog_selection():
    invalid_content = '{"selected_tools":["unknown.tool"],"execution_strategy":"sequential"}'
    selector = _build_selector(
        SelectorPlannerParityLLM(selection_content=invalid_content),
        logger_name="test.llm_tool_selector.parity.invalid",
    )
    planner = EnhancedActionPlanner(
        PlannerParityConfig(),
        llm_client=SelectorPlannerParityLLM(selection_content=invalid_content),
    )
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    with pytest.raises(StructuredContractViolationError) as selector_exc:
        await selector.select_tools(
            system_prompt="sys",
            selection_prompt="select",
            catalog=[{"id": "shell.exec"}],
            limited_tool_list=["shell.exec"],
            default_strategy=ExecutionStrategy.PARALLEL,
            max_tools=3,
            timeout_s=5,
        )
    assert selector_exc.value.error_code == "structured_contract_semantic_validation"

    with pytest.raises(StructuredContractViolationError) as planner_exc:
        await planner.build_action_plan(
            action,
            {
                "current_phase": "enumeration",
                "resolved_tools": ["shell.exec"],
                "history": [],
                "user_message": "run shell command",
            },
        )
    assert planner_exc.value.error_code == "structured_contract_semantic_validation"


@pytest.mark.asyncio
async def test_planner_retries_structured_contract_violation_and_recovers():
    planner = EnhancedActionPlanner(
        PlannerParityConfig(),
        llm_client=RetryThenSuccessSelectionLLM(),
    )
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run shell command",
        },
    )

    assert result.selected_tools == ["shell.exec"]


@pytest.mark.asyncio
async def test_planner_retries_llm_timeout_and_recovers():
    llm = TimeoutThenSuccessSelectionLLM()
    planner = EnhancedActionPlanner(
        PlannerParityConfig(),
        llm_client=llm,
    )
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run shell command",
        },
    )

    assert llm.timeout_raised is True
    assert result.selected_tools == ["shell.exec"]


@pytest.mark.asyncio
async def test_planner_unavailable_capability_skips_parameter_builder():
    llm = SelectorPlannerParityLLM(
        selection_content=(
            '{"selected_tools":["unavailable_capability"],'
            '"execution_strategy":"sequential"}'
        ),
        structured_output={
            "selected_tools": [UNAVAILABLE_CAPABILITY_TOOL],
            "execution_strategy": "sequential",
        },
    )
    planner = EnhancedActionPlanner(PlannerParityConfig(), llm_client=llm)
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run unavailable capability",
        },
    )

    assert result.selected_tools == [UNAVAILABLE_CAPABILITY_TOOL]
    assert result.tool_batch is None
    assert result.tool_parameters == {}
    assert len(llm.chat_with_usage_system_prompts) == 1
    assert not llm.chat_with_tools_system_prompts


@pytest.mark.asyncio
async def test_planner_uses_native_builder_system_for_parameter_call():
    llm = SelectorPlannerParityLLM(
        selection_content='{"selected_tools":["shell.exec"],"execution_strategy":"sequential"}',
        structured_output={
            "selected_tools": ["shell.exec"],
            "execution_strategy": "sequential",
        },
    )
    planner = EnhancedActionPlanner(PlannerParityConfig(), llm_client=llm)
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    await planner.build_action_plan(
        action,
        {
            "current_phase": "enumeration",
            "resolved_tools": ["shell.exec"],
            "history": [],
            "user_message": "run shell command",
        },
    )

    assert llm.chat_with_usage_system_prompts
    assert "pentest tool caller" in llm.chat_with_usage_system_prompts[0]
    assert llm.chat_with_tools_system_prompts
    parameter_system_prompt = llm.chat_with_tools_system_prompts[0]
    assert "You are the native tool-call builder" in parameter_system_prompt
    assert "Candidate Tools section of the current turn input" in parameter_system_prompt
    assert "Turn Execution Brief above" not in parameter_system_prompt


# --- Phase 2 Tests: candidate_tools migration alias ---


@pytest.mark.asyncio
async def test_candidate_tools_alias_present():
    """Selector populates the new ``candidate_tools`` field on every result."""
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["net.nmap"],
            "execution_strategy": "sequential",
        }
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.candidate_alias")

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}],
        limited_tool_list=["net.nmap"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )

    assert hasattr(result, "candidate_tools")
    assert result.candidate_tools == ["net.nmap"]


@pytest.mark.asyncio
async def test_candidate_tools_equal_selected_tools_during_migration():
    """During the Phase 2 → 3 migration the alias mirrors selected_tools."""
    llm = StructuredSelectionLLM(
        {
            "selected_tools": ["net.nmap", "net.nmap", "web.fetch"],
            "execution_strategy": "parallel",
        }
    )
    selector = _build_selector(llm, logger_name="test.llm_tool_selector.candidate_parity")

    result = await selector.select_tools(
        system_prompt="sys",
        selection_prompt="select",
        catalog=[{"id": "net.nmap"}, {"id": "web.fetch"}],
        limited_tool_list=["net.nmap", "web.fetch"],
        default_strategy=ExecutionStrategy.PARALLEL,
        max_tools=3,
        timeout_s=5,
    )

    assert result.candidate_tools == result.selected_tools
