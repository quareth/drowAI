"""Authoritative prompt-authority boundary lock for intent-interpretation runner control.

This module is the single, enumerated source of truth for Tasks 4.1
and 4.2 of ``docs/plans/intent_interpretation_wiring.md``. Every
per-seam guardrail added in Phase 3 locks one seam in isolation; this
file consolidates the full boundary so a reviewer can open one test
module and see, in one place, which LangGraph-runtime LLM seams may
read the shared ``ConversationContextBundle`` transcript and which
may not. Task 4.2 additionally adds (a) a parametrized
reject-regression summary that asserts every narrowed public builder
raises ``TypeError`` when a caller passes the removed transcript
kwarg, and (b) a drift-lock scan that prevents the
``# transitional; removed in phase 4`` shim annotation from silently
returning to production code.

The runner control boundary splits existing LLM callsites into two disjoint
groups:

Full-history seams (transcript-authoritative; MUST keep transcript
imports, MUST render bundle transcript content into their prompts):

- ``backend.services.langgraph_chat.intent.classifier`` -- turn-start
  interpretation authority.
- ``agent.graph.nodes.finalize`` -- final-answer authority; the
  documented full-history exception. (Phase 6 cutover of the unified
  finalizer collapsed the legacy ``deep_reasoning_finalizer`` shim
  into this single, capability-aware node.)

Narrowed seams (brief-driven; MUST NOT import bundle/transcript
symbols and MUST NOT rebuild turn meaning from transcript):

- ``agent.graph.nodes.select_tool_categories``
- ``core.prompts.builders.tool_planning`` (tool-selection + parameter
  generation prompt builders for the direct executor stack)
- ``agent.graph.nodes.planner`` (deep-reasoning planner)
- ``agent.graph.nodes.tool_articulation``
- ``agent.graph.nodes.post_tool_reasoning.node``

The checks here are deliberately cheap: module-attribute introspection
and a scoped source-level scan. Heavy runtime fixtures already live in
their per-seam test modules; the two behavioural tests at the bottom
of this file re-exercise those seams with minimal setup so the
consolidated contract is self-contained for future reviewers.

If any test in this module fails, the runner control prompt-authority boundary
has shifted. Either the change is intentional (in which case update
``docs/plans/intent_interpretation_wiring.md`` and the associated
ownership checklist) or it is a regression.
"""

from __future__ import annotations

import importlib
import pathlib
import re
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _import(module_path: str) -> ModuleType:
    return importlib.import_module(module_path)


def _assert_module_has(module: ModuleType, names: Sequence[str]) -> None:
    missing = [name for name in names if not hasattr(module, name)]
    assert not missing, (
        f"{module.__name__} is a full-history seam and must keep "
        f"transcript-wiring symbols; missing: {missing}"
    )


def _assert_module_has_none_of(module: ModuleType, names: Sequence[str]) -> None:
    present = [name for name in names if hasattr(module, name)]
    assert not present, (
        f"{module.__name__} is a narrowed seam and must not expose "
        f"transcript-wiring symbols; unexpectedly present: {present}"
    )


# ---------------------------------------------------------------------------
# Positive locks: the two full-history seams.
# ---------------------------------------------------------------------------


def test_intent_classifier_module_imports_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: classifier keeps full-history authority.

    The intent classifier is one of the two documented runner control seams
    permitted to consume the shared bundle transcript (see the
    ``two-full-history-seams-only`` ownership rule). Removing any of
    the transcript-wiring symbols below from the classifier module
    means its runtime path can no longer read the bundle, and the
    full-history seam has silently narrowed.
    """
    classifier_module = _import("backend.services.langgraph_chat.intent.classifier")
    _assert_module_has(
        classifier_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "project_for_intent_classifier",
        ),
    )


def test_deep_reasoning_finalizer_module_imports_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: finalizer keeps full-history authority.

    The unified finalizer (``agent.graph.nodes.finalize``) is the second
    documented full-history seam (see
    ``finalizer-exception-is-documented-and-tested``). On the
    deep-reasoning capability path it renders bundle transcript +
    runtime-state into its user prompt via ``project_for_articulation``
    + ``serialize_projection_to_section_map``; removing either means
    the finalizer is no longer reading full history, and runner control's
    explicit exception has drifted.

    The Phase 6 unified-finalizer cutover replaced the legacy
    ``deep_reasoning_finalizer`` shim with the capability-aware
    ``finalize`` node, so this lock now targets that module.
    """
    finalizer_module = _import("agent.graph.nodes.finalize")
    _assert_module_has(
        finalizer_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "SECTION_RUNTIME_STATE",
            "project_for_articulation",
            "serialize_projection_to_section_map",
        ),
    )


# ---------------------------------------------------------------------------
# Negative locks: narrowed seams must not re-acquire transcript symbols.
# ---------------------------------------------------------------------------


def test_category_selector_module_has_no_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: category selector no longer reads transcript.

    After Phase 3 Task 3.1 the category selector consumes the
    classifier-derived ``intent_brief`` from runtime metadata.
    Re-exposing any of the bundle/transcript symbols below on
    ``select_tool_categories`` means a transcript read has regressed
    onto the category-selector hot path.
    """
    selector_module = _import("agent.graph.nodes.select_tool_categories")
    _assert_module_has_none_of(
        selector_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "project_for_category_selector",
            "serialize_projection_to_section_map",
        ),
    )


def test_dr_planner_module_has_no_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: DR planner no longer reads transcript.

    After Phase 3 Task 3.3 ``agent.graph.nodes.planner`` consumes
    ``intent_brief`` from runtime metadata and MUST NOT
    rebuild turn meaning from the bundle transcript. Any of these
    symbols reappearing on the module surface indicates a regression.
    """
    planner_module = _import("agent.graph.nodes.planner")
    _assert_module_has_none_of(
        planner_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "project_for_planner",
            "serialize_projection_to_section_map",
        ),
    )


def test_tool_planning_builder_has_no_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: tool-selection and parameter-generation
    prompts no longer carry transcript sections.

    After Phase 3 Task 3.2 the direct-executor tool-planning prompt
    builder is brief-driven. Any projection helper, bundle metadata key,
    transcript section constant, or the legacy
    ``conversation_history_text`` keyword symbol reappearing on the
    builder module means a transcript-shaped input has been reintroduced
    into tool selection or parameter generation.
    """
    tool_planning_module = _import("core.prompts.builders.tool_planning")
    _assert_module_has_none_of(
        tool_planning_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "project_for_articulation",
            "project_for_planner",
            "project_for_category_selector",
            "serialize_projection_to_section_map",
            "serialize_projection_to_prompt_sections",
            "conversation_history_text",
        ),
    )


def test_tool_articulation_module_has_no_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: articulation no longer reads transcript.

    After Phase 3 Task 3.4 ``agent.graph.nodes.tool_articulation`` is
    brief-driven. In particular the transitional
    ``_resolve_articulation_bundle_context`` helper was removed along
    with every bundle/transcript import the articulation path used.
    """
    articulation_module = _import("agent.graph.nodes.tool_articulation")
    _assert_module_has_none_of(
        articulation_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "SECTION_RUNTIME_STATE",
            "project_for_articulation",
            "serialize_projection_to_section_map",
            "_resolve_articulation_bundle_context",
        ),
    )


def test_ptr_node_module_has_no_transcript_symbols() -> None:
    """Locks Task 4.1 acceptance: PTR receives supplemental intent context
    without full transcript.

    Phase 3 Task 3.5 wires a classifier-derived supplemental intent
    section into the PTR prompt, but PTR is explicitly NOT a transcript
    consumer (``ptr-is-not-a-transcript-consumer``). Any bundle or
    transcript symbol appearing on ``post_tool_reasoning.node`` would
    mean a transcript read has leaked onto the PTR hot path as a side
    effect of that wiring.
    """
    ptr_module = _import("agent.graph.nodes.post_tool_reasoning.node")
    _assert_module_has_none_of(
        ptr_module,
        (
            "METADATA_CONTEXT_BUNDLE_KEY",
            "SECTION_RECENT_TRANSCRIPT",
            "project_for_articulation",
            "serialize_projection_to_section_map",
        ),
    )


# ---------------------------------------------------------------------------
# Source-level scan (belt-and-suspenders for the boundary).
#
# The module-attribute checks above fail fast when a symbol is imported
# at the top of a narrowed module. The two scans below additionally fail
# when a symbol reappears as a bare reference (``foo = some_module.project_for_articulation``,
# a local import inside a function, etc.) anywhere in production code
# under the runner control boundary, except at the defining / full-history
# callsites we explicitly allowlist.
# ---------------------------------------------------------------------------


# Trees under which runner control wiring lives. Walked by the scans below.
# ``agent/graph/context`` is included so that the symbol-defining
# modules are considered alongside consumer modules — the allowlists
# below explicitly name them, so a reviewer sees every file that
# references a transcript symbol in one place.
_SCAN_ROOTS: Tuple[pathlib.Path, ...] = (
    _REPO_ROOT / "agent" / "graph" / "context",
    _REPO_ROOT / "agent" / "graph" / "nodes",
    _REPO_ROOT / "agent" / "graph" / "subgraphs",
    _REPO_ROOT / "agent" / "reasoning",
    _REPO_ROOT / "backend" / "services" / "langgraph_chat",
    _REPO_ROOT / "core" / "prompts",
)


def _iter_production_sources() -> Iterable[pathlib.Path]:
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            # Skip test files and any file sitting under a ``tests`` dir.
            if "tests" in path.parts:
                continue
            if path.name.startswith("test_"):
                continue
            yield path


def _files_referencing(symbol: str) -> List[pathlib.Path]:
    pattern = re.compile(rf"(?<![\w.]){re.escape(symbol)}(?![\w])")
    offenders: List[pathlib.Path] = []
    for path in _iter_production_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        if pattern.search(source):
            offenders.append(path)
    return offenders


def _rel(paths: Iterable[pathlib.Path]) -> List[str]:
    return sorted(str(p.relative_to(_REPO_ROOT)) for p in paths)


def test_project_for_articulation_is_only_referenced_by_two_seams() -> None:
    """Locks Task 4.1 acceptance: the finalizer is the only runtime consumer
    of ``project_for_articulation``.

    ``project_for_articulation`` is the bundle projection used by the
    finalizer to build its user prompt. The symbol is DEFINED in
    ``agent/graph/context/projections.py`` (so that file legitimately
    references it), and CONSUMED only by the finalizer module. Any
    other production-code file referencing this symbol under the
    runner control boundary indicates the finalizer's full-history projection
    has been resurrected at a second seam.
    """
    expected = {
        "agent/graph/context/projections.py",  # defining module
        # Phase 6 unified finalizer is the one runtime seam; the legacy
        # ``deep_reasoning_finalizer`` shim has been removed.
        "agent/graph/nodes/finalize.py",
    }

    actual = set(_rel(_files_referencing("project_for_articulation")))

    unexpected = actual - expected
    missing = expected - actual
    assert not unexpected and not missing, (
        "project_for_articulation must be referenced only by the "
        "finalizer runtime and its defining projections module. "
        f"unexpected: {sorted(unexpected)} missing: {sorted(missing)}"
    )


def test_section_recent_transcript_is_only_referenced_by_full_history_seams() -> None:
    """Locks Task 4.1 acceptance: the two full-history seams are the only
    production consumers of ``SECTION_RECENT_TRANSCRIPT``.

    ``SECTION_RECENT_TRANSCRIPT`` is DEFINED in
    ``agent/graph/context/serialization.py`` and re-exported by
    ``agent/graph/context/projections.py``. The only runtime CONSUMERS
    are the intent classifier and the deep-reasoning finalizer. Any
    other production-code file referencing this symbol under the
    runner control boundary indicates a narrowed node has reacquired the
    transcript section key.
    """
    expected = {
        # Defining / re-exporting modules (symbol originates here).
        "agent/graph/context/serialization.py",
        "agent/graph/context/projections.py",
        # The two explicit full-history seams. Phase 6 unified the
        # finalizer; the legacy ``deep_reasoning_finalizer`` shim has
        # been removed.
        "backend/services/langgraph_chat/intent/classifier.py",
        "agent/graph/nodes/finalize.py",
    }

    actual = set(_rel(_files_referencing("SECTION_RECENT_TRANSCRIPT")))

    unexpected = actual - expected
    missing = expected - actual
    assert not unexpected and not missing, (
        "SECTION_RECENT_TRANSCRIPT may only be referenced by the "
        "defining serialization/projections modules and the two "
        f"full-history seams. unexpected: {sorted(unexpected)} "
        f"missing: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Behavioral locks: prompt text presence/absence at each full-history seam.
# ---------------------------------------------------------------------------


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


@pytest.mark.asyncio
async def test_classifier_prompt_contains_bundle_transcript_markers() -> None:
    """Locks Task 4.1 acceptance: classifier receives full bundle transcript.

    Drive the classifier with a bundle that carries distinctive
    per-turn marker strings and assert the rendered user prompt
    contains them verbatim. This is the positive counterpart to the
    narrowed-seam negative tests: the classifier IS allowed to see
    transcript text, and failing this test means the runner control
    full-history seam has silently narrowed.
    """
    from types import SimpleNamespace

    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )
    from backend.services.langgraph_chat.contracts import (
        ChatInputs,
        ExecutionMode,
        LangGraphRuntimeConfig,
    )
    from backend.services.langgraph_chat.intent.classifier import (
        IntentClassifier,
    )

    marker_user = "CLASSIFIER_TRANSCRIPT_MARKER_USER_XYZ"
    marker_assistant = "CLASSIFIER_TRANSCRIPT_MARKER_ASSISTANT_ABC"
    history = [
        {"role": "user", "content": marker_user},
        {"role": "assistant", "content": marker_assistant},
        {"role": "user", "content": "follow up please"},
    ]

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    bundle = build_conversation_context_bundle(
        conversation_id="conv-boundary-classifier",
        turn_id="turn-boundary-classifier",
        turn_sequence=0,
        messages=list(history),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle

    config = LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=99,
            user_id=1,
            message="follow up please",
            conversation_id="conv-boundary-classifier",
            history=history,
            api_key="test-key",
            model="stub-classifier",
        ),
        metadata=metadata,
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )

    captured: Dict[str, Optional[str]] = {"prompt": None}

    class _CapturingClient:
        async def chat_with_usage(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            **_: Any,
        ) -> Any:
            captured["prompt"] = user_prompt
            return SimpleNamespace(
                content="{}",
                usage=None,
                structured_output=None,
            )

    classifier = IntentClassifier(
        client_factory=lambda call_settings: _CapturingClient()
    )
    await classifier.enrich_runtime_config(config)

    prompt = captured["prompt"] or ""
    assert marker_user in prompt, (
        "classifier must see bundle transcript content; user-turn "
        "marker missing from rendered user prompt"
    )
    assert marker_assistant in prompt, (
        "classifier must see bundle transcript content; assistant-turn "
        "marker missing from rendered user prompt"
    )


def test_finalizer_prompt_contains_bundle_transcript_markers() -> None:
    """Locks Task 4.1 acceptance: finalizer receives full bundle transcript.

    Drive the finalizer prompt builder with a bundle carrying
    distinctive per-turn markers and assert the rendered user prompt
    contains both markers AND the unified ``<turn ...>`` transcript
    shape produced by the bundle serializer. This re-asserts the
    Task 3.6 F-2 invariant at the consolidated boundary file so
    reviewers see both halves of the split in one place.
    """
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )
    from agent.graph.context.contracts import RuntimeStateSnapshot
    from agent.graph.nodes.finalize import _build_prompts
    from agent.graph.state import InteractiveState
    from core.prompts.constants import CONVERSATION_SECTION_LABEL

    marker_user = "FINALIZER_TRANSCRIPT_MARKER_USER_QQQ"
    marker_assistant = "FINALIZER_TRANSCRIPT_MARKER_ASSISTANT_RRR"

    bundle = build_conversation_context_bundle(
        conversation_id="conv-boundary-finalizer",
        turn_id="turn-boundary-finalizer",
        turn_sequence=4,
        messages=[
            {"role": "user", "content": marker_user},
            {"role": "assistant", "content": marker_assistant},
            {"role": "user", "content": "finalize please"},
        ],
        runtime_state=RuntimeStateSnapshot(
            active_target={"value": "1.2.3.4", "kind": "ip"},
            current_goal=None,
            current_decision=None,
            in_flight_tool=None,
            handles={},
        ),
    )

    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "conversation_id": "conv-boundary-finalizer",
            "message": "finalize please",
            # The full-history bundle seam is the deep-reasoning
            # capability of the unified finalizer.
            "capability": "deep_reasoning",
            "metadata": {METADATA_CONTEXT_BUNDLE_KEY: bundle},
        },
        "trace": {"history": [], "reasoning": [], "observations": []},
    }

    interactive = InteractiveState.from_mapping(state)
    _, user_prompt = _build_prompts(interactive)

    assert marker_user in user_prompt, (
        "finalizer must render bundle transcript; user-turn marker "
        "missing from rendered user prompt"
    )
    assert marker_assistant in user_prompt, (
        "finalizer must render bundle transcript; assistant-turn "
        "marker missing from rendered user prompt"
    )
    assert f"## {CONVERSATION_SECTION_LABEL}" in user_prompt, (
        "finalizer must expose the unified conversation section header"
    )
    assert "<turn" in user_prompt, (
        "finalizer must expose the bundle's <turn ...> transcript "
        "shape — that is how it consumes full history"
    )


# ---------------------------------------------------------------------------
# Task 4.2 migration cleanup guardrails.
#
# The reject-regression parametrization below locks the post-cutover
# signatures of every narrowed prompt builder so a future patch that
# tries to revive a ``history_text`` / ``conversation_history_text`` /
# ``conversation_context`` / ``history_section`` kwarg fails loudly
# with ``TypeError``. The drift-lock scan additionally fails if any
# production source line reintroduces a ``# transitional; removed in
# phase 4`` annotation — that marker is specifically reserved for the
# runner control cutover and must not reappear silently.
# ---------------------------------------------------------------------------


_FORBIDDEN_TRANSCRIPT = "<turn n=1 role=user>leak</turn>"


def _call_build_tool_category_selection_prompt() -> None:
    from core.prompts.constants import build_tool_category_selection_prompt

    build_tool_category_selection_prompt(
        categories_text="network_discovery: test",
        intent_brief={"resolved_user_intent": "x"},
        history_text=_FORBIDDEN_TRANSCRIPT,  # type: ignore[call-arg]
    )


def _call_build_planning_prompt() -> None:
    from core.prompts.constants import build_planning_prompt

    build_planning_prompt(
        targets_str="1.2.3.4",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief={"resolved_user_intent": "x"},
        history_section=_FORBIDDEN_TRANSCRIPT,  # type: ignore[call-arg]
    )


def _call_build_tool_articulation_prompt() -> None:
    from core.prompts.constants import build_tool_articulation_prompt

    build_tool_articulation_prompt(
        selected_tool="nmap",
        tool_params="{}",
        intent_brief={"resolved_user_intent": "x"},
        conversation_context=_FORBIDDEN_TRANSCRIPT,  # type: ignore[call-arg]
    )


def _call_build_select_tools_prompt() -> None:
    from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

    ToolPlanningPromptBuilder().build_select_tools_prompt(
        resolved_tools=[],
        target="1.2.3.4",
        phase="reconnaissance",
        constraints={},
        intent_brief={"resolved_user_intent": "x"},
        conversation_history_text=_FORBIDDEN_TRANSCRIPT,  # type: ignore[call-arg]
    )


def _call_build_tool_parameters_prompt() -> None:
    from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

    ToolPlanningPromptBuilder().build_tool_parameters_prompt(
        selected_tools=["nmap"],
        target="1.2.3.4",
        phase="reconnaissance",
        constraints={},
        intent_brief={"resolved_user_intent": "x"},
        conversation_history_text=_FORBIDDEN_TRANSCRIPT,  # type: ignore[call-arg]
    )


@pytest.mark.parametrize(
    "call_id, invoke",
    [
        (
            "build_tool_category_selection_prompt:history_text",
            _call_build_tool_category_selection_prompt,
        ),
        (
            "build_planning_prompt:history_section",
            _call_build_planning_prompt,
        ),
        (
            "build_tool_articulation_prompt:conversation_context",
            _call_build_tool_articulation_prompt,
        ),
        (
            "ToolPlanningPromptBuilder.build_select_tools_prompt:conversation_history_text",
            _call_build_select_tools_prompt,
        ),
        (
            "ToolPlanningPromptBuilder.build_tool_parameters_prompt:conversation_history_text",
            _call_build_tool_parameters_prompt,
        ),
    ],
)
def test_narrowed_builders_reject_removed_transcript_kwargs(
    call_id: str, invoke: Any
) -> None:
    """Locks Task 4.2 acceptance: every narrowed seam rejects the removed kwarg.

    This is the consolidated reject-regression summary test. Per-seam
    guardrails already live next to each builder (see the Task 4.1
    prior-work list); this parametrization re-asserts the invariant at
    one location so a reviewer can open one file and see that every
    removed transcript kwarg is still a ``TypeError`` on the current
    public signature.
    """
    with pytest.raises(TypeError):
        invoke()
    # And ensure the caller identity is preserved in the failure
    # message for debuggability. (pytest raises the TypeError before
    # we observe anything; the parametrize id suffices.)
    assert call_id  # sanity anchor for the parametrize label


# ---------------------------------------------------------------------------
# Drift-lock: no production source line may reintroduce the
# ``# transitional; removed in phase 4`` annotation.
# ---------------------------------------------------------------------------


_TRANSITIONAL_PHASE4_RE = re.compile(
    r"#\s*transitional;\s*removed\s+in\s+phase\s*4", re.IGNORECASE
)


def test_production_sources_have_no_transitional_phase_4_annotations() -> None:
    """Locks Task 4.2 acceptance: production code carries no ``transitional;
    removed in phase 4`` shim annotations after cutover.

    The phrase ``# transitional; removed in phase 4`` was the standard
    annotation used by in-flight Phase 3 cutovers to flag shim kwargs
    scheduled for Phase 4 removal. All such shims have been removed by
    the end of runner control, so the annotation must not appear in any
    production source. A regression (re-introducing the marker in
    production code) is blocked by this test; a reviewer is redirected
    here and to ``docs/plans/intent_interpretation_wiring.md``.

    The scan uses the same ``_SCAN_ROOTS`` set as the earlier
    source-level scans in this module, and explicitly skips test files
    and ``tests/`` directories — tests may legitimately describe the
    retired shim for documentation.
    """
    offenders: List[str] = []
    for path in _iter_production_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            if _TRANSITIONAL_PHASE4_RE.search(line):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "Production code must not carry ``# transitional; removed in "
        "phase 4`` annotations after the runner_control cutover. Found:\n  - "
        + "\n  - ".join(offenders)
    )
