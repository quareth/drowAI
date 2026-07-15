"""Tests for: Progress Tracking Schema Extension.

Tests the new TodoProgress model and progress tracking fields
added to PostToolReasoningOutput."""

import pytest
from pydantic import ValidationError

from core.llm.structured_schemas import POST_TOOL_DECISION_STRUCTURED_OUTPUT

from agent.graph.nodes.post_tool_reasoning import (
    PostToolReasoningOutput,
    PostToolReasoningDecisionOutput,
    map_decision_output_to_post_tool_reasoning_output,
    TodoProgress,
    ToolIntent,
    VALID_TODO_STATUSES,
    _parse_reasoning_response,
)


# -----------------------------------------------------------------------------
# TodoProgress Model Tests
# -----------------------------------------------------------------------------


class TestTodoProgress:
    """Tests for the TodoProgress model."""
    
    def test_valid_progress_parses(self):
        """Valid todo progress should parse correctly."""
        progress = TodoProgress(
            index=0,
            status="completed",
            completion_type="positive",
            completion_reason="Found open ports via nmap scan"
        )
        
        assert progress.index == 0
        assert progress.status == "completed"
        assert progress.completion_reason == "Found open ports via nmap scan"
    
    def test_valid_progress_without_reason(self):
        """Progress without completion_reason should parse (it's optional)."""
        progress = TodoProgress(
            index=2,
            status="in_progress"
        )
        
        assert progress.index == 2
        assert progress.status == "in_progress"
        assert progress.completion_reason is None
    
    def test_all_valid_statuses(self):
        """All valid statuses should be accepted."""
        for status in VALID_TODO_STATUSES:
            kwargs = {"index": 0, "status": status}
            if status == "completed":
                kwargs["completion_type"] = "positive"
                kwargs["completion_reason"] = "Objective resolved with affirmative evidence"
            if status == "skipped":
                kwargs["completion_reason"] = "No longer required due to alternate path"
            progress = TodoProgress(**kwargs)
            assert progress.status == status
    
    def test_invalid_index_negative_rejected(self):
        """Negative index should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            TodoProgress(index=-1, status="pending")
        
        errors = exc_info.value.errors()
        assert any("index" in str(e["loc"]) for e in errors)
    
    def test_invalid_status_rejected(self):
        """Invalid status value should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            TodoProgress(index=0, status="invalid_status")
        
        errors = exc_info.value.errors()
        assert any("status" in str(e["loc"]) for e in errors)
    
    def test_missing_required_fields_rejected(self):
        """Missing required fields should raise ValidationError."""
        with pytest.raises(ValidationError):
            TodoProgress(index=0)  # Missing status
        
        with pytest.raises(ValidationError):
            TodoProgress(status="pending")  # Missing index


# -----------------------------------------------------------------------------
# PostToolReasoningOutput Progress Fields Tests
# -----------------------------------------------------------------------------


class TestPostToolReasoningOutputProgress:
    """Tests for progress tracking fields in PostToolReasoningOutput."""
    
    def test_with_progress_fields_parses(self):
        """Output with new progress fields should parse correctly."""
        output = PostToolReasoningOutput(
            observation="I found open ports on the target host.",
            next_action="finalize",
            action_reasoning="Goal achieved - port scan complete",
            user_goal_achieved=True,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Host found",
                ),
                TodoProgress(
                    index=1,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Ports scanned",
                ),
            ],
            effective_next_goal=None,
        )
        
        assert output.user_goal_achieved is True
        assert len(output.todo_progress) == 2
        assert output.todo_progress[0].status == "completed"
        assert output.effective_next_goal is None
    
    def test_without_progress_fields_parses(self):
        """Output without progress fields should parse (backward compatibility)."""
        output = PostToolReasoningOutput(
            observation="Running initial port scan on target.",
            next_action="call_tool",
            action_reasoning="Need to scan ports",
            tool_intent=ToolIntent(description="Scan ports", target="127.0.0.1"),
        )
        
        # Defaults should be applied
        assert output.user_goal_achieved is False
        assert output.todo_progress == []
        assert output.effective_next_goal is None
    
    def test_user_goal_achieved_default_false(self):
        """user_goal_achieved should default to False."""
        output = PostToolReasoningOutput(
            observation="Found some interesting data.",
            next_action="think_more",
            action_reasoning="Need to analyze further",
        )
        
        assert output.user_goal_achieved is False
    
    def test_todo_progress_default_empty(self):
        """todo_progress should default to empty list."""
        output = PostToolReasoningOutput(
            observation="Starting scan.",
            next_action="call_tool",
            action_reasoning="Initial scan needed",
            tool_intent=ToolIntent(description="Port scan"),
        )
        
        assert output.todo_progress == []
    
    def test_effective_next_goal_optional(self):
        """effective_next_goal should be optional."""
        output = PostToolReasoningOutput(
            observation="Completed host discovery.",
            next_action="call_tool",
            action_reasoning="Moving to port scanning",
            effective_next_goal="Scan open ports on discovered hosts",
            tool_intent=ToolIntent(description="Port scan"),
        )
        
        assert output.effective_next_goal == "Scan open ports on discovered hosts"


class TestPostToolDecisionContract:
    """Tests for the new decision-only contract."""

    def test_decision_structured_output_constant_exists(self):
        """Structured-output schema should include expected decision fields."""
        schema = POST_TOOL_DECISION_STRUCTURED_OUTPUT.schema

        assert POST_TOOL_DECISION_STRUCTURED_OUTPUT.name == "post_tool_decision"
        assert schema["type"] == "object"
        props = schema["properties"]
        required = set(schema.get("required", []))

        assert set(props.keys()) >= {
            "next_action",
            "action_reasoning",
            "tool_intent",
            "user_goal_achieved",
            "todo_progress",
            "effective_next_goal",
            "failure_detected",
            "failure_category",
            "retry_suggested",
            "candidate_observations",
        }
        assert required == {
            "next_action",
            "action_reasoning",
            "tool_intent",
            "user_goal_achieved",
            "todo_progress",
            "effective_next_goal",
            "failure_detected",
            "failure_category",
            "retry_suggested",
            "candidate_observations",
        }

    def test_decision_output_model_maps_to_runtime_output(self):
        """Decision payload should map to full runtime contract with observation."""
        decision = PostToolReasoningDecisionOutput(
            next_action="call_tool",
            action_reasoning="Need extra context",
            tool_intent=ToolIntent(description="Service scan"),
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Host found",
                )
            ],
            effective_next_goal="Scan discovered host",
            failure_detected=True,
            failure_category="network_error",
            retry_suggested=True,
            candidate_observations=[
                {
                    "observation_type": "finding.vulnerability_detected",
                    "subject_type": "finding.instance",
                    "subject_key_hint": "cve-2024-0001:service.socket:10.0.0.5/tcp/5432",
                    "assertion_level": "candidate",
                    "confidence": 0.87,
                    "attributes": [{"key": "version", "value": "11.5"}],
                    "rationale": "Version string indicates likely vulnerable release.",
                    "evidence_refs": [
                        {
                            "source_artifact_id": "artifact-1",
                            "excerpt": "PostgreSQL 11.5",
                        }
                    ],
                    "vulnerability_confidence": 0.91,
                }
            ],
        )

        mapped = map_decision_output_to_post_tool_reasoning_output(
            decision,
            observation="Tool results indicate open services.",
        )

        assert isinstance(mapped, PostToolReasoningOutput)
        assert mapped.observation == "Tool results indicate open services."
        assert mapped.next_action == "call_tool"
        assert mapped.tool_intent is not None
        assert mapped.user_goal_achieved is False
        assert mapped.failure_detected is True
        assert len(decision.candidate_observations or []) == 1

    def test_decision_model_requires_action_reasoning(self):
        """Decision model should require action_reasoning."""
        with pytest.raises(ValidationError):
            PostToolReasoningDecisionOutput(next_action="finalize")

    def test_decision_model_rejects_candidate_without_evidence_identifier(self):
        """Candidate rows must include evidence_archive_id or source_artifact_id."""
        with pytest.raises(ValidationError):
            PostToolReasoningDecisionOutput(
                next_action="reflect",
                action_reasoning="Need to reason further",
                candidate_observations=[
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key_hint": "candidate-key",
                        "assertion_level": "candidate",
                        "confidence": 0.7,
                        "attributes": [],
                        "rationale": "Candidate with invalid evidence references",
                        "evidence_refs": [{"excerpt": "missing identifier"}],
                        "vulnerability_confidence": 0.8,
                    }
                ],
            )


# -----------------------------------------------------------------------------
# Validation Logic Tests
# -----------------------------------------------------------------------------


class TestProgressValidation:
    """Tests for validation logic in _parse_reasoning_response."""
    
    def test_goal_achieved_with_finalize_unchanged(self):
        """user_goal_achieved=True with finalize action should be unchanged."""
        response = """
I have successfully completed all tasks.
===DECISION===
{
    "next_action": "finalize",
    "action_reasoning": "All goals achieved",
    "user_goal_achieved": true,
    "todo_progress": [{"index": 0, "status": "completed", "completion_type": "positive", "completion_reason": "Done"}]
}
"""
        output = _parse_reasoning_response(response)
        
        assert output.user_goal_achieved is True
        assert output.next_action == "finalize"
    
    def test_goal_achieved_with_call_tool_overridden_to_finalize(self):
        """user_goal_achieved=True with call_tool should be overridden to finalize."""
        response = """
I have completed the user's request but I'll try one more scan.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Try one more scan",
    "user_goal_achieved": true,
    "tool_intent": {"description": "Extra scan"}
}
"""
        output = _parse_reasoning_response(response)
        
        # Should be overridden to finalize
        assert output.user_goal_achieved is True
        assert output.next_action == "finalize"
        assert "(Override: goal achieved)" in output.action_reasoning
    
    def test_goal_not_achieved_with_call_tool_unchanged(self):
        """user_goal_achieved=False with call_tool should be unchanged."""
        response = """
Still need to complete the port scan.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Port scan needed",
    "user_goal_achieved": false,
    "tool_intent": {"description": "Port scan"}
}
"""
        output = _parse_reasoning_response(response)
        
        assert output.user_goal_achieved is False
        assert output.next_action == "call_tool"
    
    def test_goal_achieved_default_false_when_missing(self):
        """user_goal_achieved should default to False when not in response."""
        response = """
Running the next scan.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Continue scanning",
    "tool_intent": {"description": "Continue"}
}
"""
        output = _parse_reasoning_response(response)
        
        assert output.user_goal_achieved is False
    
    def test_todo_progress_parsed_correctly(self):
        """todo_progress array should be parsed correctly."""
        response = """
Completed the first two tasks.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Moving to next phase",
    "user_goal_achieved": false,
    "todo_progress": [
        {"index": 0, "status": "completed", "completion_type": "positive", "completion_reason": "Host discovery done"},
        {"index": 1, "status": "skipped", "completion_reason": "Not needed - used fallback"}
    ],
    "effective_next_goal": "Scan ports on fallback host",
    "tool_intent": {"description": "Port scan"}
}
"""
        output = _parse_reasoning_response(response)
        
        assert len(output.todo_progress) == 2
        assert output.todo_progress[0].index == 0
        assert output.todo_progress[0].status == "completed"
        assert output.todo_progress[0].completion_type == "positive"
        assert output.todo_progress[1].status == "skipped"
        assert output.effective_next_goal == "Scan ports on fallback host"
    
    def test_empty_todo_progress_accepted(self):
        """Empty todo_progress should be accepted."""
        response = """
Initial observation.
===DECISION===
{
    "next_action": "think_more",
    "action_reasoning": "Need to think",
    "todo_progress": []
}
"""
        output = _parse_reasoning_response(response)
        
        assert output.todo_progress == []

    def test_failure_category_empty_string_treated_as_none(self):
        """Empty failure_category should be normalized to None (no validation error)."""
        response = """
Observation text for failure normalization.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Need to read artifact",
    "failure_detected": false,
    "failure_category": "",
    "tool_intent": {"description": "Read gobuster artifact"}
}
"""
        output = _parse_reasoning_response(response)

        assert output.failure_category is None
        assert output.failure_detected is False


# -----------------------------------------------------------------------------
# Backward Compatibility Tests
# -----------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests ensuring backward compatibility with existing code."""
    
    def test_old_format_without_progress_fields_works(self):
        """Old format without any progress fields should still work."""
        response = """
I found port 22 open with SSH.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Need service detection",
    "tool_intent": {"description": "Run service detection on port 22", "target": "127.0.0.1:22"}
}
"""
        output = _parse_reasoning_response(response)
        
        assert output.observation.startswith("Decision: call_tool")
        assert output.next_action == "call_tool"
        assert output.user_goal_achieved is False
        assert output.todo_progress == []
        assert output.effective_next_goal is None
    
    def test_extra_fields_ignored(self):
        """Extra fields not in schema should be ignored (extra='ignore')."""
        response = """
Some observation text here.
===DECISION===
{
    "next_action": "finalize",
    "action_reasoning": "Task is complete",
    "some_unknown_field": "should be ignored",
    "another_unknown": 123
}
"""
        # Should not raise
        output = _parse_reasoning_response(response)
        
        assert output.next_action == "finalize"
        # Unknown fields should not be accessible
        assert not hasattr(output, "some_unknown_field")

