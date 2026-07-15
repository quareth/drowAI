"""Phase 4 Task 4.3 unit tests for ``BatchValidator``.

Locks the validator's full decision matrix:

- Tool-call budget pre-check (gap #7).
- Commit cap from ``AgentConfig.max_committed_tools_per_batch``.
- Compatibility downgrade with ``downgrade_reason`` recorded.
- Both requested + effective strategies preserved on the result.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.compatibility import (
    CompatibilityOutcome,
    CompatibilityVerdict,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tool_runtime.batch.validator import BatchValidator


def _batch(tool_ids, *, strategy=ExecutionStrategy.PARALLEL, params_by_index=None):
    params_by_index = params_by_index or {}
    return ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=tuple(
            ToolCall(
                tool_call_id=f"tc_{i}",
                tool_id=tid,
                parameters=dict(params_by_index.get(i, {})),
            )
            for i, tid in enumerate(tool_ids)
        ),
        requested_execution_strategy=strategy,
    )


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


def _patch_metadata(monkeypatch, mapping):
    from agent.tool_runtime.batch import compatibility as compat_module

    monkeypatch.setattr(compat_module, "_metadata_for", lambda tid: mapping.get(tid))


def _validating_ctx(tool_ids, **overrides):
    def _validate(tool_id, params, **kwargs):
        _ = kwargs
        if params.get("invalid"):
            return SimpleNamespace(valid=False, normalized_parameters={}, reason="invalid")
        return SimpleNamespace(valid=True, normalized_parameters=dict(params))

    ctx = {
        "max_committed_tools_per_batch": 3,
        "available_tool_ids": list(tool_ids),
        "candidate_tool_ids": list(tool_ids),
        "validate_tool_parameters_fn": _validate,
    }
    ctx.update(overrides)
    return ctx


def test_validator_records_requested_and_effective_strategies(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(parallel_compatible=False), "tool.b": _meta()},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b"]),
        ctx=_validating_ctx(["tool.a", "tool.b"]),
    )
    assert result.admitted is True
    assert result.requested_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.effective_execution_strategy is ExecutionStrategy.SEQUENTIAL
    assert result.strategy_downgraded is True
    assert result.downgrade_reason == "parallel_compatible_false"


def test_validator_passes_through_compatible_audited_set(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(), "tool.b": _meta()},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b"]),
        ctx=_validating_ctx(["tool.a", "tool.b"]),
    )
    assert result.admitted is True
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.strategy_downgraded is False
    assert result.downgrade_reason is None


def test_validator_ignores_batch_audited_marker(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(batch_audited=False), "tool.b": _meta(batch_audited=False)},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b"]),
        ctx=_validating_ctx(["tool.a", "tool.b"]),
    )
    assert result.admitted is True
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.downgrade_reason is None


def test_validator_rejects_when_batch_overshoots_remaining_tool_call_budget(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(), "tool.b": _meta(), "tool.c": _meta()},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b", "tool.c"]),
        ctx={
            **_validating_ctx(["tool.a", "tool.b", "tool.c"]),
            "max_committed_tools_per_batch": 5,
            "max_tool_calls": 10,
            "tool_calls_used": 9,  # only 1 call remaining
        },
    )
    assert result.admitted is False
    assert result.rejected_reason == "tool_call_budget_exceeded"


def test_validator_admits_when_batch_fits_remaining_tool_call_budget(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(), "tool.b": _meta()},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b"]),
        ctx={
            **_validating_ctx(["tool.a", "tool.b"]),
            "max_committed_tools_per_batch": 5,
            "max_tool_calls": 10,
            "tool_calls_used": 6,  # 4 remaining; batch needs 2.
        },
    )
    assert result.admitted is True


def test_validator_rejects_when_batch_exceeds_commit_cap(monkeypatch):
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(), "tool.b": _meta(), "tool.c": _meta()},
    )
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b", "tool.c"]),
        ctx=_validating_ctx(["tool.a", "tool.b", "tool.c"], max_committed_tools_per_batch=2),
    )
    assert result.admitted is False
    assert result.rejected_reason == "tool_calls_above_max"


def test_validator_skips_budget_check_when_facts_absent(monkeypatch):
    _patch_metadata(monkeypatch, {"tool.a": _meta()})
    # No max_tool_calls/tool_calls_used in ctx — legacy behavior.
    result = BatchValidator().validate(
        _batch(["tool.a"]),
        ctx=_validating_ctx(["tool.a"]),
    )
    assert result.admitted is True


def test_validator_uses_injected_compatibility_checker(monkeypatch):
    class _ForceReject:
        def check(self, batch):
            return CompatibilityVerdict(
                outcome=CompatibilityOutcome.REJECT,
                effective_strategy=ExecutionStrategy.SEQUENTIAL,
                reason="forced_reject",
            )

    result = BatchValidator(compatibility=_ForceReject()).validate(
        _batch(["tool.a"]),
        ctx=_validating_ctx(["tool.a"]),
    )
    assert result.admitted is False
    assert result.rejected_reason == "forced_reject"


def test_validator_allows_repeated_tool_ids_when_call_ids_are_unique(monkeypatch):
    _patch_metadata(monkeypatch, {"tool.a": _meta()})
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.a"]),
        ctx=_validating_ctx(["tool.a"]),
    )
    assert result.admitted is True
    assert [call.tool_id for call in result.batch.tool_calls] == ["tool.a", "tool.a"]
    assert [call.tool_call_id for call in result.batch.tool_calls] == ["tc_0", "tc_1"]


def test_validator_downgrades_same_target_concurrency_above_limit(monkeypatch):
    _patch_metadata(monkeypatch, {"tool.a": _meta(max_concurrent_per_target=1)})
    result = BatchValidator().validate(
        _batch(
            ["tool.a", "tool.a"],
            params_by_index={
                0: {"target": "127.0.0.1", "ports": "80"},
                1: {"target": "127.0.0.1", "ports": "443"},
            },
        ),
        ctx=_validating_ctx(["tool.a"]),
    )
    assert result.admitted is True
    assert result.effective_execution_strategy is ExecutionStrategy.SEQUENTIAL
    assert result.downgrade_reason == "max_concurrent_per_target_exceeded"


def test_validator_keeps_explicit_pty_transport_parallel(monkeypatch):
    _patch_metadata(monkeypatch, {"tool.a": _meta(), "tool.b": _meta()})
    result = BatchValidator().validate(
        _batch(
            ["tool.a", "tool.b"],
            params_by_index={1: {"target": "127.0.0.1", "transport": "pty"}},
        ),
        ctx=_validating_ctx(["tool.a", "tool.b"]),
    )
    assert result.admitted is True
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.downgrade_reason is None


def test_validator_rejects_duplicate_tool_call_ids():
    batch = ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=(
            ToolCall(tool_call_id="tc_dup", tool_id="tool.a", parameters={}),
            ToolCall(tool_call_id="tc_dup", tool_id="tool.a", parameters={}),
        ),
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )
    result = BatchValidator().validate(batch, ctx=_validating_ctx(["tool.a"]))
    assert result.admitted is False
    assert result.rejected_reason == "duplicate_tool_call_id"


def test_validator_rejects_non_candidate_tool():
    result = BatchValidator().validate(
        _batch(["tool.a", "tool.b"]),
        ctx=_validating_ctx(["tool.a", "tool.b"], candidate_tool_ids=["tool.a"]),
    )
    assert result.admitted is False
    assert result.rejected_reason == "tool_not_in_candidate_set"


def test_validator_rejects_missing_tool():
    result = BatchValidator().validate(
        _batch(["tool.missing"]),
        ctx=_validating_ctx([], available_tool_ids=[]),
    )
    assert result.admitted is False
    assert result.rejected_reason == "tool_not_available"


def test_validator_rejects_invalid_parameters():
    batch = ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=(
            ToolCall(tool_call_id="tc_1", tool_id="tool.a", parameters={"invalid": True}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    result = BatchValidator().validate(batch, ctx=_validating_ctx(["tool.a"]))
    assert result.admitted is False
    assert result.rejected_reason == "invalid_parameters:tool.a"


def test_validator_rejects_placeholders_after_deserialization():
    batch = ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=(
            ToolCall(tool_call_id="tc_1", tool_id="tool.a", parameters={"target": "${prior}"}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    result = BatchValidator().validate(batch, ctx=_validating_ctx(["tool.a"]))
    assert result.admitted is False
    assert result.rejected_reason == "placeholder_parameters"


def test_validator_rejects_high_risk_multi_call_batch():
    result = BatchValidator().validate(
        _batch(["shell.exec", "tool.a"], strategy=ExecutionStrategy.SEQUENTIAL),
        ctx=_validating_ctx(
            ["shell.exec", "tool.a"],
            high_risk_tool_prefixes=("shell.exec",),
        ),
    )
    assert result.admitted is False
    assert result.rejected_reason == "high_risk_tool_in_batch"


def test_exclusive_tool_in_multi_call_rejects(monkeypatch):
    from agent.tools import compatibility as tool_compat
    from agent.tool_runtime.batch import compatibility as batch_compat

    class _StubAnalyzer:
        def __init__(self) -> None:
            self.compatibility_matrix = {
                ("tool.a", "tool.b"): tool_compat.CompatibilityLevel.EXCLUSIVE,
            }

    monkeypatch.setattr(tool_compat, "ToolCompatibilityAnalyzer", _StubAnalyzer)
    _patch_metadata(
        monkeypatch,
        {"tool.a": _meta(), "tool.b": _meta()},
    )

    result = BatchValidator(compatibility=batch_compat.BatchCompatibilityChecker()).validate(
        _batch(["tool.a", "tool.b"], strategy=ExecutionStrategy.SEQUENTIAL),
        ctx=_validating_ctx(["tool.a", "tool.b"]),
    )
    assert result.admitted is False
    assert result.rejected_reason.startswith("exclusive_tool_conflict:")
    assert "tool.a" in result.rejected_reason
    assert "tool.b" in result.rejected_reason
