"""Pydantic models for post-tool reasoning output.

This module defines PTR-SPECIFIC models only. For shared models
(TodoItem, TodoStatus, CompletionType), import from agent.graph.state.

The models define the structured output schema for the post-tool reasoning
LLM call, which combines observation articulation with decision making.

Example:
    from agent.graph.state import TodoItem, TodoStatus  # Shared models
    from .models import PostToolReasoningOutput         # PTR-specific
"""

from __future__ import annotations

import re
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =============================================================================
# IMPORTANT: Do NOT duplicate these models - they exist in agent.graph.state:
# - TodoStatus (enum)
# - CompletionType (enum)
# - TodoItem (BaseModel)
# - AgentPauseRequest (BaseModel)
#
# This module ONLY contains PTR-specific models.
# =============================================================================


class PostToolReasoningError(Exception):
    """Raised when post-tool reasoning fails.
    
    This exception is raised for:
    - LLM call failures
    - Response parsing errors
    - Invalid action values
    
    The exception message should contain details about what failed.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retryable: bool = False,
        retry_mode: str | None = None,
        diagnostics: dict[str, object] | None = None,
        graph_name: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.retryable = retryable
        self.retry_mode = retry_mode
        self.diagnostics = diagnostics or {}
        self.graph_name = graph_name
        super().__init__(message)


class RetryablePostToolReasoningError(PostToolReasoningError):
    """Raised when post-tool reasoning can safely be retried from checkpoint."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retry_mode: str = "checkpoint",
        diagnostics: dict[str, object] | None = None,
        graph_name: str | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code=error_code,
            retryable=True,
            retry_mode=retry_mode,
            diagnostics=diagnostics,
            graph_name=graph_name,
        )


class ToolIntent(BaseModel):
    """Structured description of what tool action to perform next.
    
    This is ONLY populated when next_action is "call_tool".
    The LLM describes what it wants to do in a tool-agnostic way,
    and the parameter generator uses this to select appropriate tools/params.
    
    Attributes:
        description: Natural language description of what you want to accomplish.
            Example: 'Perform version detection on the PostgreSQL service'
        target: The specific target for this action (IP, hostname, URL, file path).
            Example: '127.0.0.1:5432' or 'example.com'
        focus: What aspect to focus on (service name, port, protocol, vulnerability type).
            Example: 'PostgreSQL service' or 'open ports'
    """
    
    description: str = Field(
        ...,
        description=(
            "Natural language description of what you want to accomplish. "
            "Example: 'Perform version detection on the PostgreSQL service to identify exact version and configuration'"
        ),
    )
    target: Optional[str] = Field(
        None,
        description=(
            "The specific target for this action (IP, hostname, URL, file path, etc). "
            "Example: '127.0.0.1:5432' or 'example.com' or '/etc/passwd'"
        ),
    )
    focus: Optional[str] = Field(
        None,
        description=(
            "What aspect to focus on (service name, port, protocol, vulnerability type). "
            "Example: 'PostgreSQL service' or 'open ports' or 'web directories'"
        ),
    )


class TodoProgress(BaseModel):
    """Progress status for a single todo item.
    
    Used by the LLM to report which todos have changed status during
    the current reasoning iteration. This enables tracking task completion
    without a separate completion checker.
    
    NOTE: This is different from TodoItem in agent.graph.state:
    - TodoItem: Full todo state with history (in state.py)
    - TodoProgress: LLM's assessment of status change (PTR-specific)
    
    The status values map to TodoStatus in state.py:
    - "pending" → TodoStatus.PENDING
    - "in_progress" → TodoStatus.IN_PROGRESS
    - "completed" + completion_type="positive" → TodoStatus.COMPLETE_POSITIVE
    - "completed" + completion_type="negative" → TodoStatus.COMPLETE_NEGATIVE
    - "skipped" → TodoStatus.COMPLETE_NEGATIVE
    
    Attributes:
        index: Zero-based index of the todo item in the todo list.
        status: Current status after this iteration.
        completion_type: REQUIRED when status is completed. Distinguishes
            positive evidence ("found/open/present") from negative evidence
            ("closed/not found/not present").
        completion_reason: REQUIRED when status is completed or skipped.
            Must provide concrete terminal evidence.
    """
    
    index: int = Field(
        ...,
        ge=0,
        description="Zero-based index of the todo item in the todo list"
    )
    status: Literal["pending", "in_progress", "completed", "skipped"] = Field(
        ...,
        description=(
            "Current status of this todo. "
            "pending: Not started. "
            "in_progress: Currently working on. "
            "completed: Objective resolved (positive OR negative outcome). "
            "skipped: No longer needed (alternative path taken or superseded)."
        )
    )
    completion_type: Optional[Literal["positive", "negative"]] = Field(
        None,
        description=(
            "REQUIRED when status is 'completed'. "
            "positive: objective resolved with affirmative finding. "
            "negative: objective resolved with a definitive negative finding."
        ),
    )
    completion_reason: Optional[str] = Field(
        None,
        description=(
            "Required when status is completed or skipped. "
            "Must provide concrete terminal evidence. "
            "Example: 'port 5432 verified closed on selected host' or "
            "'Skipped - fallback host completed the same objective'"
        )
    )

    @model_validator(mode="after")
    def _validate_terminal_completion_fields(self) -> "TodoProgress":
        if self.status == "completed":
            if self.completion_type not in {"positive", "negative"}:
                raise ValueError(
                    "completion_type must be 'positive' or 'negative' when status is 'completed'"
                )
            if not (self.completion_reason or "").strip():
                raise ValueError(
                    "completion_reason is required when status is 'completed'"
                )
        elif self.status == "skipped":
            if not (self.completion_reason or "").strip():
                raise ValueError(
                    "completion_reason is required when status is 'skipped'"
                )
        return self


_VULNERABILITY_OBSERVATION_PATTERN = re.compile(r"^finding\.vulnerability(?:[._]|$)")


class CandidateAttribute(BaseModel):
    """Key/value attribute emitted with one candidate observation."""

    key: str = Field(..., min_length=1)
    value: str = Field(default="")


class CandidateEvidenceRef(BaseModel):
    """Evidence reference for post-tool candidate rows.

    Evidence may point to a durable archive id (replay path) or to
    source artifact id from compact artifact refs (live path).
    """

    excerpt: str = Field(..., min_length=1)
    evidence_archive_id: Optional[str] = Field(default=None, min_length=1)
    source_artifact_id: Optional[str] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_reference_identity(self) -> "CandidateEvidenceRef":
        if not (self.evidence_archive_id or self.source_artifact_id):
            raise ValueError(
                "candidate evidence refs require evidence_archive_id or source_artifact_id"
            )
        return self


class CandidateVulnerability(BaseModel):
    """Optional vulnerability metadata for vulnerability candidate rows."""

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    severity: str = Field(..., min_length=1)


class CandidateObservation(BaseModel):
    """Structured candidate observation emitted by post-tool decision call."""

    observation_type: str = Field(..., min_length=3)
    subject_type: str = Field(..., min_length=3)
    subject_key_hint: str = Field(..., min_length=1)
    assertion_level: Literal["candidate"] = Field(
        ...,
        description="Candidate-only observation rows are expected in this payload.",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    attributes: List[CandidateAttribute] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1)
    evidence_refs: List[CandidateEvidenceRef] = Field(default_factory=list, min_length=1)
    vulnerability: Optional[CandidateVulnerability] = None
    vulnerability_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_vulnerability_fields(self) -> "CandidateObservation":
        is_vulnerability = bool(
            _VULNERABILITY_OBSERVATION_PATTERN.search(str(self.observation_type or "").strip())
        )
        if is_vulnerability:
            return self
        if self.vulnerability is not None or self.vulnerability_confidence is not None:
            raise ValueError(
                "vulnerability fields are only allowed for finding.vulnerability* observation types"
            )
        return self

    def to_payload_dict(self) -> dict[str, Any]:
        """Return stable payload dict shape for ingestion mapping."""
        return self.model_dump(exclude_none=True)


class PostToolReasoningOutput(BaseModel):
    """Structured output from post-tool reasoning LLM call.

    This model ensures the LLM provides both an observation (for streaming
    to frontend) and a decision (for graph routing) in a single coherent response.

    The key guarantee: what the LLM says it will do in the observation
    is the same as what actually happens next (because both come from
    the same response).

    Attributes:
        observation: 4-5 sentence first-person observation about tool results,
            including what was found, how it relates to prior context, and
            what the agent plans to do next.
        next_action: The decided next action based on the observation.
            Must be one of: call_tool, think_more, reflect, finalize.
        action_reasoning: Brief explanation linking the observation to the
            action choice. This helps with debugging and transparency.
        tool_intent: When next_action is "call_tool", describes what tool
            action to perform. This is used by downstream parameter generation.
        user_goal_achieved: True if user's original request is fully satisfied.
        todo_progress: Progress updates for todos that changed status.
        effective_next_goal: Updated goal for next iteration.
    """
    
    observation: str = Field(
        ...,
        min_length=10,
        description=(
            "4-5 sentence first-person observation about tool results. "
            "Include what was found, how it relates to prior context, "
            "any hypotheses or contradictions, and what you intend to do next."
        ),
    )
    next_action: Literal["call_tool", "think_more", "reflect", "finalize"] = Field(
        ...,
        description=(
            "The decided next action based on observation. "
            "call_tool: Execute another tool to gather more data. "
            "think_more: Reason further about current findings. "
            "reflect: Step back and reconsider approach. "
            "finalize: Conclude the reasoning loop with findings."
        ),
    )
    action_reasoning: str = Field(
        ...,
        min_length=5,
        description=(
            "Brief explanation linking observation to action choice. "
            "Why does this action logically follow from what was observed?"
        ),
    )
    tool_intent: Optional[ToolIntent] = Field(
        None,
        description=(
            "REQUIRED when next_action is 'call_tool'. "
            "Describes what you want to accomplish with the next tool. "
            "This drives parameter generation - be specific about target and focus."
        ),
    )
    
    # Progress tracking fields (LLM-driven completion assessment)
    user_goal_achieved: bool = Field(
        False,
        description=(
            "True if the user's ORIGINAL request is fully satisfied. "
            "When true, agent should finalize. Consider fallback paths as valid completion. "
            "If fallback was triggered AND completed successfully, set to true."
        ),
    )
    todo_progress: List[TodoProgress] = Field(
        default_factory=list,
        description=(
            "Progress updates for todo items that CHANGED STATUS this iteration. "
            "Include items that were completed, skipped, or started. "
            "A todo can be completed by alternative means (e.g., nmap achieved what ip addr would have)."
        ),
    )
    effective_next_goal: Optional[str] = Field(
        None,
        description=(
            "What we're now working toward. Updates current_goal for next iteration. "
            "Set when advancing to a new phase of the task (e.g., from host discovery to port scanning)."
        ),
    )
    
    # Failure detection and retry guidance fields
    failure_detected: bool = Field(
        False,
        description=(
            "True if tool execution failed (exit code non-zero, empty output, or error status)"
        ),
    )
    failure_category: Optional[
        Literal[
            "network_error",
            "permission_denied",
            "timeout",
            "invalid_params",
            "tool_unavailable",
            "empty_output",
            "unknown",
        ]
    ] = Field(
        default=None,
        description="Classification of failure type when failure_detected is True",
    )
    retry_suggested: bool = Field(
        False,
        description="True if retry is recommended based on failure analysis",
    )
    candidate_observations: Optional[List[CandidateObservation]] = Field(
        default=None,
        description=(
            "Optional candidate observations for compatibility with decision-first "
            "flows and legacy tests that return full output payloads."
        ),
    )

    @field_validator("todo_progress", mode="before")
    @classmethod
    def _normalize_todo_progress(cls, value: Any) -> Any:
        """Normalize nullable todo_progress payloads to an empty list."""
        if value is None:
            return []
        return value

    model_config = ConfigDict(extra="ignore")


class PostToolReasoningDecisionOutput(BaseModel):
    """Decision-only post-tool reasoning payload (no observation text).

    Used for the first structured LLM call in Phase 1/2 split.
    Observation content is populated in a separate call and merged by
    the post-tool reasoning node.
    """

    next_action: Literal["call_tool", "think_more", "reflect", "finalize"] = Field(
        ...,
        description=(
            "Decides the next action after reviewing tool output. "
            "call_tool: Execute another tool call. "
            "think_more: Continue reasoning without a tool. "
            "reflect: Replan or revise the current approach. "
            "finalize: Conclude the reasoning cycle."
        ),
    )
    action_reasoning: str = Field(
        ...,
        min_length=5,
        description=(
            "Brief explanation linking observed evidence to the chosen next action."
        ),
    )
    tool_intent: Optional[ToolIntent] = Field(
        None,
        description=(
            "Required when next_action is 'call_tool'. "
            "Describes tool intent in a generic, tool-agnostic way."
        ),
    )
    user_goal_achieved: bool = Field(
        False,
        description="True when the user goal is fully satisfied.",
    )
    todo_progress: List[TodoProgress] = Field(
        default_factory=list,
        description=(
            "Todo updates for items that changed status in this reasoning step."
        ),
    )
    effective_next_goal: Optional[str] = Field(
        None,
        description=(
            "Optional next phase goal to replace the current goal in state."
        ),
    )
    failure_detected: bool = Field(
        False,
        description="True when tool execution failed or produced unusable results.",
    )
    failure_category: Optional[
        Literal[
            "network_error",
            "permission_denied",
            "timeout",
            "invalid_params",
            "tool_unavailable",
            "empty_output",
            "unknown",
        ]
    ] = Field(
        default=None,
        description="Failure classification when failure_detected is True.",
    )
    retry_suggested: bool = Field(
        False,
        description="Whether retrying the current path is advised.",
    )
    candidate_observations: Optional[List[CandidateObservation]] = Field(
        default=None,
        description=(
            "Optional candidate observations derived in post-tool reasoning. "
            "When provided, these become the durable candidate source during ingestion."
        ),
    )

    model_config = ConfigDict(extra="ignore")

    @field_validator("todo_progress", mode="before")
    @classmethod
    def _normalize_todo_progress(cls, value: Any) -> Any:
        """Normalize nullable todo_progress payloads to an empty list."""
        if value is None:
            return []
        return value


def map_decision_output_to_post_tool_reasoning_output(
    decision_output: PostToolReasoningDecisionOutput,
    *,
    observation: str,
) -> PostToolReasoningOutput:
    """Merge decision payload with observation into runtime output contract.

    This keeps downstream callsites unchanged while the decision and
    observation are produced via separate LLM calls.
    """

    payload = decision_output.model_dump()
    payload["observation"] = observation
    return PostToolReasoningOutput.model_validate(payload)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "PostToolReasoningError",
    "RetryablePostToolReasoningError",
    "ToolIntent",
    "PostToolReasoningDecisionOutput",
    "TodoProgress",
    "PostToolReasoningOutput",
    "CandidateObservation",
    "CandidateEvidenceRef",
    "CandidateAttribute",
    "CandidateVulnerability",
    "map_decision_output_to_post_tool_reasoning_output",
]






