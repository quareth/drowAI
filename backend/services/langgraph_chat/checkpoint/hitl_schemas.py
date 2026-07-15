"""Human-in-the-Loop schemas for tool approval flow."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class TodoItemPayload(BaseModel):
    """Single todo item in plan review payload."""

    id: str = Field(..., description="Unique identifier for this todo")
    text: str = Field(..., description="Todo item description")
    status: Literal["pending", "in_progress", "completed", "skipped"] = "pending"


class PlanReviewPayload(BaseModel):
    """Payload surfaced to user when plan requires approval.

    Contains all information needed to render PlanCard on frontend.
    This payload is persisted by checkpointer, enabling recovery on refresh.
    """

    type: Literal["plan_review"] = "plan_review"
    goal: str = Field(..., description="First goal / primary objective")
    plan_steps: List[str] = Field(..., description="Ordered list of plan steps")
    todo_list: List[TodoItemPayload] = Field(
        default_factory=list,
        description="Actionable todo items",
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="LLM's reasoning for this plan",
    )
    targets: List[str] = Field(
        default_factory=list,
        description="Target IPs/hostnames",
    )
    run_id: Optional[int] = Field(
        default=None,
        description="Turn sequence for multi-run tracking",
    )
    plan_version: Optional[int] = Field(
        default=None,
        description="Plan version within the current run",
    )
    turn_sequence: Optional[int] = Field(
        default=None,
        description="Canonical turn sequence for resume/persistence",
    )
    turn_id: Optional[str] = Field(
        default=None,
        description="Stable turn identifier for resume/persistence",
    )
    reserved_message_id: Optional[int] = Field(
        default=None,
        description="Reserved ChatMessage id to update after resume",
    )


class PlanReviewResponse(BaseModel):
    """User's response to a plan review request."""

    action: Literal["approve", "edit", "reject"]
    edited_goal: Optional[str] = None
    edited_plan_steps: Optional[List[str]] = None
    edited_todo_list: Optional[List[str]] = None
    user_note: Optional[str] = None


class ToolApprovalItem(BaseModel):
    """Single committed call inside a multi-call batch approval surface (Phase 7 Task 7.1)."""

    tool_call_id: str = Field(
        default="", description="Stable tool_call_id from batch_commit"
    )
    tool_id: str = Field(..., description="Tool identifier (e.g., 'network.nmap')")
    tool_name: str = Field(..., description="Human-readable tool name")
    parameters: Dict[str, Any] = Field(default_factory=dict)
    description: str = Field(default="", description="What this tool call will do")
    risk_level: Optional[str] = Field(default=None, description="low/medium/high")


class ToolApprovalPayload(BaseModel):
    """Payload surfaced to user when tool execution requires approval.

    Phase 7 Task 7.1 added the ``items`` list + ``tool_batch_id`` so multi-
    call batches present every committed call in one approval context. The
    legacy single-tool fields (``tool_id`` / ``tool_name`` / ``parameters``
    / ``description`` / ``risk_level``) remain populated from ``items[0]``
    during the migration window so frontends that haven't picked up the
    batch shape still render the first call.
    """

    type: Literal["tool_approval"] = "tool_approval"
    tool_id: str = Field(..., description="Tool identifier (e.g., 'network.nmap')")
    tool_name: str = Field(..., description="Human-readable tool name")
    parameters: Dict[str, Any] = Field(default_factory=dict)
    description: str = Field(..., description="What this tool will do")
    risk_level: Optional[str] = Field(default=None, description="low/medium/high")
    estimated_duration: Optional[int] = Field(
        default=None,
        description="Estimated duration in seconds",
    )
    items: List[ToolApprovalItem] = Field(
        default_factory=list,
        description="All committed calls in this batch approval surface (Phase 7).",
    )
    tool_batch_id: str = Field(
        default="",
        description="Batch identifier the approved calls belong to (Phase 7).",
    )
    turn_sequence: Optional[int] = Field(
        default=None,
        description="Canonical turn sequence for resume/persistence",
    )
    turn_id: Optional[str] = Field(
        default=None,
        description="Stable turn identifier for resume/persistence",
    )
    reserved_message_id: Optional[int] = Field(
        default=None,
        description="Reserved ChatMessage id to update after resume",
    )


class ToolApprovalResponse(BaseModel):
    """User's response to a tool approval request."""

    action: Literal["approve", "edit", "skip"]
    edited_parameters: Optional[Dict[str, Any]] = None
    user_note: Optional[str] = None


class ToolApprovalDecision(BaseModel):
    """Per-call decision returned by the batch tool-approval surface."""

    tool_call_id: Optional[str] = None
    action: Literal["approve", "edit", "skip"]
    edited_parameters: Optional[Dict[str, Any]] = None


class ClarifyQuestionPayload(BaseModel):
    """Single clarify question emitted when required inputs are missing."""

    question_id: str = Field(..., description="Stable identifier for answer mapping")
    input_type: Literal["select"] = Field(
        default="select",
        description="Input control type for this question",
    )
    label: str = Field(..., description="User-facing question label")
    options: List[str] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="Allowed values for select input (1-4 predefined options)",
    )
    required: bool = Field(
        default=True,
        description="Whether this input must be answered before resume",
    )

    @model_validator(mode="after")
    def validate_options_for_input_type(self) -> "ClarifyQuestionPayload":
        normalized: List[str] = []
        seen: set[str] = set()
        for option in self.options:
            value = str(option).strip()
            if not value:
                raise ValueError("options must not contain empty values")
            if value in seen:
                raise ValueError("options must be unique")
            seen.add(value)
            normalized.append(value)
        self.options = normalized
        return self


class ClarifyRequestPayload(BaseModel):
    """Interrupt payload asking user for mandatory blocker inputs."""

    type: Literal["clarify_request"] = "clarify_request"
    questions: List[ClarifyQuestionPayload] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="Mandatory blocker questions requiring user input",
    )
    context_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional context used to explain why clarification is required",
    )


class ClarifyResponse(BaseModel):
    """User's response to a clarify request interrupt."""

    action: Literal["answer"] = "answer"
    answers: Dict[str, str] = Field(
        ...,
        min_length=1,
        description="Answers keyed by clarify question_id",
    )
    user_note: Optional[str] = None


class HITLResumeResponse(BaseModel):
    """Combined response type for resuming any HITL interrupt.

    This unified schema supports both tool approval and plan review responses,
    allowing the backend to accept all possible fields without dropping them.
    """

    action: Literal["approve", "edit", "skip", "reject", "answer"]
    # Tool approval fields
    edited_parameters: Optional[Dict[str, Any]] = None
    tool_batch_id: Optional[str] = None
    decisions: Optional[List[ToolApprovalDecision]] = None
    # Plan review fields
    edited_goal: Optional[str] = None
    edited_plan_steps: Optional[List[str]] = None
    edited_todo_list: Optional[List[str]] = None
    # Clarify request fields
    answers: Optional[Dict[str, str]] = None
    # Shared
    user_note: Optional[str] = None


__all__ = [
    "ClarifyQuestionPayload",
    "ClarifyRequestPayload",
    "ClarifyResponse",
    "HITLResumeResponse",
    "PlanReviewPayload",
    "PlanReviewResponse",
    "TodoItemPayload",
    "ToolApprovalItem",
    "ToolApprovalDecision",
    "ToolApprovalPayload",
    "ToolApprovalResponse",
]
