"""Phase 8 Task 8.2 unit tests for batch telemetry emission.

Locks the metric surface that operators will chart:

- ``record_batch_validation_metrics`` emits candidate/committed gauges,
  per-strategy counters, and the downgrade-reason / rejection-reason
  histograms used to surface validator policy decisions.
- ``record_batch_aggregate_metrics`` emits an aggregate-status counter
  and a batch duration gauge.
- ``record_per_call_metrics`` distinguishes ``denied`` and ``cancelled``
  per-call statuses so the failure mix is visible per tool.
"""

from __future__ import annotations

from agent.execution_strategy import ExecutionStrategy
from agent.graph.subgraphs.tool_execution_runtime.observability import (
    record_batch_aggregate_metrics,
    record_batch_validation_metrics,
    record_per_call_metrics,
    record_per_call_metrics_for_batch,
)
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolCallResult,
    ToolCallStatus,
)


def test_metrics_record_downgrade_reason():
    inc_calls: list[str] = []
    gauge_calls: list[tuple[str, float]] = []

    record_batch_validation_metrics(
        candidate_count=3,
        committed_count=2,
        requested_strategy="parallel",
        effective_strategy="sequential",
        strategy_downgraded=True,
        downgrade_reason="max_concurrent_per_target_exceeded",
        rejected_reason=None,
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )

    assert ("batch_candidate_count", 3.0) in gauge_calls
    assert ("batch_committed_count", 2.0) in gauge_calls
    assert "batch_requested_strategy_parallel" in inc_calls
    assert "batch_effective_strategy_sequential" in inc_calls
    assert "batch_strategy_downgraded_total" in inc_calls
    assert "batch_downgrade_reason_max_concurrent_per_target_exceeded" in inc_calls


def test_metrics_record_validation_rejection_reason():
    inc_calls: list[str] = []

    record_batch_validation_metrics(
        candidate_count=2,
        committed_count=2,
        requested_strategy="sequential",
        effective_strategy="sequential",
        strategy_downgraded=False,
        downgrade_reason=None,
        rejected_reason="tool_call_budget_exceeded",
        inc_fn=inc_calls.append,
        gauge_fn=lambda *_args: None,
    )

    assert "batch_validation_rejected_reason_tool_call_budget_exceeded" in inc_calls
    # No downgrade fired.
    assert "batch_strategy_downgraded_total" not in inc_calls


def test_metrics_record_aggregate_status():
    inc_calls: list[str] = []
    gauge_calls: list[tuple[str, float]] = []

    record_batch_aggregate_metrics(
        aggregate_status="completed_with_errors",
        duration_ms=1234.5,
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )

    assert "batch_aggregate_status_completed_with_errors" in inc_calls
    assert ("batch_duration_ms", 1234.5) in gauge_calls


def test_per_call_metrics_distinguish_denied_and_cancelled():
    inc_calls: list[str] = []
    gauge_calls: list[tuple[str, float]] = []

    record_per_call_metrics(
        tool_id="tool.alpha",
        status="denied",
        duration_ms=10.0,
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )
    record_per_call_metrics(
        tool_id="tool.beta",
        status="cancelled",
        duration_ms=20.0,
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )

    assert "tool_call_status_denied" in inc_calls
    assert "tool_call_status_cancelled" in inc_calls
    assert "tool_call_status_tool_alpha_denied" in inc_calls
    assert "tool_call_status_tool_beta_cancelled" in inc_calls
    assert ("tool_call_duration_ms", 10.0) in gauge_calls
    assert ("tool_call_duration_ms", 20.0) in gauge_calls


def _batch_result_with_rows(rows):
    return BatchResult(
        tool_batch_id="tb_metrics",
        status=BatchStatus.COMPLETED_WITH_ERRORS,
        call_results=tuple(rows),
        effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )


def test_per_call_metrics_for_batch_emits_one_record_per_row():
    inc_calls: list[str] = []
    gauge_calls: list[tuple[str, float]] = []

    rows = [
        ToolCallResult(
            tool_call_id="tc_1",
            tool_id="tool.alpha",
            status=ToolCallStatus.SUCCESS,
            duration_ms=11,
            raw_result={
                "metadata": {
                    "route_policy": {
                        "selected_lane": "container_scoped",
                        "selected_authority": "container_runner_transport",
                    }
                }
            },
        ),
        ToolCallResult(
            tool_call_id="tc_2",
            tool_id="tool.beta",
            status=ToolCallStatus.FAILED,
            duration_ms=22,
            failure_category="tool_error",
            raw_result={
                "metadata": {
                    "route_policy": {
                        "selected_lane": "backend_scoped",
                        "selected_authority": "backend_direct",
                    }
                }
            },
        ),
        ToolCallResult(
            tool_call_id="tc_3",
            tool_id="tool.gamma",
            status=ToolCallStatus.DENIED,
            failure_category="denied",
        ),
        ToolCallResult(
            tool_call_id="tc_4",
            tool_id="tool.delta",
            status=ToolCallStatus.CANCELLED,
            failure_category="batch_cancelled",
        ),
    ]
    record_per_call_metrics_for_batch(
        _batch_result_with_rows(rows),
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )

    # One status counter per row (and one tool-scoped counter per row).
    status_counters = [c for c in inc_calls if c.startswith("tool_call_status_")]
    # 4 rows × 2 increments each (global + tool-scoped) = 8 counter calls.
    assert len(status_counters) == 8
    assert "tool_call_status_success" in inc_calls
    assert "tool_call_status_failed" in inc_calls
    assert "tool_call_status_denied" in inc_calls
    assert "tool_call_status_cancelled" in inc_calls
    assert "tool_call_lane_container_scoped" in inc_calls
    assert "tool_call_authority_runner" in inc_calls
    assert "tool_call_lane_authority_container_scoped_runner" in inc_calls
    assert "tool_call_lane_backend_scoped" in inc_calls
    assert "tool_call_authority_cloud" in inc_calls
    assert "tool_call_lane_authority_backend_scoped_cloud" in inc_calls

    # Duration gauge fires once per row, including zero-duration denied/cancelled rows.
    duration_gauges = [g for g in gauge_calls if g[0] == "tool_call_duration_ms"]
    assert len(duration_gauges) == 4
    assert (("tool_call_duration_ms", 11.0)) in duration_gauges
    assert (("tool_call_duration_ms", 22.0)) in duration_gauges
    assert (("tool_call_duration_ms", 0.0)) in duration_gauges  # denied + cancelled


def test_per_call_metrics_emits_unknown_lane_authority_when_route_policy_missing():
    inc_calls: list[str] = []

    record_per_call_metrics(
        tool_id="tool.alpha",
        status="success",
        duration_ms=5.0,
        raw_result={},
        inc_fn=inc_calls.append,
        gauge_fn=lambda *_args: None,
    )

    assert "tool_call_lane_unknown" in inc_calls
    assert "tool_call_authority_unknown" in inc_calls
    assert "tool_call_lane_authority_unknown_unknown" in inc_calls


def test_per_call_metrics_for_batch_handles_empty_results():
    inc_calls: list[str] = []
    gauge_calls: list[tuple[str, float]] = []
    record_per_call_metrics_for_batch(
        _batch_result_with_rows([]),
        inc_fn=inc_calls.append,
        gauge_fn=lambda name, value: gauge_calls.append((name, value)),
    )
    assert inc_calls == []
    assert gauge_calls == []
