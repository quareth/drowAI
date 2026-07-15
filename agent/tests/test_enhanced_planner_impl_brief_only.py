"""Guardrail tests: direct-executor planner stack is brief-only.

Phase 3 Task 3.2 completed the cutover of the direct-executor tool-
planning stack from transcript-driven prompts to brief-driven prompts.
These tests lock the post-cutover invariants at the
``EnhancedActionPlanner._try_llm_action_plan`` seam:

- The tool-planning builder is called without any
  ``conversation_history_text`` kwarg (that kwarg has been removed
  from the builder signature and a silent reintroduction here would
  fail the contract).
- The classifier-derived ``intent_brief`` is plumbed into
  both the ``build_select_tools_prompt`` and
  ``build_tool_parameters_prompt`` calls so downstream tool selection
  and parameter generation read their current-turn interpretation
  from the brief exclusively.

A monkey-patched capturing wrapper records each builder call's kwargs,
and the test drives ``_try_llm_action_plan`` end-to-end with a fake
LLM that satisfies both the tool-selection and parameter-generation
paths without making any network calls. The wrapper asserts by
inspection, so the guardrail fails loudly if transcript fanout is
reintroduced at this seam in future work.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent.models import Action, ActionType
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

_VISIBLE_TEST_TOOL_ID = "information_gathering.network_discovery.nmap"


class _BriefOnlyDummyConfig:
    """Minimal config shim with the knobs ``EnhancedActionPlanner`` reads."""

    openai_api_key = "test"
    model_name = "gpt-4"
    max_tools_per_action = 1
    default_execution_strategy = "parallel"
    enforce_llm_tool_selection = False
    llm_tool_selection_timeout = 5
    use_llm_tool_calls = True
    max_tools_exposed = 2
    tool_call_timeout = 5


class _BriefOnlyFakeLLM:
    """Fake LLM that satisfies selection + parameter paths without I/O.

    Dispatch is by structured-output spec name: the selector receives a
    ``tool_selector`` envelope, the builder (Phase 3 Task 3.0) receives a
    ``commit_tool_batch`` envelope. ``chat_with_tools_with_usage`` is the
    repair fallback for the builder.
    """

    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id

    def _parameters(self) -> Dict[str, Any]:
        if self.tool_id == _VISIBLE_TEST_TOOL_ID:
            return {"target": "localhost"}
        return {"command": "echo ok"}

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
                        "tool_id": self.tool_id,
                        "parameters": self._parameters(),
                    }
                ],
                "execution_strategy": "sequential",
                },
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "selected_tools": [self.tool_id],
                    "execution_strategy": "sequential",
                }
            ),
            usage=None,
            structured_output={
                "selected_tools": [self.tool_id],
                "execution_strategy": "sequential",
            },
        )

    async def chat_with_tools_with_usage(self, _system_prompt, _user_prompt, tools, **_kwargs):
        first_tool = tools[0]
        if isinstance(first_tool, dict):
            fn_name = first_tool["function"]["name"]
        else:
            fn_name = first_tool.name
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call1",
                    name=fn_name,
                    arguments=json.dumps(self._parameters()),
                ),
            ],
            raw={},
            usage=None,
        )


_POPULATED_BRIEF: Dict[str, Any] = {
    "resolved_user_intent": "Run an echo smoke check",
    "overall_goal": "Confirm the shell tool is reachable",
    "continuation_mode": "new_request",
    "next_operational_goal": "Invoke nmap against localhost",
    "success_condition": "Receive an nmap result for localhost",
    "execution_readiness": "ready",
    "blocking_reason": None,
    "explicit_constraints": [],
    "suggested_category_focus": ["shell"],
    "retrieval_hints": [],
    "relevant_memory_fragments": [],
    "request_contract": {
        "question_type": "single_step",
        "answer_style": "normal",
        "terminal_when": "step_done",
    },
    "target": {
        "resolved_target": "localhost",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "prior_target_reuse": "allow",
    },
}


def _install_builder_capture(monkeypatch) -> List[Dict[str, Any]]:
    """Patch the tool-planning builder methods to capture call kwargs."""
    captured: List[Dict[str, Any]] = []

    original_select = ToolPlanningPromptBuilder.build_select_tools_prompt
    original_params = ToolPlanningPromptBuilder.build_tool_parameters_prompt

    def _capture_select(self, *args, **kwargs):
        captured.append({"method": "build_select_tools_prompt", "kwargs": dict(kwargs)})
        return original_select(self, *args, **kwargs)

    def _capture_params(self, *args, **kwargs):
        captured.append({"method": "build_tool_parameters_prompt", "kwargs": dict(kwargs)})
        return original_params(self, *args, **kwargs)

    monkeypatch.setattr(
        ToolPlanningPromptBuilder,
        "build_select_tools_prompt",
        _capture_select,
    )
    monkeypatch.setattr(
        ToolPlanningPromptBuilder,
        "build_tool_parameters_prompt",
        _capture_params,
    )
    return captured


def _drive_plan(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    fake_llm = _BriefOnlyFakeLLM(tool_id=_VISIBLE_TEST_TOOL_ID)
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=fake_llm)
    action = Action(
        type=ActionType.GATHER_INFO,
        target="localhost",
        parameters={},
        reasoning="",
        expected_outcome="",
    )
    asyncio.run(planner.build_action_plan(action, context))
    return []  # the captured list is populated by the monkeypatched wrappers


def test_try_llm_action_plan_passes_intent_brief_to_builders(monkeypatch) -> None:
    """Selection and parameter prompts receive ``intent_brief``.

    Phase 3 Task 3.2: the direct-executor planner stack reads its
    current-turn interpretation from the classifier-derived brief
    exclusively. Both builder calls in ``_try_llm_action_plan`` must
    forward ``intent_brief`` verbatim from ``context``.
    """
    captured = _install_builder_capture(monkeypatch)
    context: Dict[str, Any] = {
        "current_phase": "enumeration",
        "resolved_tools": [_VISIBLE_TEST_TOOL_ID],
        "history": [],
        "user_message": "run echo ok",
        "intent_brief": _POPULATED_BRIEF,
    }

    _drive_plan(context)

    methods = [entry["method"] for entry in captured]
    assert "build_select_tools_prompt" in methods
    assert "build_tool_parameters_prompt" in methods

    for entry in captured:
        kwargs = entry["kwargs"]
        assert "intent_brief" in kwargs, (
            f"{entry['method']} called without intent_brief; "
            "direct-executor planner stack must forward the brief into "
            "both tool-selection and parameter-generation prompts."
        )
        assert kwargs["intent_brief"] is _POPULATED_BRIEF

    select_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_select_tools_prompt"
    )
    assert select_kwargs["resolved_tools"] == [_VISIBLE_TEST_TOOL_ID]
    assert select_kwargs["max_tools_per_action"] == _BriefOnlyDummyConfig.max_tools_per_action
    assert select_kwargs["catalog"]
    assert select_kwargs["catalog"][0]["id"] == _VISIBLE_TEST_TOOL_ID
    assert select_kwargs["catalog"][0]["description"]

    params_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_tool_parameters_prompt"
    )
    assert params_kwargs["execution_strategy"] == "sequential"


def test_try_llm_action_plan_does_not_fallback_selector_to_planner_summary(monkeypatch) -> None:
    """Selection uses selector memory only; parameters may use planner summary."""
    captured = _install_builder_capture(monkeypatch)
    context: Dict[str, Any] = {
        "current_phase": "enumeration",
        "resolved_tools": [_VISIBLE_TEST_TOOL_ID],
        "history": [],
        "user_message": "run echo ok",
        "intent_brief": _POPULATED_BRIEF,
        "working_memory_summary": (
            "## Prior Current-Turn Phase Memory\n"
            '<phase turn="12" phase="0" source="tool">\n'
            "## Tool Output\n"
            "Tool: netdiscover\n"
            "Status: failed\n"
            "Failure category: tool_unavailable\n"
            "Summary: bash: netdiscover: command not found\n"
            "</phase>"
        ),
        "latest_phase_memory": (
            "## Latest Current-Turn Phase\n"
            '<phase turn="12" phase="1" source="ptr">\n'
            "## Tool Intent\n"
            "target: localhost\n"
            "</phase>"
        ),
        "referenced_prior_turns": "Referenced Prior Turns:\n- Turn 1 (user): discover hosts",
    }

    _drive_plan(context)

    select_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_select_tools_prompt"
    )
    assert select_kwargs["working_memory_summary"] is None
    assert select_kwargs["latest_phase_memory"] == context["latest_phase_memory"]
    assert select_kwargs["referenced_prior_turns"] == context["referenced_prior_turns"]

    params_kwargs = next(
        entry["kwargs"]
        for entry in captured
        if entry["method"] == "build_tool_parameters_prompt"
    )
    assert params_kwargs["working_memory_summary"] == context["working_memory_summary"]
    assert params_kwargs["referenced_prior_turns"] == context["referenced_prior_turns"]


def test_try_llm_action_plan_does_not_pass_conversation_history_text(monkeypatch) -> None:
    """Neither builder call may carry the removed transcript kwarg.

    Phase 3 Task 3.2 removed ``conversation_history_text`` from every
    public tool-planning builder method. This test guards against a
    regression that reintroduces the kwarg at the
    ``_try_llm_action_plan`` seam — even smuggling it as a silent
    no-op would now cause the builder to raise ``TypeError`` rather
    than render transcript, but the stronger invariant we enforce
    here is that the caller never attempts to pass it.
    """
    captured = _install_builder_capture(monkeypatch)
    context: Dict[str, Any] = {
        "current_phase": "enumeration",
        "resolved_tools": [_VISIBLE_TEST_TOOL_ID],
        "history": [],
        "user_message": "run echo ok",
        "intent_brief": _POPULATED_BRIEF,
    }

    _drive_plan(context)

    for entry in captured:
        kwargs = entry["kwargs"]
        assert "conversation_history_text" not in kwargs, (
            f"{entry['method']} called with removed "
            "conversation_history_text kwarg; direct-executor planner "
            "stack must not plumb transcript text into tool-planning "
            "prompts after the Phase 3 Task 3.2 cutover."
        )


def test_try_llm_action_plan_tolerates_missing_intent_brief(monkeypatch) -> None:
    """Missing briefs resolve to ``None`` rather than a transcript fallback.

    The brief is a soft input during rollout: if the classifier did
    not write one, ``_extract_intent_brief`` returns ``None``
    and the builder renders ``(none)`` placeholders. No transcript
    fallback may be introduced at this seam.
    """
    captured = _install_builder_capture(monkeypatch)
    context: Dict[str, Any] = {
        "current_phase": "enumeration",
        "resolved_tools": [_VISIBLE_TEST_TOOL_ID],
        "history": [],
        "user_message": "run echo ok",
    }

    _drive_plan(context)

    for entry in captured:
        kwargs = entry["kwargs"]
        assert kwargs.get("intent_brief") is None
        assert "conversation_history_text" not in kwargs


def test_llm_catalog_filters_hidden_tools() -> None:
    """LLM-facing planner catalog must not expose hidden tools."""
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=_BriefOnlyFakeLLM("shell.exec"))
    context: Dict[str, Any] = {
        "resolved_tools": [
            "shell.exec",
            "shell.script",
            "artifact.search",
            "filesystem.read_file",
            "information_gathering.network_discovery.netdiscover",
        ],
        "selected_categories": ["shell", "artifact", "filesystem", "information_gathering"],
        "artifact_tool_exposure": {
            "allow_search": False,
            "allow_read": False,
            "has_persisted_artifacts": False,
            "known_artifact_ids": [],
            "evidence_gap_signal": False,
        },
    }

    visible = planner._resolve_tool_catalog_for_llm(
        context=context,
        user_message="inspect files",
    )

    assert "artifact.search" not in visible
    assert "information_gathering.network_discovery.netdiscover" not in visible
    assert "shell.exec" not in visible
    assert "shell.script" not in visible
    assert "filesystem.read_file" in visible


def test_llm_catalog_hides_artifact_overlay_tools_even_when_exposure_allows_them() -> None:
    """Artifact tools do not survive the final visibility guard."""
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=_BriefOnlyFakeLLM("artifact.search"))
    context: Dict[str, Any] = {
        "resolved_tools": [
            "shell.exec",
            "artifact.search",
            "filesystem.read_file",
        ],
        "artifact_tool_exposure": {
            "allow_search": True,
            "allow_read": False,
            "has_persisted_artifacts": True,
            "known_artifact_ids": [],
            "evidence_gap_signal": False,
        },
    }

    visible = planner._resolve_tool_catalog_for_llm(
        context=context,
        user_message="search prior outputs",
    )

    assert "artifact.search" not in visible
    assert "filesystem.read_file" in visible
    assert "shell.exec" not in visible


def test_llm_catalog_does_not_fallback_to_raw_registry_tools(monkeypatch) -> None:
    """Hidden registry tools must not re-enter when visible fallback catalogs are empty."""
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=_BriefOnlyFakeLLM("artifact.search"))
    context: Dict[str, Any] = {
        "resolved_tools": [],
        "artifact_tool_exposure": {
            "allow_search": False,
            "allow_read": False,
            "has_persisted_artifacts": False,
            "known_artifact_ids": [],
            "evidence_gap_signal": False,
        },
    }
    monkeypatch.setattr(
        "agent.reasoning.enhanced_planner_impl.build_full_tool_catalog",
        lambda _config, *, logger: [],
    )
    with pytest.raises(RuntimeError, match="No tools available for LLM selection"):
        planner._resolve_tool_catalog_for_llm(
            context=context,
            user_message="inspect files",
        )


def test_llm_catalog_excludes_current_turn_tool_unavailable_from_runtime_signal() -> None:
    """Runtime-owned unavailable-tool signal disqualifies tools before selection."""
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=_BriefOnlyFakeLLM("shell.exec"))
    context: Dict[str, Any] = {
        "resolved_tools": [
            "information_gathering.network_discovery.netdiscover",
            "information_gathering.network_discovery.nmap",
            "shell.exec",
        ],
        "selected_categories": ["information_gathering", "shell"],
        "working_memory": {
            "current_turn_phases": [
                {
                    "turn_sequence": 12,
                    "phase_sequence": 0,
                    "source": "tool",
                    "kind": "information_gathering.network_discovery.nmap",
                    "action": "information_gathering.network_discovery.nmap",
                    "status": "failed",
                    "result": "error",
                    "failure_category": "tool_unavailable",
                    "summary": "stale phase-memory record should not drive filtering",
                }
            ]
        },
        "current_turn_unavailable_tools": [
            "information_gathering.network_discovery.netdiscover"
        ],
    }

    visible = planner._resolve_tool_catalog_for_llm(
        context=context,
        user_message="discover Docker hosts",
    )

    assert "information_gathering.network_discovery.netdiscover" not in visible
    assert "information_gathering.network_discovery.nmap" in visible
    assert "shell.exec" not in visible


# ---------------------------------------------------------------------------
# Phase 3 Task 3.3: ActionPlan now carries a ToolBatch and legacy fields
# (selected_tools, tool_parameters) are derived projections of it.
# ---------------------------------------------------------------------------


def _build_planner_with_fake(tool_id: str = _VISIBLE_TEST_TOOL_ID):
    fake_llm = _BriefOnlyFakeLLM(tool_id=tool_id)
    planner = EnhancedActionPlanner(_BriefOnlyDummyConfig(), llm_client=fake_llm)
    return planner, fake_llm


def _basic_context() -> Dict[str, Any]:
    return {
        "current_phase": "enumeration",
        "resolved_tools": [_VISIBLE_TEST_TOOL_ID],
        "history": [],
        "user_message": "run echo ok",
        "intent_brief": _POPULATED_BRIEF,
    }


def _basic_action() -> Action:
    return Action(
        type=ActionType.GATHER_INFO,
        target="localhost",
        parameters={},
        reasoning="",
        expected_outcome="",
    )


def test_planner_emits_tool_batch_field() -> None:
    """ActionPlan.tool_batch is populated when the builder commits a batch."""
    planner, _ = _build_planner_with_fake()
    plan = asyncio.run(planner.build_action_plan(_basic_action(), _basic_context()))

    assert plan.tool_batch is not None
    assert plan.tool_batch.tool_batch_id.startswith("tb_")
    assert len(plan.tool_batch.tool_calls) == 1
    assert plan.tool_batch.tool_calls[0].tool_id == _VISIBLE_TEST_TOOL_ID
    assert plan.tool_batch.tool_calls[0].tool_call_id.startswith("tc_")


def test_planner_legacy_fields_derived_from_batch() -> None:
    """selected_tools / tool_parameters are derived projections of tool_batch."""
    planner, _ = _build_planner_with_fake()
    plan = asyncio.run(planner.build_action_plan(_basic_action(), _basic_context()))

    derived_ids = [call.tool_id for call in plan.tool_batch.tool_calls]
    derived_params = {
        call.tool_id: dict(call.parameters) for call in plan.tool_batch.tool_calls
    }
    assert plan.selected_tools == derived_ids
    assert plan.tool_parameters == derived_params


def test_planner_passes_config_cap_to_batch_commit(monkeypatch) -> None:
    """The planner forwards AgentConfig.max_committed_tools_per_batch as max_calls.

    No module-level cap literal lives in batch_commit; the cap must come from
    the config surface for every commit attempt. We capture
    ``commit_tool_batch`` to assert the kwarg is plumbed through.
    """
    captured: List[Dict[str, Any]] = []

    from agent.reasoning import enhanced_planner_impl as planner_module

    real_commit = planner_module.commit_tool_batch

    def _capture_commit(*args, **kwargs):
        captured.append({"args": args, "kwargs": dict(kwargs)})
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(planner_module, "commit_tool_batch", _capture_commit)

    class _CapConfig(_BriefOnlyDummyConfig):
        max_committed_tools_per_batch = 5

    fake_llm = _BriefOnlyFakeLLM(tool_id=_VISIBLE_TEST_TOOL_ID)
    planner = EnhancedActionPlanner(_CapConfig(), llm_client=fake_llm)
    asyncio.run(planner.build_action_plan(_basic_action(), _basic_context()))

    assert captured, "commit_tool_batch was never invoked"
    last = captured[-1]
    assert last["kwargs"].get("max_calls") == 5
