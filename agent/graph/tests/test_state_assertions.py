"""Tests for compact-output state assertion helpers."""

from __future__ import annotations

import pytest

from agent.graph.tests._state_assertions import (
    assert_compact_envelope_present,
    assert_no_raw_tool_output_in_state,
)


def _valid_compact() -> dict:
    return {
        "schema_version": "2.0",
        "tool": "nmap",
        "status": "success",
        "success": True,
        "exit_code": 0,
        "summary": "Scan completed.",
        "key_findings": ["port 22 open"],
        "errors": [],
        "report_recommendations": ["Investigate exposed SSH service"],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "low",
        "artifact_refs": [{"path": "/workspace/artifacts/tool-output-1.txt"}],
        "compression": {"source": "deterministic"},
    }


def test_assert_no_raw_tool_output_in_state_passes_without_forbidden_keys():
    metadata = {
        "last_tool_result": {"status": "success", "success": True},
        "tool_history": [{"tool": "nmap", "status": "success"}],
        "last_tool_result_compact": _valid_compact(),
    }
    assert_no_raw_tool_output_in_state(metadata)


def test_assert_no_raw_tool_output_in_state_raises_for_last_tool_result():
    metadata = {"last_tool_result": {"stdout": "raw output should not be here"}}
    with pytest.raises(AssertionError, match="Raw tool output keys are forbidden"):
        assert_no_raw_tool_output_in_state(metadata)


def test_assert_no_raw_tool_output_in_state_raises_for_tool_history():
    metadata = {
        "tool_history": [
            {"tool": "nmap", "details": {"stderr_excerpt": "timed out"}},
        ]
    }
    with pytest.raises(AssertionError, match="Raw tool output keys are forbidden"):
        assert_no_raw_tool_output_in_state(metadata)


def test_assert_no_raw_tool_output_allows_skip_payload_in_last_tool_result():
    metadata = {
        "tool_skipped": True,
        "last_tool_result": {
            "status": "rejected",
            "stdout": "User declined tool execution.",
            "stderr": "",
        },
    }
    assert_no_raw_tool_output_in_state(metadata)


def test_assert_no_raw_tool_output_still_checks_tool_history_on_skip():
    metadata = {
        "tool_skipped": True,
        "last_tool_result": {"stdout": "allowed for skip path"},
        "tool_history": [{"result": {"stderr": "must still be rejected"}}],
    }
    with pytest.raises(AssertionError, match="Raw tool output keys are forbidden"):
        assert_no_raw_tool_output_in_state(metadata)


def test_assert_compact_envelope_present_raises_when_missing_required_fields():
    metadata = {"last_tool_result_compact": {"tool": "nmap"}}
    with pytest.raises(AssertionError, match="missing required fields"):
        assert_compact_envelope_present(metadata)


def test_assert_compact_envelope_present_passes_with_required_fields():
    metadata = {"last_tool_result_compact": _valid_compact()}
    assert_compact_envelope_present(metadata)
