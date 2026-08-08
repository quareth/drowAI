"""Tests for capability-agnostic failure detection."""

import pytest

from ..failure_detection import (
    FailureContext,
    build_failure_context_from_state,
    classify_failure_category,
    detect_failure,
)
from .....state import InteractiveState


class TestDetectFailure:
    """Tests for detect_failure function."""
    
    def test_detect_failure_from_success_flag_false(self):
        """Verify failure detected from success=False."""
        context = FailureContext(
            success_flag=False,
            status="completed",
            exit_code=0,
            stdout="output",
            stderr="",
            summary="summary",
            key_findings=["finding"],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is True
        assert category is not None
    
    def test_detect_failure_from_error_status(self):
        """Verify failure detected from error status."""
        context = FailureContext(
            success_flag=None,
            status="error",
            exit_code=1,
            stdout="",
            stderr="error occurred",
            summary="",
            key_findings=[],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is True
        assert category == "invalid_params"
    
    def test_detect_failure_from_failed_status(self):
        """Verify failure detected from failed status."""
        context = FailureContext(
            success_flag=None,
            status="failed",
            exit_code=1,
            stdout="",
            stderr="command failed",
            summary="",
            key_findings=[],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is True
        assert category == "invalid_params"
    
    def test_detect_failure_from_empty_output(self):
        """Verify failure detected from empty stdout/stderr and no synthesis."""
        context = FailureContext(
            success_flag=None,
            status="completed",
            exit_code=0,
            stdout="",
            stderr="",
            summary="",
            key_findings=[],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is True
        assert category == "empty_output"
    
    def test_no_failure_with_valid_output(self):
        """Verify no failure detected when tool succeeds with output."""
        context = FailureContext(
            success_flag=None,
            status="completed",
            exit_code=0,
            stdout="valid output",
            stderr="",
            summary="tool ran successfully",
            key_findings=["port 80 open"],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is False
        assert category is None
    
    def test_no_failure_with_synthesized_content_only(self):
        """Verify no failure when synthesis produced content."""
        context = FailureContext(
            success_flag=None,
            status="completed",
            exit_code=0,
            stdout="",
            stderr="",
            summary="Analyzed results",
            key_findings=["finding 1", "finding 2"],
        )
        
        failure, category = detect_failure(context)
        
        assert failure is False
        assert category is None


class TestClassifyFailureCategory:
    """Tests for classify_failure_category function."""
    
    def test_classify_network_error_connection_refused(self):
        """Verify network errors classified correctly (connection refused)."""
        category = classify_failure_category("connection refused by host", 1)
        assert category == "network_error"
    
    def test_classify_network_error_unreachable(self):
        """Verify network errors classified correctly (network unreachable)."""
        category = classify_failure_category("network unreachable", 1)
        assert category == "network_error"
    
    def test_classify_permission_denied(self):
        """Verify permission errors classified correctly."""
        category = classify_failure_category("permission denied", 1)
        assert category == "permission_denied"
    
    def test_classify_operation_not_permitted(self):
        """Verify operation not permitted classified as permission error."""
        category = classify_failure_category("operation not permitted", 1)
        assert category == "permission_denied"
    
    def test_classify_timeout_from_exit_code(self):
        """Verify timeout detected from exit code 124."""
        category = classify_failure_category("", 124)
        assert category == "timeout"
    
    def test_classify_timeout_from_stderr(self):
        """Verify timeout detected from stderr text."""
        category = classify_failure_category("connection timeout", 1)
        assert category == "timeout"
    
    def test_classify_tool_unavailable_not_found(self):
        """Verify tool not found classified correctly."""
        category = classify_failure_category("command not found: nmap", 127)
        assert category == "tool_unavailable"
    
    def test_classify_invalid_params(self):
        """Verify invalid parameters classified correctly."""
        category = classify_failure_category("invalid option: -xyz", 1)
        assert category == "invalid_params"
    
    def test_classify_empty_output(self):
        """Verify empty stderr classified as empty_output."""
        category = classify_failure_category("", 0)
        assert category == "empty_output"
    
    def test_classify_unknown_error(self):
        """Verify unknown errors get unknown category."""
        category = classify_failure_category("something mysterious happened", 255)
        assert category == "unknown"
    
    def test_case_insensitive_classification(self):
        """Verify classification is case-insensitive."""
        category = classify_failure_category("PERMISSION DENIED", 1)
        assert category == "permission_denied"


class TestBuildFailureContextFromState:
    """Tests for migration-safe failure context extraction."""

    def _build_state(self, metadata: dict) -> InteractiveState:
        return InteractiveState.from_mapping(
            {
                "facts": {
                    "task_id": 1,
                    "message": "test",
                    "metadata": metadata,
                },
                "trace": {"reasoning": []},
            }
        )

    def test_prefers_compact_fields_when_available(self):
        """Compact summary/errors/status/success are preferred over raw fields."""
        state = self._build_state(
            {
                "last_tool_result": {
                    "status": "success",
                    "success": True,
                    "exit_code": 0,
                    "stdout": "legacy output",
                    "stderr": "legacy stderr",
                },
                "synthesized_output": {
                    "status": "completed",
                    "success": True,
                    "summary": "legacy summary",
                    "key_findings": ["legacy finding"],
                },
                "last_tool_result_compact": {
                    "status": "failed",
                    "success": False,
                    "exit_code": 124,
                    "summary": "Scan timed out.",
                    "key_findings": ["host reachable"],
                    "errors": ["connection timeout after 10s"],
                },
            }
        )

        context = build_failure_context_from_state(state)
        failure, category = detect_failure(context)

        assert context.success_flag is False
        assert context.status == "failed"
        assert context.exit_code == 124
        assert context.summary == "Scan timed out."
        assert context.key_findings == ["host reachable"]
        assert context.stderr == "connection timeout after 10s"
        assert failure is True
        assert category == "timeout"

    def test_compact_only_context_does_not_read_raw_stderr_when_compact_missing(self):
        """Compact-only mode should not read raw stderr from last_tool_result."""
        state = self._build_state(
            {
                "last_tool_result": {
                    "status": "failed",
                    "success": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "invalid option: -xyz",
                },
                "synthesized_output": {
                    "summary": "",
                    "key_findings": [],
                },
            }
        )

        context = build_failure_context_from_state(state)
        failure, category = detect_failure(context)

        assert context.success_flag is False
        assert context.status == "failed"
        assert context.exit_code == 1
        assert context.stderr == ""
        assert failure is True
        assert category == "empty_output"

