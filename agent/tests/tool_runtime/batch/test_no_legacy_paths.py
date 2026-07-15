"""Phase 9 grep-style tests that lock the migration end-state.

These tests are intentionally cheap — they read source files (not run code)
to assert the absence of legacy enum values, module-level numeric cap
literals, dual approval-payload builders, dual ``last_tool_result_compact``
authors, old single-tool batch synthesis, and the now-retired
``action_orchestrator`` / ``ConcurrentToolExecutor`` symbols. They surface
regressions early when someone reintroduces a duplicate authoring site,
copies a module-level cap into the batch package, or resurrects a legacy
execution path.

Notes on Phase 9 scope:

- ``test_no_concurrent_enum_value`` locks the strategy rename (Task 1.1).
- ``test_no_module_level_cap_constants`` greps the batch package + the
  builder commit module for numeric cap literals so the cap continues to
  flow from ``AgentConfig`` instead of constants.
- ``test_compact_field_authored_once`` locks Task 9.2: the legacy
  ``last_tool_result_compact`` is authored as a derived projection in
  ``batch_runner.write_compact_batch_metadata`` (alongside the
  batch-shaped ``last_tool_result_compact_batch`` twin). The projection
  helper (``result_state_projection.py``) no longer writes the field directly.
- ``test_one_payload_builder`` asserts ``build_tool_approval_payload`` is
  the only approval payload builder (Task 7.1 single surface).
- ``test_no_action_orchestrator_imports`` and
  ``test_no_concurrent_tool_executor_imports`` lock Task 9.1 (legacy
  module + class deletion).
- ``test_no_single_tool_batch_synthesis`` locks the ToolBatch authority rule:
  active execution must not synthesize runnable batches from old planner fields.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_no_concurrent_enum_value():
    """``ExecutionStrategy.CONCURRENT`` must not exist anywhere in code."""
    targets = [
        "agent/execution_strategy.py",
        "agent/models.py",
        "agent/tool_runtime/coordinator.py",
        "agent/tool_runtime/batch/validator.py",
        "agent/tool_runtime/batch/executor.py",
        "agent/tool_runtime/batch/types.py",
        "agent/tool_runtime/batch/compatibility.py",
        "agent/tool_runtime/batch/aggregator.py",
    ]
    for path in targets:
        text = _read(path)
        assert "ExecutionStrategy.CONCURRENT" not in text, (
            f"{path} still references the legacy CONCURRENT enum value"
        )


def test_no_module_level_cap_constants():
    """The batch package must not define numeric cap literals."""
    targets = [
        "agent/tool_runtime/batch/validator.py",
        "agent/tool_runtime/batch/executor.py",
        "agent/tool_runtime/batch/types.py",
        "agent/tool_runtime/batch/aggregator.py",
        "agent/tool_runtime/batch/compatibility.py",
        "agent/tool_runtime/batch/emitter.py",
        "agent/tool_runtime/batch/ids.py",
        "agent/reasoning/batch_commit.py",
    ]
    cap_pattern = re.compile(
        r"^\s*(DEFAULT_MAX_CANDIDATES|MAX_TOOLS_PER_BATCH|MAX_CALLS|MAX_BATCH_CALLS)\s*[:=]"
    )
    for path in targets:
        text = _read(path)
        for line in text.splitlines():
            assert not cap_pattern.match(line), (
                f"{path} contains a module-level cap literal — caps must come from AgentConfig"
            )


def test_compact_field_authored_once():
    """``last_tool_result_compact`` has exactly one author site (Phase 2.2 + Task 9.2).

    Phase 9 Task 9.2 collapsed the dual-write to
    ``batch_runner.write_compact_batch_metadata``. Active execution now
    requires a canonical ``planner_plan.tool_batch`` manifest, so there is no
    no-batch fallback writer in the orchestrator.
    """
    projection_text = _read(
        "agent/graph/subgraphs/tool_execution_runtime/result_state_projection.py"
    )
    assert (
        projection_text.count('metadata["last_tool_result_compact"] = ') == 0
    ), "result_state_projection.py must not author last_tool_result_compact (Task 9.2 derivation contract)"

    batch_runner_text = _read(
        "agent/graph/subgraphs/tool_execution_runtime/batch_runner.py"
    )
    assert (
        batch_runner_text.count('metadata["last_tool_result_compact_batch"] = ') == 1
    ), "batch_runner.py must author last_tool_result_compact_batch exactly once"
    assert (
        batch_runner_text.count('metadata["last_tool_result_compact"] = ') == 1
    ), "batch_runner.py must author the derived legacy last_tool_result_compact exactly once"

    orchestrator_text = _read(
        "agent/graph/subgraphs/tool_execution_runtime/orchestrator.py"
    )
    # Phase 2.2 (re-audit): legacy single-call body deleted, so there is
    # NO orchestrator-side authoring of last_tool_result_compact anymore.
    # Every path now flows through batch_runner.write_compact_batch_metadata.
    assert (
        orchestrator_text.count('metadata["last_tool_result_compact"] = ') == 0
    ), "orchestrator must not directly author last_tool_result_compact (Phase 2.2: single execution path)"


def test_no_single_tool_batch_synthesis():
    """Old planner fields must not be converted into executable ToolBatches."""
    assert not (REPO_ROOT / "agent/tool_runtime/batch/legacy_adapter.py").exists(), (
        "agent/tool_runtime/batch/legacy_adapter.py must be deleted"
    )
    orchestrator_text = _read(
        "agent/graph/subgraphs/tool_execution_runtime/orchestrator.py"
    )
    assert "synthesize_legacy_batch" not in orchestrator_text
    assert "legacy_adapter" not in orchestrator_text
    assert "requires planner_plan.tool_batch" in orchestrator_text


def test_no_action_orchestrator_imports():
    """``agent/execution/action_orchestrator.py`` is gone (Task 9.1)."""
    assert not (REPO_ROOT / "agent/execution/action_orchestrator.py").exists(), (
        "agent/execution/action_orchestrator.py must be deleted (Task 9.1)"
    )
    # No live module references the symbol either.
    forbidden_paths = [
        "agent/executor.py",
        "agent/reasoning/enhanced_planner_impl.py",
        "agent/graph/subgraphs/tool_execution_runtime/orchestrator.py",
    ]
    for path in forbidden_paths:
        text = _read(path)
        assert "action_orchestrator" not in text, (
            f"{path} still references action_orchestrator (Task 9.1 retirement)"
        )
        assert "execute_with_enhanced_selection" not in text, (
            f"{path} still references execute_with_enhanced_selection (Task 9.1 retirement)"
        )


def test_no_concurrent_tool_executor_imports():
    """``ConcurrentToolExecutor`` and its module are gone (Task 9.1)."""
    assert not (REPO_ROOT / "agent/tools/concurrent_executor.py").exists(), (
        "agent/tools/concurrent_executor.py must be deleted (Task 9.1)"
    )
    forbidden_paths = [
        "agent/executor.py",
        "agent/reasoning/enhanced_planner_impl.py",
        "agent/tools/__init__.py",
        "agent/tools/tool_registry.py",
    ]
    for path in forbidden_paths:
        text = _read(path)
        assert "ConcurrentToolExecutor" not in text, (
            f"{path} still references ConcurrentToolExecutor (Task 9.1 retirement)"
        )
        assert "concurrent_executor" not in text, (
            f"{path} still references concurrent_executor (Task 9.1 retirement)"
        )


def test_one_payload_builder():
    """Only ``build_tool_approval_payload`` exists as the approval payload builder."""
    text = _read("agent/graph/nodes/hitl_helpers.py")
    matches = re.findall(r"^def\s+build_tool_approval_payload\s*\(", text, re.MULTILINE)
    assert len(matches) == 1
    # No alternative builder names live anywhere in the repo.
    forbidden = ("build_tool_batch_approval_payload", "build_batch_approval_payload")
    for name in forbidden:
        assert name not in text, (
            f"Found forbidden alternate builder name {name} in hitl_helpers.py"
        )
