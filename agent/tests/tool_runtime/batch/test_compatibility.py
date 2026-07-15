"""Phase 4 Task 4.2 unit tests for ``BatchCompatibilityChecker``.

Locks the explicit-transport, parallel-compatible, avoid-with, same-target
concurrency, and single-call branches.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.compatibility import (
    BatchCompatibilityChecker,
    CompatibilityOutcome,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall


def _meta(
    *,
    batch_audited=True,
    parallel_compatible=True,
    avoid_with=None,
    max_concurrent_per_target=1,
):
    return SimpleNamespace(
        batch_audited=batch_audited,
        parallel_compatible=parallel_compatible,
        avoid_with=list(avoid_with or []),
        max_concurrent_per_target=max_concurrent_per_target,
    )


def _batch(tool_ids, strategy=ExecutionStrategy.PARALLEL, params_by_index=None):
    params_by_index = params_by_index or {}
    return ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=tuple(
            ToolCall(
                tool_call_id=f"tc_{idx}",
                tool_id=tid,
                parameters=dict(params_by_index.get(idx, {})),
            )
            for idx, tid in enumerate(tool_ids)
        ),
        requested_execution_strategy=strategy,
    )


def _patch_metadata(monkeypatch, mapping):
    """Patch the per-tool metadata resolver used by the checker."""
    from agent.tool_runtime.batch import compatibility as compat_module

    def _lookup(tool_id):
        return mapping.get(tool_id)

    monkeypatch.setattr(compat_module, "_metadata_for", _lookup)


def test_batch_audited_marker_does_not_gate_parallel(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {
            "tool.a": _meta(batch_audited=False),
            "tool.b": _meta(batch_audited=False),
        },
    )
    verdict = BatchCompatibilityChecker().check(_batch(["tool.a", "tool.b"]))
    assert verdict.outcome is CompatibilityOutcome.PARALLEL_OK
    assert verdict.effective_strategy is ExecutionStrategy.PARALLEL
    assert verdict.reason is None


def test_avoid_with_conflict_downgrades(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {
            "tool.a": _meta(avoid_with=["tool.b"]),
            "tool.b": _meta(),
        },
    )
    verdict = BatchCompatibilityChecker().check(_batch(["tool.a", "tool.b"]))
    assert verdict.outcome is CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL
    assert verdict.reason == "avoid_with_conflict"


def test_parallel_compatible_false_downgrades(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {
            "tool.a": _meta(),
            "tool.b": _meta(parallel_compatible=False),
        },
    )
    verdict = BatchCompatibilityChecker().check(_batch(["tool.a", "tool.b"]))
    assert verdict.outcome is CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL
    assert verdict.reason == "parallel_compatible_false"


def test_explicit_pty_transport_stays_parallel_compatible(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {
            "tool.a": _meta(),
            "tool.b": _meta(),
        },
    )
    verdict = BatchCompatibilityChecker().check(
        _batch(
            ["tool.a", "tool.b"],
            params_by_index={0: {"target": "127.0.0.1", "transport": "pty"}},
        )
    )
    assert verdict.outcome is CompatibilityOutcome.PARALLEL_OK
    assert verdict.effective_strategy is ExecutionStrategy.PARALLEL
    assert verdict.reason is None


def test_compatible_audited_set_is_parallel_ok(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {
            "tool.a": _meta(),
            "tool.b": _meta(),
        },
    )
    verdict = BatchCompatibilityChecker().check(_batch(["tool.a", "tool.b"]))
    assert verdict.outcome is CompatibilityOutcome.PARALLEL_OK
    assert verdict.effective_strategy is ExecutionStrategy.PARALLEL


def test_same_tool_same_target_above_limit_downgrades(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(max_concurrent_per_target=1)},
    )
    verdict = BatchCompatibilityChecker().check(
        _batch(
            ["tool.a", "tool.a"],
            params_by_index={
                0: {"target": "127.0.0.1", "ports": "80"},
                1: {"target": "127.0.0.1", "ports": "443"},
            },
        )
    )
    assert verdict.outcome is CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL
    assert verdict.reason == "max_concurrent_per_target_exceeded"


def test_same_tool_same_target_within_limit_is_parallel_ok(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(max_concurrent_per_target=2)},
    )
    verdict = BatchCompatibilityChecker().check(
        _batch(
            ["tool.a", "tool.a"],
            params_by_index={
                0: {"target": "127.0.0.1", "ports": "80"},
                1: {"target": "127.0.0.1", "ports": "443"},
            },
        )
    )
    assert verdict.outcome is CompatibilityOutcome.PARALLEL_OK
    assert verdict.effective_strategy is ExecutionStrategy.PARALLEL


def test_single_call_batch_runs_sequentially(monkeypatch):
    _patch_metadata(monkeypatch, {"tool.a": _meta()})
    verdict = BatchCompatibilityChecker().check(_batch(["tool.a"]))
    assert verdict.effective_strategy is ExecutionStrategy.SEQUENTIAL


def test_explicit_sequential_request_passes_through(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(batch_audited=False), "tool.b": _meta()},
    )
    verdict = BatchCompatibilityChecker().check(
        _batch(["tool.a", "tool.b"], strategy=ExecutionStrategy.SEQUENTIAL)
    )
    # Builder asked for sequential — no downgrade is necessary.
    assert verdict.effective_strategy is ExecutionStrategy.SEQUENTIAL
