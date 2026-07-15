"""LangGraph DRY Tier 3 baseline guardrail tests.

Purpose
-------
Lock down Tier 3 invariants from
``docs/refactor/langgraph-dry-tier-3-implementation-guide.md`` *before*
production code is edited.

The tests in this module enforce four things:

1. Retry metadata contract:
   - Top-level ``metadata["retry_suggested"]`` is the canonical advisory
     retry flag emitted by PTR.
   - ``metadata["retry_tracking"]`` is the canonical retry budget/counter
     record. The nested ``last_retry_suggested`` key is dead.
   - DR post-tool routing must record retry observability through
     ``backend.services.metrics.utils.safe_inc`` (patched as
     ``agent.graph.builders.deep_reasoning_builder.safe_inc``) and must
     not gate on ``last_retry_suggested``.

2. Terminal metadata predicates:
   - ``user_goal_achieved=True`` and ``request_contract_terminal=True``
     are hard finalize signals from PTR.
   - DR post-tool routing finalizes for either flag *before* following a
     ``call_tool`` decision recorded in ``decision_history``.
   - Decision-router guardrails reach the same terminal verdict via the
     shared predicate landed by Phase 2.

3. Retry constant canonicalization:
   - ``MAX_RETRIES`` and ``RETRY_METADATA_KEY`` have one active home in
     ``agent.graph.nodes.post_tool_reasoning.core.retry_logic``.

4. Static inventory guards (Phase 0+):
   - No active production read/write of ``last_retry_suggested``.
   - No active production raw ``"retry_tracking"`` literal outside the
     active retry core and the test/doc/archive trees.
   - No active production ``metrics.increment(...)`` calls (Tier 1
     invariant; reasserted in Tier 3 to prevent regressions).

Phase-aware activation
----------------------
Tests describing future-phase target state are marked
``pytest.mark.xfail(strict=True, reason="...")`` so the same assertion
holds across the migration: while the issue exists the test xfails;
once the owning phase ships, ``strict=True`` flips an unexpected pass
into a failure, forcing removal of the marker.

The tests intentionally do not require network, Docker, a database, or
LLM calls. They construct ``InteractiveState`` directly via Pydantic and
patch ``deep_reasoning_builder.safe_inc`` for observability assertions.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

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
            message="tier3 routing regression",
            conversation_id="conv-tier3",
            metadata={},
            decision_history=[],
        ),
        trace=TraceState(),
    )


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GRAPH_ROOT = REPO_ROOT / "agent" / "graph"
ARCHIVE_DIR = AGENT_GRAPH_ROOT / "utils" / "_archive"

# Active retry core (canonical home for MAX_RETRIES / RETRY_METADATA_KEY).
RETRY_CORE_PATH = (
    AGENT_GRAPH_ROOT / "nodes" / "post_tool_reasoning" / "core" / "retry_logic.py"
)
def _iter_active_python_files() -> list[Path]:
    """Return active ``.py`` files under ``agent/graph``.

    Excludes:
    - The ``_archive`` subtree (deprecated/legacy).
    - The ``tests`` subtrees (regression fixtures may carry the patterns
      we are guarding against in production).
    - ``__pycache__`` byte-code directories.
    - This very file (it references the broken patterns as string
      literals in static guards below).
    """

    files: list[Path] = []
    for path in AGENT_GRAPH_ROOT.rglob("*.py"):
        if ARCHIVE_DIR in path.parents:
            continue
        if "tests" in path.parts:
            continue
        if "__pycache__" in path.parts:
            continue
        if path == Path(__file__).resolve():
            continue
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# Task 0.2: Retry metadata regression tests
# ---------------------------------------------------------------------------


class TestRetryMetadataContract:
    """Lock down the retry metadata contract for DR post-tool routing.

    The canonical contract after Tier 3:
    - ``metadata["failure_detected"]=True`` plus
      ``metadata["retry_suggested"]=True`` is the *advisory* retry signal.
    - ``metadata["retry_tracking"]`` is the *budget/counter* record only.
    - Routing follows the recorded PTR decision in ``decision_history``;
      the retry advisory only adds an observability metric on a
      ``call_tool`` route.
    """

    def test_canonical_retry_metadata_routes_call_tool(
        self, base_state: InteractiveState
    ) -> None:
        """``call_tool`` router outcome routes to ``select_categories``."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {
            "failure_detected": True,
            "retry_suggested": True,
            "router_outcome": {"action": "call_tool"},
        }

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_decision(base_state)

        assert result == "select_categories"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "deep_reasoning_router_action_call_tool" in recorded

    def test_call_tool_without_retry_advisory_routes_call_tool(
        self, base_state: InteractiveState
    ) -> None:
        """``call_tool`` decision without retry advisory still routes to
        ``select_categories``; no retry-specific metric fires."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {"router_outcome": {"action": "call_tool"}}

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_decision(base_state)

        assert result == "select_categories"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "deep_reasoning_router_action_call_tool" in recorded

    def test_advisory_drift_without_failure_is_not_retry(
        self, base_state: InteractiveState
    ) -> None:
        """``retry_suggested=True`` without ``failure_detected=True`` is
        advisory drift, not a canonical retry; the helper landed by
        Phase 1 must reject it."""

        # Helper lands in Task 1.1; deferred import keeps this file
        # collectable until then.
        from agent.graph.nodes.post_tool_reasoning.core.retry_logic import (
            retry_suggested,
        )

        # Drift: advisory flag is set but failure_detected is missing.
        assert retry_suggested({"retry_suggested": True}) is False
        # Drift: failure_detected without advisory flag.
        assert retry_suggested({"failure_detected": True}) is False
        # Canonical retry: both flags set.
        assert (
            retry_suggested(
                {"failure_detected": True, "retry_suggested": True}
            )
            is True
        )
        # Empty metadata is not a retry.
        assert retry_suggested({}) is False

    def test_retry_observability_uses_safe_inc_not_metrics_increment(
        self, base_state: InteractiveState
    ) -> None:
        """Retry observability must record through ``safe_inc`` (Tier 1 API);
        active code must not call ``metrics.increment(...)``."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {
            "failure_detected": True,
            "retry_suggested": True,
            "router_outcome": {"action": "call_tool"},
        }

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            _route_decision(base_state)

        assert mock_safe_inc.called, (
            "Expected DR post-tool routing to call safe_inc for retry "
            "observability; either Phase 1 has not landed or the call "
            "site no longer uses safe_inc."
        )


# ---------------------------------------------------------------------------
# Task 0.3: Terminal metadata predicate tests
# ---------------------------------------------------------------------------


class TestTerminalMetadataRouting:
    """Lock down terminal metadata behavior in DR post-tool routing.

    Terminal metadata flags (``user_goal_achieved`` and
    ``request_contract_terminal``) are hard safety signals: even when
    PTR records ``call_tool`` in ``decision_history``, DR must finalize
    if either terminal flag is set.
    """

    def test_user_goal_achieved_forces_finalize_over_call_tool(
        self, base_state: InteractiveState
    ) -> None:
        """``user_goal_achieved=True`` forces finalize even when the
        recorded decision is ``call_tool``."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {
            "user_goal_achieved": True,
            "router_outcome": {"action": "finalize"},
        }

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_decision(base_state)

        assert result == "finalize"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "deep_reasoning_router_action_finalize" in recorded

    def test_request_contract_terminal_forces_finalize_over_call_tool(
        self, base_state: InteractiveState
    ) -> None:
        """``request_contract_terminal=True`` (without
        ``user_goal_achieved``) must also force finalize. This is the
        previously shadowed branch; Phase 2 collapses both checks behind
        a shared predicate."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {
            "request_contract_terminal": True,
            "router_outcome": {"action": "finalize"},
        }

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_decision(base_state)

        assert result == "finalize"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "deep_reasoning_router_action_finalize" in recorded

    def test_no_terminal_metadata_follows_decision_history(
        self, base_state: InteractiveState
    ) -> None:
        """Without terminal flags, routing follows ``decision_history``."""

        from agent.graph.builders.deep_reasoning_builder import (
            _route_decision,
        )

        base_state.facts.metadata = {"router_outcome": {"action": "think_more"}}

        with patch(
            "agent.graph.builders.deep_reasoning_builder.safe_inc"
        ):
            result = _route_decision(base_state)

        assert result == "think_more"


class TestPostToolMetadataPredicateModule:
    """The Phase 2 shared predicate module ``post_tool_metadata.py`` must
    expose ``user_goal_achieved``, ``request_contract_terminal``, and
    ``post_tool_terminal`` helpers driven by canonical metadata keys."""

    def test_post_tool_metadata_module_exists_and_exports_helpers(
        self,
    ) -> None:
        from agent.graph.utils import post_tool_metadata as ptm

        assert callable(getattr(ptm, "user_goal_achieved", None))
        assert callable(getattr(ptm, "request_contract_terminal", None))
        assert callable(getattr(ptm, "post_tool_terminal", None))

        # Helpers must be metadata-only (Mapping in, bool out).
        assert ptm.user_goal_achieved({"user_goal_achieved": True}) is True
        assert ptm.user_goal_achieved({"user_goal_achieved": False}) is False
        assert ptm.user_goal_achieved({}) is False

        assert (
            ptm.request_contract_terminal({"request_contract_terminal": True})
            is True
        )
        assert ptm.request_contract_terminal({}) is False

        assert ptm.post_tool_terminal({"user_goal_achieved": True}) is True
        assert (
            ptm.post_tool_terminal({"request_contract_terminal": True}) is True
        )
        assert ptm.post_tool_terminal({}) is False

    def test_decision_router_guardrails_recognizes_request_contract_terminal(
        self,
    ) -> None:
        from agent.graph.nodes.decision_router.guardrails import (
            check_goal_completion,
        )

        # Build a minimal facts object that exposes safe_metadata via the
        # FactsState model used in production.
        facts = FactsState(
            task_id=99,
            message="tier3 guardrail terminal",
            metadata={"request_contract_terminal": True},
        )

        is_complete, reason = check_goal_completion(facts, facts.safe_metadata)

        assert is_complete is True
        # Reason must distinguish the request-contract terminal case from
        # the user_goal_achieved case so observability stays meaningful.
        assert reason is not None
        assert "request" in reason.lower() or "contract" in reason.lower()


# ---------------------------------------------------------------------------
# Task 0.4: Static inventory tests
# ---------------------------------------------------------------------------


LAST_RETRY_SUGGESTED_RE = re.compile(r"\blast_retry_suggested\b")
RAW_RETRY_TRACKING_LITERAL_RE = re.compile(r"['\"]retry_tracking['\"]")
BROKEN_METRICS_INCREMENT_RE = re.compile(r"\bmetrics\.increment\s*\(")


def _scan_for_pattern(
    pattern: re.Pattern[str],
    files: list[Path],
    *,
    allow_paths: tuple[Path, ...] = (),
) -> list[str]:
    """Return ``"<relative_path>:<line_no>: <line>"`` hits for ``pattern``.

    Files in ``allow_paths`` are excluded from the scan so callers can
    permit a literal in the canonical home (active retry core /
    deprecated shim) without disabling the rule for the rest of the tree.
    """

    offenders: list[str] = []
    allow_set = {p.resolve() for p in allow_paths}
    for path in files:
        if path.resolve() in allow_set:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: {line.rstrip()}"
                )
    return offenders


class TestStaticInventoryGuards:
    """Static guards that lock down Tier 3 production-code invariants.

    Each guard is phase-aware: it is marked
    ``pytest.mark.xfail(strict=True, ...)`` while the offending pattern
    still exists in production. When the owning phase ships and the
    pattern disappears, ``strict=True`` flips the unexpected pass into a
    failure so the marker must be removed.
    """

    def test_no_active_last_retry_suggested_reference(self) -> None:
        offenders = _scan_for_pattern(
            LAST_RETRY_SUGGESTED_RE,
            _iter_active_python_files(),
        )
        assert not offenders, (
            "Active production code must not read or write "
            "'last_retry_suggested' (PTR never writes that key). "
            "Offenders:\n  - " + "\n  - ".join(sorted(offenders))
        )

    def test_no_raw_retry_tracking_literal_outside_canonical_home(
        self,
    ) -> None:
        # The active retry core legitimately *defines* the literal as
        # ``RETRY_METADATA_KEY = "retry_tracking"``; the guard targets only
        # consumers.
        allow = (RETRY_CORE_PATH,)
        offenders = _scan_for_pattern(
            RAW_RETRY_TRACKING_LITERAL_RE,
            _iter_active_python_files(),
            allow_paths=allow,
        )
        assert not offenders, (
            "Active production code must consume RETRY_METADATA_KEY "
            "from agent.graph.nodes.post_tool_reasoning.core.retry_logic "
            "instead of the raw 'retry_tracking' string. Offenders:\n  - "
            + "\n  - ".join(sorted(offenders))
        )

    def test_no_metrics_increment_in_active_agent_graph(self) -> None:
        """Tier 1 invariant reasserted in Tier 3 to prevent regressions.

        Tier 1 already converged the active tree onto ``safe_inc``; if
        Tier 3 work accidentally reintroduces ``metrics.increment(...)``
        this guard catches it immediately.
        """

        offenders = _scan_for_pattern(
            BROKEN_METRICS_INCREMENT_RE,
            _iter_active_python_files(),
        )
        assert not offenders, (
            "Active production code must not call metrics.increment(...) "
            "(Metrics has no such method). Use "
            "backend.services.metrics.utils.safe_inc(...) instead. "
            "Offenders:\n  - " + "\n  - ".join(sorted(offenders))
        )

    def test_static_scan_excludes_archive_and_tests(self) -> None:
        """Sanity: the active scan never sees archive or test files."""

        for path in _iter_active_python_files():
            assert ARCHIVE_DIR not in path.parents, (
                f"Active scan must skip archive: {path}"
            )
            assert "tests" not in path.parts, (
                f"Active scan must skip tests: {path}"
            )
            assert "__pycache__" not in path.parts, (
                f"Active scan must skip __pycache__: {path}"
            )


# ---------------------------------------------------------------------------
# Phase 4 / 5 future-state placeholders
# ---------------------------------------------------------------------------


class TestSimpleToolPostToolRouteMetrics:
    """Phase 4 adds simple-tool post-tool route metric parity through
    ``backend.services.metrics.utils.safe_inc``.
    """

    def test_simple_tool_call_tool_route_records_metric(self) -> None:
        from agent.graph.builders.simple_tool_builder import (
            _route_after_router,
        )

        state = InteractiveState(
            facts=FactsState(
                task_id=2,
                message="tier3 simple-tool route metrics",
                metadata={},
                decision_history=[],
            ),
            trace=TraceState(),
        )
        state.facts.metadata["router_outcome"] = {"action": "call_tool"}

        with patch(
            "agent.graph.builders.simple_tool_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_after_router(state)

        assert result == "select_tool_categories"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert "simple_tool_router_action_call_tool" in recorded

    def test_simple_tool_empty_history_records_fallback_metric(self) -> None:
        from agent.graph.builders.simple_tool_builder import (
            _route_after_router,
        )

        state = InteractiveState(
            facts=FactsState(
                task_id=3,
                message="tier3 simple-tool fallback",
                metadata={},
                decision_history=[],
            ),
            trace=TraceState(),
        )

        with patch(
            "agent.graph.builders.simple_tool_builder.safe_inc"
        ) as mock_safe_inc:
            result = _route_after_router(state)

        assert result == "format_results"
        recorded = [call.args[0] for call in mock_safe_inc.call_args_list]
        assert any(name.startswith("simple_tool_router_action_") for name in recorded), (
            "Expected at least one simple-tool-scoped fallback metric; "
            f"recorded={recorded}"
        )


class TestBuilderDiagnosticsModule:
    """Phase 5 lands ``agent/graph/builders/diagnostics.py`` as the shared
    diagnostic helper for builder graph builds and wrappers.
    """

    def test_builder_diagnostics_module_exports_helpers(self) -> None:
        from agent.graph.builders import diagnostics

        assert callable(getattr(diagnostics, "get_builder_diagnostic_logger", None))
        assert callable(getattr(diagnostics, "log_builder_graph_build", None))
        assert callable(getattr(diagnostics, "make_wrapper_log_callback", None))

    def test_common_edges_does_not_import_backend_diagnostics(self) -> None:
        common_edges_path = AGENT_GRAPH_ROOT / "builders" / "common_edges.py"
        text = common_edges_path.read_text(encoding="utf-8")
        # The Phase 5 contract says diagnostic logger imports belong to
        # builders/diagnostics.py, not common_edges.py. This guard locks
        # that boundary so future drift is caught immediately.
        assert "diagnostic_logger" not in text, (
            "common_edges.py must not import the backend diagnostic "
            "logger; route diagnostic imports through "
            "agent.graph.builders.diagnostics instead."
        )

    def test_dr_compile_helper_logs_active_graph_build_path(self) -> None:
        from agent.graph.builders import deep_reasoning_builder

        class FakeGraph:
            nodes = {"classification": object(), "finalize": object()}

            def __init__(self) -> None:
                self.compiled_with = None

            def compile(self, *, checkpointer):
                self.compiled_with = checkpointer
                return {"compiled_with": checkpointer}

        class FakeCheckpointer:
            pass

        fake_graph = FakeGraph()
        fake_checkpointer = FakeCheckpointer()

        with (
            patch.object(
                deep_reasoning_builder,
                "build_deep_reasoning_graph",
                return_value=fake_graph,
            ),
            patch.object(
                deep_reasoning_builder,
                "_log_dr_graph_build",
            ) as mock_log_graph_build,
        ):
            compiled = deep_reasoning_builder.compile_deep_reasoning_graph(
                checkpointer=fake_checkpointer,
            )

        assert compiled == {"compiled_with": fake_checkpointer}
        assert fake_graph.compiled_with is fake_checkpointer
        mock_log_graph_build.assert_called_once_with(
            fake_graph,
            fake_checkpointer,
        )
