"""Phase 3 Task 3.2 unit tests for ``batch_commit.commit_tool_batch``.

Locks the structural-validation contract:

- Single-call envelopes are accepted.
- Multi-call envelopes up to ``max_calls`` are accepted.
- Counts above ``max_calls`` are rejected with a machine-readable reason.
- Unknown tool ids (not in candidate set) are rejected.
- Placeholder / result-dependent parameter values are rejected.

The commit cap is supplied by the caller (sourced from
``AgentConfig.max_committed_tools_per_batch``); the module under test must
not bake any literal cap.
"""

from __future__ import annotations

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.reasoning.batch_commit import BatchCommitError, commit_tool_batch


def _envelope(tool_calls, **extras):
    payload = {"tool_calls": tool_calls}
    payload.update(extras)
    return payload


def _commit(envelope, *, candidate_tool_ids=None, max_calls=3, strategy=ExecutionStrategy.SEQUENTIAL):
    return commit_tool_batch(
        envelope,
        candidate_tool_ids=candidate_tool_ids or ["shell.exec"],
        max_calls=max_calls,
        requested_execution_strategy=strategy,
    )


def test_commit_single_call_batch():
    batch = _commit(
        _envelope([{"tool_id": "shell.exec", "parameters": {"command": "ls"}}]),
    )

    assert len(batch.tool_calls) == 1
    assert batch.tool_calls[0].tool_id == "shell.exec"
    assert batch.tool_calls[0].tool_call_id.startswith("tc_")
    assert batch.tool_batch_id.startswith("tb_")
    assert batch.requested_execution_strategy is ExecutionStrategy.SEQUENTIAL


@pytest.mark.parametrize("max_calls", [1, 2, 3, 5])
def test_commit_max_call_batch(max_calls):
    tool_calls = [
        {"tool_id": "shell.exec", "parameters": {"command": f"echo {i}"}}
        for i in range(max_calls)
    ]
    batch = _commit(
        _envelope(tool_calls),
        max_calls=max_calls,
        strategy=ExecutionStrategy.PARALLEL,
    )

    assert len(batch.tool_calls) == max_calls
    assert batch.requested_execution_strategy is ExecutionStrategy.PARALLEL
    # Each call must mint a distinct tool_call_id.
    ids = {call.tool_call_id for call in batch.tool_calls}
    assert len(ids) == max_calls


@pytest.mark.parametrize("max_calls,committed", [(1, 2), (2, 3), (3, 4)])
def test_reject_count_above_max_calls(max_calls, committed):
    tool_calls = [
        {"tool_id": "shell.exec", "parameters": {"command": f"echo {i}"}}
        for i in range(committed)
    ]
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope(tool_calls),
            max_calls=max_calls,
        )
    assert exc_info.value.reason == "tool_calls_above_max"


def test_reject_empty_tool_calls():
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([]),
        )
    assert exc_info.value.reason == "empty_tool_calls"


def test_reject_unknown_tool_id():
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "not.a.candidate", "parameters": {}}]),
        )
    assert exc_info.value.reason == "unknown_tool_id"


def test_reject_placeholder_in_parameter():
    placeholder_envelope = _envelope(
        [
            {
                "tool_id": "shell.exec",
                "parameters": {"command": "cat ${prev.output}"},
            }
        ]
    )
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            placeholder_envelope,
        )
    assert exc_info.value.reason == "placeholder_parameters"


def test_reject_parameters_not_json_when_string_is_not_valid_json():
    # Wire shape uses JSON-encoded string parameters; a malformed string
    # surfaces as parameters_not_json so callers can branch on the failure
    # mode separately from "decoded successfully but isn't an object".
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "shell.exec", "parameters": "not-an-object"}]),
        )
    assert exc_info.value.reason == "parameters_not_json"


def test_reject_parameters_not_mapping_when_decoded_value_is_not_object():
    # JSON decodes successfully but the result is a primitive, not an object.
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "shell.exec", "parameters": "42"}]),
        )
    assert exc_info.value.reason == "parameters_not_mapping"


def test_reject_parameters_not_mapping_when_neither_string_nor_mapping():
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "shell.exec", "parameters": 42}]),
        )
    assert exc_info.value.reason == "parameters_not_mapping"


def test_accepts_json_encoded_parameters_string_from_wire():
    envelope = _envelope(
        [
            {
                "tool_id": "shell.exec",
                "parameters": '{"command": "ls -la", "timeout": 30}',
            }
        ]
    )
    batch = _commit(
        envelope,
    )
    assert batch.tool_calls[0].parameters == {"command": "ls -la", "timeout": 30}


def test_reject_invalid_requested_execution_strategy():
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "shell.exec", "parameters": {}}]),
            candidate_tool_ids=["shell.exec"],
            max_calls=3,
            strategy="hyperdrive",
        )
    assert exc_info.value.reason == "invalid_execution_strategy"


def test_envelope_execution_strategy_is_ignored():
    batch = _commit(
        _envelope(
            [{"tool_id": "shell.exec", "parameters": {"command": "ls"}}],
            execution_strategy="parallel",
        ),
        strategy=ExecutionStrategy.SEQUENTIAL,
    )

    assert batch.requested_execution_strategy is ExecutionStrategy.SEQUENTIAL


def test_deferred_followups_and_rationale_round_trip():
    batch = _commit(
        _envelope(
            [{"tool_id": "shell.exec", "parameters": {"command": "ls"}, "intent": "list cwd"}],
            deferred_followups=["scan discovered hosts after"],
            selection_rationale="shell.exec is enough",
        ),
    )

    assert batch.tool_calls[0].intent == "list cwd"
    assert batch.deferred_followups == ("scan discovered hosts after",)
    assert batch.selection_rationale == "shell.exec is enough"


def test_max_calls_must_be_positive_integer():
    with pytest.raises(BatchCommitError) as exc_info:
        _commit(
            _envelope([{"tool_id": "shell.exec", "parameters": {}}]),
            max_calls=0,
        )
    assert exc_info.value.reason == "invalid_max_calls"
