"""Schema validation tests for HITL interrupt payload contracts."""

from backend.services.langgraph_chat.checkpoint.hitl_schemas import (
    ClarifyQuestionPayload,
    ClarifyRequestPayload,
    ClarifyResponse,
    HITLResumeResponse,
    PlanReviewPayload,
    PlanReviewResponse,
    TodoItemPayload,
    ToolApprovalPayload,
    ToolApprovalResponse,
)
import pytest
from pydantic import ValidationError


def test_tool_approval_payload_validation() -> None:
    payload = ToolApprovalPayload(
        tool_id="network.nmap",
        tool_name="Nmap Port Scanner",
        parameters={"target": "192.168.1.0/24"},
        description="Scan network for open ports",
    )
    assert payload.type == "tool_approval"


def test_tool_approval_response_edit() -> None:
    response = ToolApprovalResponse(
        action="edit",
        edited_parameters={"target": "10.0.0.0/24"},
    )
    assert response.edited_parameters is not None


def test_plan_review_payload_validation() -> None:
    payload = PlanReviewPayload(
        goal="Scan network for vulnerabilities",
        plan_steps=["Step 1: Discovery", "Step 2: Scan"],
        todo_list=[
            TodoItemPayload(id="1", text="Run nmap scan"),
            TodoItemPayload(id="2", text="Analyze results"),
        ],
    )
    assert payload.type == "plan_review"
    assert len(payload.plan_steps) == 2
    assert payload.todo_list[0].status == "pending"


def test_plan_review_response_edit() -> None:
    response = PlanReviewResponse(
        action="edit",
        edited_plan_steps=["New Step 1", "New Step 2"],
        edited_goal="Updated goal",
    )
    assert response.action == "edit"
    assert response.edited_plan_steps is not None


def test_plan_review_payload_serializable() -> None:
    payload = PlanReviewPayload(
        goal="Test",
        plan_steps=["Step 1"],
    )
    json_str = payload.model_dump_json()
    restored = PlanReviewPayload.model_validate_json(json_str)
    assert restored.goal == payload.goal


def test_clarify_request_payload_validation() -> None:
    payload = ClarifyRequestPayload(
        questions=[
            ClarifyQuestionPayload(
                question_id="target",
                input_type="select",
                label="What host should I scan?",
                options=["10.0.0.1", "10.0.0.2"],
            ),
            ClarifyQuestionPayload(
                question_id="scan_mode",
                input_type="select",
                label="Which scan mode is required?",
                options=["quick", "full"],
            ),
        ],
        context_metadata={"source": "planner"},
    )
    assert payload.type == "clarify_request"
    assert len(payload.questions) == 2


def test_clarify_question_requires_options_for_select() -> None:
    with pytest.raises(ValidationError):
        ClarifyQuestionPayload(
            question_id="scan_mode",
            input_type="select",
            label="Which scan mode is required?",
        )


def test_clarify_question_rejects_duplicate_options() -> None:
    with pytest.raises(ValidationError):
        ClarifyQuestionPayload(
            question_id="target",
            input_type="select",
            label="Choose target",
            options=["10.0.0.1", "10.0.0.1"],
        )


def test_clarify_question_rejects_text_input_type() -> None:
    with pytest.raises(ValidationError):
        ClarifyQuestionPayload(
            question_id="target",
            input_type="text",
            label="Choose target",
            options=["10.0.0.1"],
        )


def test_clarify_question_rejects_more_than_four_options() -> None:
    with pytest.raises(ValidationError):
        ClarifyQuestionPayload(
            question_id="target",
            input_type="select",
            label="Choose target",
            options=["1", "2", "3", "4", "5"],
        )


def test_clarify_question_rejects_empty_options_list() -> None:
    with pytest.raises(ValidationError):
        ClarifyQuestionPayload(
            question_id="target",
            input_type="select",
            label="Choose target",
            options=[],
        )


def test_hitl_resume_response_supports_answer_action() -> None:
    resume = HITLResumeResponse(
        action="answer",
        answers={"target": "10.0.0.1"},
        user_note="Use production-safe scope only",
    )
    assert resume.action == "answer"
    assert resume.answers == {"target": "10.0.0.1"}


def test_clarify_response_validation() -> None:
    response = ClarifyResponse(action="answer", answers={"target": "10.0.0.1"})
    assert response.action == "answer"
    assert response.answers["target"] == "10.0.0.1"
