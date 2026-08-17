"""Phase 6 Task 6.3 unit tests for ``read_compact_evidence``.

Locks the batch-preferred reading contract: when both metadata fields are
present the helper returns the batch view; otherwise falls back to the
legacy single-tool field. Failure detection must use the batch aggregate
when present so partial failures are not hidden by the compatibility
single-result field.
"""

from __future__ import annotations

import pytest

from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    read_compact_evidence,
    register_runtime_compact_evidence,
    select_compact_evidence_for_reasoning,
)


def _batch_metadata(*, success):
    return {
        "tool_batch_id": "tb-1",
        "execution_strategy": "parallel",
        "status": "completed" if success else "completed_with_errors",
        "success": success,
        "results": [
            {
                "tool_call_id": "tc-1",
                "tool_id": "web.ffuf",
                "intent": "find paths",
                "status": "success",
                "success": True,
            },
            {
                "tool_call_id": "tc-2",
                "tool_id": "web.whatweb",
                "intent": "fingerprint",
                "status": "success" if success else "failed",
                "success": success,
                "failure_category": None if success else "timeout",
            },
        ],
        "deferred_followups": ["scan paths after"],
    }


def test_evidence_helper_prefers_batch():
    metadata = {
        "last_tool_result_compact": {"tool": "web.ffuf", "summary": "ok"},
        "last_tool_result_compact_batch": _batch_metadata(success=True),
    }
    view = read_compact_evidence(metadata)
    assert view is not None
    assert view.source == "batch"
    assert view.status == "completed"
    assert view.success is True
    assert len(view.rows) == 2
    assert view.successful_rows == tuple(view.rows)
    assert view.failed_rows == ()
    assert view.deferred_followups == ("scan paths after",)


def test_ptr_failure_detection_uses_batch_aggregate():
    """Even when the legacy single field looks like success, the batch view
    surfaces partial failures so PTR's failure detection sees them."""
    metadata = {
        # legacy single-field would suggest "everything ok"
        "last_tool_result_compact": {"tool": "web.ffuf", "summary": "ok"},
        # but the batch aggregate has a failed sibling
        "last_tool_result_compact_batch": _batch_metadata(success=False),
    }
    view = read_compact_evidence(metadata)
    assert view is not None
    assert view.source == "batch"
    assert view.success is False
    assert len(view.failed_rows) == 1
    assert view.failed_rows[0]["failure_category"] == "timeout"


def test_evidence_helper_falls_back_to_single():
    metadata = {
        "last_tool_result_compact": {
            "tool": "shell.exec",
            "summary": "echo ok",
            "success": True,
        },
    }
    view = read_compact_evidence(metadata)
    assert view is not None
    assert view.source == "single"
    assert view.success is True
    assert len(view.rows) == 1
    assert view.rows[0]["tool_id"] == "shell.exec"


def test_evidence_helper_returns_none_when_no_metadata():
    assert read_compact_evidence({}) is None
    assert read_compact_evidence({"unrelated": True}) is None


def test_single_failure_surfaces_in_failed_rows():
    metadata = {
        "last_tool_result_compact": {
            "tool": "shell.exec",
            "summary": "boom",
            "success": False,
            "failure_category": "tool_error",
        },
    }
    view = read_compact_evidence(metadata)
    assert view is not None
    assert view.success is False
    assert len(view.failed_rows) == 1
    assert view.successful_rows == ()


def test_reasoning_selection_uses_durable_ids_with_matching_runtime_rows():
    batch_id = "tb-reasoning-selection"
    register_runtime_compact_evidence(
        {
            "tool_batch_id": batch_id,
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc-transient",
                    "tool_id": "example.transient",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {"summary": "TRANSIENT_SENTINEL"},
                },
                {
                    "tool_call_id": "tc-durable",
                    "tool_id": "example.durable",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {"summary": "RAW_DURABLE_SENTINEL"},
                },
            ],
        }
    )
    metadata = {
        "tool_batch_id": batch_id,
        "last_tool_result_compact_batch": {
            "tool_batch_id": batch_id,
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc-durable",
                    "tool_id": "example.durable",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {"summary": "MASKED_DURABLE_SENTINEL"},
                }
            ],
        },
    }

    view, is_durable = select_compact_evidence_for_reasoning(metadata)

    assert view is not None
    assert is_durable is True
    assert [row["tool_call_id"] for row in view.rows] == ["tc-durable"]
    assert view.rows[0]["compact_tool_result"]["summary"] == (
        "RAW_DURABLE_SENTINEL"
    )
    assert "TRANSIENT_SENTINEL" not in str(view.raw)


def test_reasoning_selection_marks_runtime_only_evidence_non_durable():
    batch_id = "tb-runtime-only-selection"
    register_runtime_compact_evidence(
        {
            "tool_batch_id": batch_id,
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc-runtime-only",
                    "tool_id": "example.transient",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {"summary": "RUNTIME_ONLY_SENTINEL"},
                }
            ],
        }
    )

    view, is_durable = select_compact_evidence_for_reasoning(
        {"tool_batch_id": batch_id}
    )

    assert view is not None
    assert is_durable is False
    assert view.rows[0]["tool_call_id"] == "tc-runtime-only"


def test_reasoning_selection_preserves_long_terminal_session_aggregate():
    batch_id = "tb-long-terminal-session"
    running_rows = [
        {
            "tool_call_id": f"tc-running-{index}",
            "tool_id": "shell.write_stdin",
            "status": "success",
            "success": True,
            "compact_tool_result": {
                "tool": "shell.write_stdin",
                "summary": f"running step {index}",
                "stdout": f"progress-{index}\n",
                "process_status": "running",
                "session_status": "active",
                "session_id": "shs-long",
            },
        }
        for index in range(12)
    ]
    terminal_row = {
        "tool_call_id": "tc-terminal",
        "tool_id": "shell.write_stdin",
        "status": "success",
        "success": True,
        "compact_tool_result": {
            "tool": "shell.write_stdin",
            "summary": "Calculator returned all requested values.",
            "stdout": "7\n42\n50\n50\n",
            "input": "quit\n",
            "process_status": "completed",
            "session_status": "closed",
            "session_id": None,
            "exit_code": 0,
        },
    }
    register_runtime_compact_evidence(
        {
            "tool_batch_id": batch_id,
            "execution_session_aggregate": True,
            "status": "completed",
            "success": True,
            "results": [*running_rows, terminal_row],
            "deferred_followups": [],
        },
        single_compact=terminal_row["compact_tool_result"],
    )

    view, is_durable = select_compact_evidence_for_reasoning(
        {"tool_batch_id": batch_id}
    )

    assert view is not None
    assert is_durable is False
    expected_call_ids = [
        *(f"tc-running-{index}" for index in range(12)),
        "tc-terminal",
    ]
    assert [row["tool_call_id"] for row in view.rows] == expected_call_ids
    assert [row["tool_call_id"] for row in view.raw["results"]] == expected_call_ids
    assert {
        row["compact_tool_result"]["session_id"] for row in view.rows[:-1]
    } == {"shs-long"}
    terminal = view.rows[-1]["compact_tool_result"]
    assert terminal["process_status"] == "completed"
    assert terminal["session_status"] == "closed"
    assert terminal["exit_code"] == 0
    assert terminal["input"] == "quit\n"
    assert terminal["stdout"] == "7\n42\n50\n50\n"


def test_reasoning_selection_keeps_ordinary_batch_rows_and_failure_semantics():
    metadata = {"last_tool_result_compact_batch": _batch_metadata(success=False)}

    view, is_durable = select_compact_evidence_for_reasoning(metadata)

    assert view is not None
    assert is_durable is True
    assert [row["tool_call_id"] for row in view.rows] == ["tc-1", "tc-2"]
    assert view.status == "completed_with_errors"
    assert view.success is False
    assert len(view.failed_rows) == 1
