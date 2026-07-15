"""Unit tests for batch_runner helpers wired by the orchestrator.

Phase 1.1 — ``build_batch_cancel_check`` reuses the same lifecycle source
the single-tool path queries (``run_lifecycle.is_cancel_requested``).
Phase 1.3 — ``validate_batch`` falls back to the selector candidate count
(not the committed batch size) so the candidate-count gauge is meaningful.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.graph.subgraphs.tool_execution_runtime import batch_runner
from agent.tool_runtime.batch.types import ToolBatch, ToolCall


def test_cancel_check_returns_none_without_task_or_turn():
    assert batch_runner.build_batch_cancel_check(task_id=None, turn_id="turn-1") is None
    assert batch_runner.build_batch_cancel_check(task_id=42, turn_id=None) is None
    assert batch_runner.build_batch_cancel_check(task_id="", turn_id="turn-1") is None
    assert batch_runner.build_batch_cancel_check(task_id=42, turn_id="") is None


def test_cancel_check_polls_lifecycle_and_caches_true(monkeypatch):
    poll_count = {"n": 0}
    pending = {"flag": False}

    class FakeLifecycle:
        def is_cancel_requested(self, *, task_id, turn_id):
            poll_count["n"] += 1
            return pending["flag"]

    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.run_lifecycle.get_run_lifecycle_service",
        lambda: FakeLifecycle(),
    )

    cancel_check = batch_runner.build_batch_cancel_check(task_id=7, turn_id="turn-x")
    assert cancel_check is not None

    # First call hits the lifecycle source.
    assert cancel_check() is False
    assert poll_count["n"] == 1

    # Throttled: a second call within the poll window does NOT hit the source.
    assert cancel_check() is False
    assert poll_count["n"] == 1

    # Flip the source. The closure stays cached at False until the next poll
    # window, so we force a poll by clearing the throttle state directly.
    pending["flag"] = True
    # Allow a fresh poll: simulate time passing by reaching into the closure
    # cells via __closure__ is messy; instead, just check that once the
    # cached value is True it stays True without re-polling.
    # We bypass throttle by constructing a fresh checker.
    cancel_check2 = batch_runner.build_batch_cancel_check(task_id=7, turn_id="turn-x")
    assert cancel_check2 is not None
    assert cancel_check2() is True
    polls_before = poll_count["n"]
    # Once cached True, subsequent calls do not re-poll.
    assert cancel_check2() is True
    assert poll_count["n"] == polls_before


def test_cancel_check_swallows_lifecycle_exception(monkeypatch):
    class BoomLifecycle:
        def is_cancel_requested(self, *, task_id, turn_id):
            raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.run_lifecycle.get_run_lifecycle_service",
        lambda: BoomLifecycle(),
    )

    cancel_check = batch_runner.build_batch_cancel_check(task_id=7, turn_id="turn-x")
    assert cancel_check is not None
    # Exception in the lifecycle call must not raise out of the cancel hook.
    assert cancel_check() is False


def test_cancel_check_returns_none_when_lifecycle_module_unavailable(monkeypatch):
    import sys

    # Remove any compatibility alias for the same module so the deferred import fails.
    runtime_module_name = "backend.services.langgraph_chat.runtime.run_lifecycle"
    runtime_module = sys.modules.get(runtime_module_name)
    for module_name, module in list(sys.modules.items()):
        if module is runtime_module and module_name.endswith(".run_lifecycle"):
            monkeypatch.delitem(sys.modules, module_name, raising=False)
    runtime_package = sys.modules.get("backend.services.langgraph_chat.runtime")
    if runtime_package is not None:
        monkeypatch.delattr(runtime_package, "run_lifecycle", raising=False)
    monkeypatch.setitem(sys.modules, "backend.services.langgraph_chat.runtime.run_lifecycle", None)

    cancel_check = batch_runner.build_batch_cancel_check(task_id=7, turn_id="turn-x")
    assert cancel_check is None


# ---------------------------------------------------------------------------
# Phase 1.3 — candidate_count fallback
# ---------------------------------------------------------------------------


def _one_call_batch(tool_id: str = "tool.alpha") -> ToolBatch:
    return ToolBatch(
        tool_batch_id="tb_candidate_count",
        tool_calls=(ToolCall(tool_call_id="tc_0", tool_id=tool_id, parameters={}),),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )


class _FactsWithCandidates:
    def __init__(self, tool_candidates):
        self.tool_candidates = list(tool_candidates)
        self.metadata = {}
        self.tool_calls_used = 0
        self.budgets = SimpleNamespace(max_tool_calls=100)


class _Config:
    max_committed_tools_per_batch = 3
    shell_exec_max_command_chars = 320


def test_validate_batch_candidate_count_uses_selector_list_when_explicit_arg_omitted(
    monkeypatch,
):
    recorded: dict = {}

    def fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.observability.record_batch_validation_metrics",
        fake_record,
    )

    facts = _FactsWithCandidates(["tool.alpha", "tool.beta", "tool.gamma"])
    batch_runner.validate_batch(_one_call_batch("tool.alpha"), config=_Config(), facts=facts)

    # Selector returned 3 candidates; builder committed 1.
    # The metric MUST report 3, not 1 (audit-gap signal).
    assert recorded.get("candidate_count") == 3
    assert recorded.get("committed_count") == 1


def test_validate_batch_candidate_count_explicit_arg_wins(monkeypatch):
    recorded: dict = {}

    def fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.observability.record_batch_validation_metrics",
        fake_record,
    )

    facts = _FactsWithCandidates(["tool.alpha", "tool.beta", "tool.gamma"])
    batch_runner.validate_batch(
        _one_call_batch("tool.alpha"),
        config=_Config(),
        facts=facts,
        candidate_count=42,
    )

    # Explicit kwarg wins over derived candidates.
    assert recorded.get("candidate_count") == 42
    assert recorded.get("committed_count") == 1


def test_validate_batch_candidate_count_falls_back_to_committed_when_no_candidates(
    monkeypatch,
):
    recorded: dict = {}

    def fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.observability.record_batch_validation_metrics",
        fake_record,
    )

    facts = _FactsWithCandidates([])  # canonical batch with no selector list
    batch_runner.validate_batch(_one_call_batch("tool.alpha"), config=_Config(), facts=facts)

    # No selector context; committed_count is the safe non-zero fallback.
    assert recorded.get("candidate_count") == 1
    assert recorded.get("committed_count") == 1
