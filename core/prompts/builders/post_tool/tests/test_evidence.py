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
