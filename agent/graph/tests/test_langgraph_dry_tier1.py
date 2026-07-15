"""LangGraph DRY Tier 1 baseline guardrail tests.

Purpose
-------
Lock down Tier 1 invariants from
``docs/refactor/langgraph-dry-tier-1-implementation-guide.md`` *before*
production code is edited. This file accumulates tests across Phase 0
(Tasks 0.1, 0.2, 0.3) and is consumed by every later phase's verification
step.

Task 0.1 (this commit) covers the metrics regression. Active agent graph
telemetry must record through ``backend.services.metrics.utils.safe_inc``
and must not invoke the nonexistent ``Metrics.increment`` API.

The tests intentionally do not require network, Docker, or LLM access.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.graph.builders.deep_reasoning_builder import (
    _route_decision,
)
from agent.graph.nodes import reflect as reflect_module
from agent.graph.state import (
    FactsState,
    InteractiveState,
    TraceState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_state() -> InteractiveState:
    """Minimal interactive state suitable for routing-level tests."""

    return InteractiveState(
        facts=FactsState(
            task_id=1,
            message="tier1 metrics regression",
            conversation_id="conv-tier1",
            metadata={},
            decision_history=[],
        ),
        trace=TraceState(),
    )


# ---------------------------------------------------------------------------
# Task 0.1: Metrics regression tests
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GRAPH_ROOT = REPO_ROOT / "agent" / "graph"
ARCHIVE_DIR = AGENT_GRAPH_ROOT / "utils" / "_archive"

# Canonical telemetry callable that Phase 1 must converge on at every active
# call site under ``agent/graph``.
CANONICAL_METRICS_API = "backend.services.metrics.utils.safe_inc"

# Pre-Phase-1 the active builders/nodes call ``metrics.increment(...)`` via
# private ``_safe_inc`` wrappers. After Phase 1 they must call the canonical
# ``safe_inc`` directly. This regex matches the broken pattern; a hit means
# Tier 1 metrics canonicalization (Phase 1) has not landed yet.
BROKEN_METRICS_INCREMENT_RE = re.compile(r"\bmetrics\.increment\s*\(")


def _iter_active_python_files() -> list[Path]:
    """Return active ``.py`` files under ``agent/graph`` (no archive, no tests).

    Mirrors the Tier 3 ownership rule for static scans: exclude
    ``_archive``, ``tests``, and ``__pycache__``. Test modules
    (including this one) reference the broken pattern as a regex / docs
    literal, so they must stay outside the active production scan.
    """

    files: list[Path] = []
    for path in AGENT_GRAPH_ROOT.rglob("*.py"):
        if ARCHIVE_DIR in path.parents:
            continue
        if "tests" in path.parts:
            continue
        if "__pycache__" in path.parts:
            continue
        files.append(path)
    return files


class TestStaticMetricsGuard:
    """Static guards that lock down the metrics canonicalization invariant."""

    def test_no_metrics_increment_in_active_agent_graph(self) -> None:
        """No active file under ``agent/graph`` should call ``metrics.increment(...)``.

        Currently FAILS because Phase 1 has not landed; this is the Tier 1
        baseline-guardrail signal that the broken metrics path still exists.
        After Phase 1 it must pass.
        """

        offenders: list[str] = []
        for path in _iter_active_python_files():
            text = path.read_text(encoding="utf-8")
            if BROKEN_METRICS_INCREMENT_RE.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))

        assert not offenders, (
            "metrics.increment(...) is not a real Metrics method (only "
            "metrics.inc(...) exists). Active call sites must use "
            f"{CANONICAL_METRICS_API} instead. Offenders:\n  - "
            + "\n  - ".join(sorted(offenders))
        )

    def test_static_guard_excludes_archive(self) -> None:
        """The static guard ignores ``agent/graph/utils/_archive`` by design."""

        for path in _iter_active_python_files():
            assert ARCHIVE_DIR not in path.parents, (
                f"Active scan must skip archive: {path}"
            )


class TestPostToolDecisionMetricsCallSite:
    """Confirm ``_route_from_post_tool_decision`` records telemetry through ``safe_inc``.

    The patch target follows the Tier 1 plan: once Phase 1 lands and
    ``deep_reasoning_builder`` switches to ``from
    backend.services.metrics.utils import safe_inc``, the symbol
    ``agent.graph.builders.deep_reasoning_builder.safe_inc`` is the call-site
    binding to patch. Today that binding does not exist, so this test fails
    at collection — that failure is the regression signal.
    """

    def test_finalize_route_records_metric_through_safe_inc(
        self, base_state: InteractiveState
    ) -> None:
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.metadata["router_outcome"] = {"action": "finalize"}

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_decision(base_state)

        assert result == "finalize"
        # DR dispatch records a router-action metric for finalize.
        assert mock_safe_inc.called, (
            "Expected post-tool finalize routing to call safe_inc; "
            "either Phase 1 has not landed or the call site no longer "
            "imports safe_inc."
        )
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "deep_reasoning_router_action_finalize" in recorded


class TestReflectMetricsCallSite:
    """Confirm ``reflect._identify_problem`` records telemetry through ``safe_inc``.

    ``reflect.py`` currently imports ``metrics`` lazily inside a try/except
    and calls ``metrics.increment(...)`` directly. After Phase 1 it must
    call ``safe_inc(...)`` imported at the module level — at that point this
    test passes by patching ``agent.graph.nodes.reflect.safe_inc``.
    """

    def test_no_progress_path_records_metric_through_safe_inc(self) -> None:
        # Reach the ``reflect_triggered_no_progress`` branch deterministically
        # via the docstring conditions: iterations > 5 and no executed tools.
        interactive = InteractiveState(
            facts=FactsState(
                task_id=2,
                message="reflect tier1 metrics regression",
                conversation_id="conv-tier1-reflect",
                metadata={},
                decision_history=[],
                iterations=6,
            ),
            trace=TraceState(executed_tools=[]),
        )

        with patch("agent.graph.nodes.reflect.safe_inc") as mock_safe_inc:
            problem = reflect_module._identify_problem(interactive)

        assert "no tool execution" in problem.lower()
        assert mock_safe_inc.called, (
            "Expected reflect's no-progress branch to call safe_inc; "
            "either Phase 1 has not landed or the call site no longer "
            "imports safe_inc."
        )
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "reflect_triggered_no_progress" in recorded


# ---------------------------------------------------------------------------
# Task 0.2: Action parsing tests for the future ``extract_action_label`` helper
# ---------------------------------------------------------------------------
#
# The helper lands in Task 2.1 in
# ``agent/graph/nodes/decision_router/helpers.py``. Imports below are
# deferred to test bodies so this file still *collects* on commits between
# Phase 0 and Phase 2; the tests themselves fail with a clear ``ImportError``
# until Task 2.1 lands, which is the intended regression signal.


class TestExtractActionLabelHelper:
    """Lock down the contract of ``extract_action_label(decision_entry)``.

    Decision-history entries follow the shape ``"action: reasoning"``. The
    helper must return only the action label and tolerate bare entries,
    whitespace, and reasoning text that itself contains additional colons
    (use ``split(":", 1)``).
    """

    def test_extract_action_label_handles_reasoning_suffix(self) -> None:
        from agent.graph.nodes.decision_router.helpers import extract_action_label

        assert extract_action_label("call_tool: run nmap") == "call_tool"

    def test_extract_action_label_handles_bare_action(self) -> None:
        from agent.graph.nodes.decision_router.helpers import extract_action_label

        assert extract_action_label("finalize") == "finalize"

    def test_extract_action_label_strips_surrounding_whitespace(self) -> None:
        from agent.graph.nodes.decision_router.helpers import extract_action_label

        assert extract_action_label("  think_more  ") == "think_more"
        assert extract_action_label("  reflect: stuck loop  ") == "reflect"

    def test_extract_action_label_returns_empty_for_empty_or_whitespace(
        self,
    ) -> None:
        from agent.graph.nodes.decision_router.helpers import extract_action_label

        assert extract_action_label("") == ""
        assert extract_action_label("   ") == ""

    def test_extract_action_label_preserves_reasoning_with_colons(self) -> None:
        """Reasoning may contain additional colons; only the first split counts."""

        from agent.graph.nodes.decision_router.helpers import extract_action_label

        assert (
            extract_action_label("call_tool: run nmap -p 1:65535 against host")
            == "call_tool"
        )


# ---------------------------------------------------------------------------
# Task 0.3: Registry double-compile regression test
# ---------------------------------------------------------------------------


class _FakeCompiledGraph:
    """Stand-in for a LangGraph compiled object.

    Records every ``.compile(...)`` invocation. The Tier 1 fix path must
    feed the registry an *uncompiled* ``StateGraph`` and call ``.compile``
    exactly once inside the registry factory. Today the simple-tool
    getter calls ``build_simple_tool_graph()`` (which already compiles)
    and then re-invokes ``.compile`` on the result, producing a second
    compile against an already-compiled graph.
    """

    def __init__(self, label: str = "compiled") -> None:
        self.label = label
        self.compile_calls: list[dict] = []

    def compile(self, *args, **kwargs):  # pragma: no cover - exercised by tests
        # Recursively returning self lets the registry factory store and
        # return *some* object even when the builder is already compiled.
        self.compile_calls.append({"args": args, "kwargs": kwargs})
        return self


class _FakeStateGraph:
    """Stand-in for an uncompiled ``StateGraph``.

    Only the ``.compile(...)`` method is required by the registry path.
    """

    def __init__(self) -> None:
        self.compiled: _FakeCompiledGraph | None = None

    def compile(self, *args, **kwargs):
        self.compiled = _FakeCompiledGraph(label="from-state-graph")
        self.compiled.compile_calls.append({"args": args, "kwargs": kwargs})
        return self.compiled


class TestRegistryDoubleCompile:
    """Regression coverage for the simple-tool registry getter (Task 4.2)."""

    def test_simple_tool_getter_does_not_double_compile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get_compiled_simple_tool_graph`` must compile exactly once.

        Today the getter calls ``build_simple_tool_graph()`` (default args
        return an *already compiled* graph) and then calls ``.compile(...)``
        again inside the registry factory — a double compile. After Task
        4.2 the getter must build an *uncompiled* graph (e.g. via
        ``build_simple_tool_graph(build_only=True)``) and let the registry
        helper own the single compile call.
        """

        from agent.graph.builders import simple_tool_builder
        from agent.graph.infrastructure.graph_registry import GraphRegistry

        already_compiled = _FakeCompiledGraph(label="already-compiled")
        uncompiled = _FakeStateGraph()

        # Capture which build mode the getter requested.
        build_calls: list[dict] = []

        def fake_build_simple_tool_graph(*, checkpointer=None, build_only=False):
            build_calls.append(
                {"checkpointer": checkpointer, "build_only": build_only}
            )
            return uncompiled if build_only else already_compiled

        monkeypatch.setattr(
            simple_tool_builder,
            "build_simple_tool_graph",
            fake_build_simple_tool_graph,
        )
        monkeypatch.setattr(
            simple_tool_builder,
            "get_default_checkpointer",
            lambda: object(),
        )

        registry = GraphRegistry()

        compiled = simple_tool_builder.get_compiled_simple_tool_graph(
            registry=registry
        )

        # The returned compiled object must not have been ``.compile``-d a
        # second time. Pre-Phase-4 the getter passes ``already_compiled``
        # (returned by the default builder call) into a registry factory
        # that calls ``.compile(...)`` on it — that is the bug we lock
        # down here.
        assert isinstance(compiled, _FakeCompiledGraph)
        assert already_compiled.compile_calls == [], (
            "get_compiled_simple_tool_graph must not call .compile() on a "
            "graph the builder already compiled. Use build_only=True so the "
            "registry compiles the StateGraph exactly once."
        )

    def test_deep_reasoning_getter_compiles_state_graph_through_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DR getter must still compile a fresh ``StateGraph`` via the registry."""

        from agent.graph.builders import deep_reasoning_builder
        from agent.graph.infrastructure.graph_registry import GraphRegistry

        uncompiled = _FakeStateGraph()

        def fake_build_deep_reasoning_graph(*, checkpointer=None):
            return uncompiled

        monkeypatch.setattr(
            deep_reasoning_builder,
            "build_deep_reasoning_graph",
            fake_build_deep_reasoning_graph,
        )
        monkeypatch.setattr(
            deep_reasoning_builder,
            "get_default_checkpointer",
            lambda: object(),
        )

        registry = GraphRegistry()

        compiled = deep_reasoning_builder.get_compiled_deep_reasoning_graph(
            registry=registry
        )

        assert isinstance(compiled, _FakeCompiledGraph)
        assert uncompiled.compiled is compiled, (
            "Deep-reasoning registry path must compile the uncompiled "
            "StateGraph returned by build_deep_reasoning_graph()."
        )
        # Exactly one compile against the StateGraph.
        assert len(compiled.compile_calls) == 1

    def test_repeated_registry_get_returns_same_compiled_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated calls through the registry must return the cached object."""

        from agent.graph.builders import deep_reasoning_builder
        from agent.graph.infrastructure.graph_registry import GraphRegistry

        # Use a counter to confirm the factory runs exactly once.
        invocations: list[_FakeStateGraph] = []

        def fake_build_deep_reasoning_graph(*, checkpointer=None):
            graph = _FakeStateGraph()
            invocations.append(graph)
            return graph

        monkeypatch.setattr(
            deep_reasoning_builder,
            "build_deep_reasoning_graph",
            fake_build_deep_reasoning_graph,
        )
        monkeypatch.setattr(
            deep_reasoning_builder,
            "get_default_checkpointer",
            lambda: object(),
        )

        registry = GraphRegistry()

        first = deep_reasoning_builder.get_compiled_deep_reasoning_graph(
            registry=registry
        )
        second = deep_reasoning_builder.get_compiled_deep_reasoning_graph(
            registry=registry
        )

        assert first is second, "Registry must cache the compiled graph."
        assert len(invocations) == 1, (
            "Registry factory must build the StateGraph exactly once across "
            "repeated registry get(...) calls."
        )
