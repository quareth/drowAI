"""Validate Phase 4 no-LTM hot-path contracts for planner context."""

from __future__ import annotations

import sys
import types
import importlib.util
import inspect
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg
if "core.prompts" not in sys.modules:
    prompts_pkg = types.ModuleType("core.prompts")
    prompts_pkg.__path__ = [str((ROOT_DIR / "core" / "prompts").resolve())]
    sys.modules["core.prompts"] = prompts_pkg

_TOOL_PLANNING_PATH = ROOT_DIR / "core" / "prompts" / "builders" / "tool_planning.py"
_TOOL_PLANNING_SPEC = importlib.util.spec_from_file_location(
    "tool_planning_for_tests",
    _TOOL_PLANNING_PATH,
)
assert _TOOL_PLANNING_SPEC and _TOOL_PLANNING_SPEC.loader
_tool_planning = importlib.util.module_from_spec(_TOOL_PLANNING_SPEC)
_TOOL_PLANNING_SPEC.loader.exec_module(_tool_planning)
ToolPlanningPromptBuilder = _tool_planning.ToolPlanningPromptBuilder

import agent.graph.nodes  # noqa: F401  # Prime node package to avoid import-cycle test collection.
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import _build_planner_context
from agent.tool_runtime.coordinator import ToolExecutionRequest


def _bundle_metadata(extra: dict | None = None) -> dict:
    """Build metadata containing the required hot-path bundle for planner tests."""
    metadata: dict = dict(extra or {})
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id="conv-test",
        turn_id="turn-test",
        turn_sequence=0,
        messages=[],
    )
    return metadata


def _build_select_prompt() -> str:
    """Build a select_tools prompt under the post-cutover contract.

    Phase 3 Task 3.2 removed the transitional
    ``conversation_history_text`` kwarg from every public tool-planning
    builder method. The planner prompt is now driven exclusively by the
    classifier-derived ``intent_brief``; LTM/threaded transcript blocks
    are out of this seam entirely.
    """
    builder = ToolPlanningPromptBuilder()
    return builder.build_select_tools_prompt(
        resolved_tools=["shell.exec"],
        catalog=[{"id": "shell.exec", "name": "shell.exec", "description": "Run shell commands"}],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
    )


def test_planner_select_prompt_drops_long_term_memory_after_brief_narrowing() -> None:
    """Phase 3 Task 3.2: the select_tools prompt never renders LTM text.

    Before the Phase 2 narrowing, the planner folded the LTM summary
    into ``conversation_history_text`` and the ``select_tools``
    template rendered that combined block. After the Phase 3 Task 3.2
    cutover, the transitional kwarg is removed entirely and the prompt
    is driven solely by ``intent_brief``; the LTM summary cannot reach
    the prompt body.
    """
    prompt = _build_select_prompt()
    assert "Long-Term Memory:" not in prompt
    assert "Remember prior scan found SSH and HTTPS." not in prompt


def test_planner_select_prompt_renders_even_without_memory() -> None:
    prompt = _build_select_prompt()
    assert "Long-Term Memory:" not in prompt
    # Structural anchor that survives the narrowing: the brief block header.
    assert "Turn Execution Brief" in prompt


def test_planner_context_drops_long_term_memory_summary_from_metadata() -> None:
    """Phase 4 cutover: planner context must not include LTM key at all.

    ``memory_retrieval_node`` still writes ``metadata["long_term_memory_summary"]``,
    but planner hot-path context must not thread that field to prompt builders.
    """
    state = InteractiveState(
        facts=FactsState(
            task_id=11,
            message="continue",
            capability="simple_tool_execution",
            metadata=_bundle_metadata(
                {"long_term_memory_summary": "Remember prior scan found SSH on 10.0.0.5"}
            ),
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=11,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "long_term_memory_summary" not in planner_context


def test_planner_context_omits_long_term_memory_summary_when_metadata_missing() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=12,
            message="continue",
            capability="simple_tool_execution",
            metadata=_bundle_metadata(),
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=12,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "long_term_memory_summary" not in planner_context


def test_tool_planning_templates_and_builder_signatures_have_no_ltm_placeholder_or_kwarg() -> None:
    versions_root = ROOT_DIR / "core" / "prompts" / "versions"
    builders_root = ROOT_DIR / "core" / "prompts" / "builders"
    placeholder = "{long_term_memory_summary}"

    templates_with_placeholder = [
        str(path.relative_to(ROOT_DIR))
        for path in versions_root.rglob("*.txt")
        if placeholder in path.read_text(encoding="utf-8")
    ]
    assert not templates_with_placeholder, (
        "Prompt templates must not contain {long_term_memory_summary}. "
        f"Offenders: {templates_with_placeholder}"
    )

    builder_sources_with_kwarg = [
        str(path.relative_to(ROOT_DIR))
        for path in builders_root.rglob("*.py")
        if "long_term_memory_summary" in path.read_text(encoding="utf-8")
    ]
    assert not builder_sources_with_kwarg, (
        "Prompt builders must not accept or thread long_term_memory_summary. "
        f"Offenders: {builder_sources_with_kwarg}"
    )

    builder = ToolPlanningPromptBuilder()
    select_sig = inspect.signature(builder.build_select_tools_prompt)
    params_sig = inspect.signature(builder.build_tool_parameters_prompt)
    assert "long_term_memory_summary" not in select_sig.parameters
    assert "long_term_memory_summary" not in params_sig.parameters
