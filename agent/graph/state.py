"""Typed state models used by the LangGraph integration.

Iteration memory contract (runtime-owned)
-----------------------------------------
Post-tool reasoning (PTR) continuity reuses the *existing* transcript turn
system as the only turn-authoritative surface; this module does not introduce
a second turn model. Within a single active ``turn_sequence``, runtime code
maintains an ordered current-turn phase ledger under
``metadata["working_memory"]``:

- ``working_memory.current_turn_phases`` - ordered list of
  :class:`~agent.graph.utils.iteration_memory.IterationMemoryRecord` entries
  containing phase-section snapshots (authoritative current-turn phase ledger
  for PTR continuity across ``tool``, ``ptr``, ``think_more``, and
  ``reflect`` phase records). Each record stores PTR-facing section content,
  not semantic summary fields.
- ``working_memory.current_turn_phase_counter`` - the single authoritative
  per-turn phase counter; never duplicated by a prompt-local counter.
- ``working_memory.current_turn_phase_turn`` - the last ``turn_sequence`` the
  counter was scoped to; used for turn-boundary reset only.

Term definitions (see Phase 1 of the PTR iteration-memory plan):

- ``turn_sequence`` - canonical user-turn ordinal, reused unchanged from the
  existing transcript system; runtime stamps it and PTR must never invent it.
- ``phase_sequence`` - ordered event index *within* the active ``turn_sequence``
  used only for PTR continuity; runtime stamps it (never PTR).

:class:`TraceState.observations` is intentionally kept as ``List[str]`` in this
iteration. The current-turn phase ledger lives in working memory (inside
metadata), not in ``trace.observations``, so existing prose-observation
consumers remain unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Mapping, MutableMapping, Set, Union

from pydantic import BaseModel, Field

from agent.graph.utils.iteration_memory import (
    IterationMemoryRecord,
)


class TodoStatus(str, Enum):
    """Status of a todo item in the task list.
    
    Tracks progression from pending through in-progress to various completion states.
    Distinguishes between positive outcomes (found what we were looking for),
    negative outcomes (thoroughly searched, nothing found), and exhaustion
    (guardrails triggered without completion).
    """
    
    PENDING = "pending"
    """Todo has not been started yet."""
    
    IN_PROGRESS = "in_progress"
    """Todo is currently being worked on."""
    
    COMPLETE_POSITIVE = "complete_positive"
    """Todo completed successfully with positive findings (found target)."""
    
    COMPLETE_NEGATIVE = "complete_negative"
    """Todo completed with negative result (thoroughly searched, nothing found)."""
    
    EXHAUSTED = "exhausted"
    """Todo exhausted guardrails (max attempts/time/actions) without completion."""


class CompletionType(str, Enum):
    """Type of completion for a todo item.
    
    Used by the LLM-based completion checker to classify how a todo was completed.
    Enables proper handling of negative results and distinguishes between
    successful completion and guardrail exhaustion.
    """
    
    POSITIVE = "positive"
    """Successfully found what we were looking for (e.g., vulnerabilities discovered)."""
    
    NEGATIVE = "negative"
    """Thoroughly searched using multiple methods, nothing found (valid completion)."""
    
    INCOMPLETE = "incomplete"
    """Not yet complete, more work needed (continue working on this todo)."""
    
    EXHAUSTED = "exhausted"
    """Hit guardrails (max attempts/time/actions) before completion."""


class AgentPauseRequest(BaseModel):
    """Request for user confirmation before continuing agent execution.
    
    Used when agent determines it should pause and ask for user approval
    to continue (e.g., many todos remaining, context getting long, risky actions).
    
    Attributes:
        reason: Machine-readable pause reason (e.g., "many_todos_remaining", "risky_action")
        current_progress: Summary of progress so far (completed todos, findings, etc.)
        remaining_todos: List of remaining todo descriptions
        question: Human-readable question to ask user
        estimated_time: Estimated additional time in seconds (if known)
        estimated_tool_calls: Estimated additional tool calls (if known)
        pause_timestamp: When pause request was created
    """
    
    reason: str = Field(
        ...,
        description="Machine-readable pause reason"
    )
    current_progress: Dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of progress (completed todos, findings, metrics)"
    )
    remaining_todos: List[str] = Field(
        default_factory=list,
        description="Descriptions of remaining todos"
    )
    question: str = Field(
        ...,
        description="Human-readable question to ask user"
    )
    estimated_time: Optional[int] = Field(
        default=None,
        description="Estimated additional time in seconds"
    )
    estimated_tool_calls: Optional[int] = Field(
        default=None,
        description="Estimated additional tool calls"
    )
    pause_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When pause request was created"
    )


class TodoItem(BaseModel):
    """Structured todo item with completion tracking and metadata.
    
    Tracks attempts, results, and completion reasoning to enable
    LLM-based completion assessment and provide audit trails for
    agent decision-making.
    
    Attributes:
        description: Human-readable description of what needs to be done
        status: Current status (pending, in_progress, complete_*, exhausted)
        attempts: Number of attempts made to complete this todo
        actions_taken: List of actions/tools executed for this todo
        results: List of results/observations from actions
        started_at: Timestamp when todo moved to in_progress
        completed_at: Timestamp when todo was marked complete
        completion_type: How todo was completed (positive/negative/exhausted)
        completion_reasoning: LLM explanation of why todo is complete
    """
    
    description: str = Field(..., description="What needs to be accomplished")
    status: TodoStatus = Field(
        default=TodoStatus.PENDING,
        description="Current status of the todo"
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description="Number of attempts made"
    )
    actions_taken: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Actions/tools executed for this todo"
    )
    results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Results/observations from actions"
    )
    started_at: Optional[datetime] = Field(
        default=None,
        description="When todo moved to in_progress"
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="When todo was marked complete"
    )
    completion_type: Optional[CompletionType] = Field(
        default=None,
        description="How todo was completed"
    )
    completion_reasoning: Optional[str] = Field(
        default=None,
        description="LLM explanation of completion decision"
    )
    
    def add_attempt(
        self,
        action: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Record an attempt with action and result.
        
        Increments attempt counter and appends timestamped action/result
        to their respective lists for LLM context.
        
        Args:
            action: Action/tool that was executed (tool_id, params, etc.)
            result: Result/observation from the action (output, findings, etc.)
        """
        self.attempts += 1
        
        # Add timestamp to action
        action_with_timestamp = {
            **action,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.actions_taken.append(action_with_timestamp)
        
        # Add timestamp to result
        result_with_timestamp = {
            **result,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.results.append(result_with_timestamp)
    
    def mark_complete(
        self,
        completion_type: CompletionType,
        reasoning: str,
    ) -> None:
        """Mark todo as complete with reasoning.
        
        Updates completion metadata and sets appropriate status based on
        completion type (positive/negative/exhausted).
        
        Args:
            completion_type: How the todo was completed
            reasoning: LLM explanation of the completion decision
        """
        self.completed_at = datetime.now(timezone.utc)
        self.completion_type = completion_type
        self.completion_reasoning = reasoning
        
        # Set status based on completion type
        if completion_type == CompletionType.POSITIVE:
            self.status = TodoStatus.COMPLETE_POSITIVE
        elif completion_type == CompletionType.NEGATIVE:
            self.status = TodoStatus.COMPLETE_NEGATIVE
        elif completion_type == CompletionType.EXHAUSTED:
            self.status = TodoStatus.EXHAUSTED
        else:
            # INCOMPLETE should not be passed to mark_complete
            raise ValueError(
                f"Cannot mark todo as complete with CompletionType.INCOMPLETE. "
                f"Received: {completion_type}"
            )
    
    def is_complete(self) -> bool:
        """Check if todo is in any completed state."""
        return self.status in {
            TodoStatus.COMPLETE_POSITIVE,
            TodoStatus.COMPLETE_NEGATIVE,
            TodoStatus.EXHAUSTED,
        }
    
    @classmethod
    def from_string(cls, description: str) -> "TodoItem":
        """Create TodoItem from legacy string format.
        
        Provides backward compatibility for existing code that uses
        simple string todos.
        
        Args:
            description: Todo description string
            
        Returns:
            New TodoItem with default values
        """
        return cls(description=description)
    
    @classmethod
    def from_string_list(cls, todos: List[str]) -> List["TodoItem"]:
        """Convert list of strings to list of TodoItems.
        
        Helper for migrating legacy string-based todo lists.
        
        Args:
            todos: List of todo description strings
            
        Returns:
            List of TodoItem objects
        """
        return [cls.from_string(todo) for todo in todos]


class BudgetState(BaseModel):
    """Track iteration/tool budgets for a turn."""

    max_tool_calls: Optional[int] = None
    max_iterations: Optional[int] = None
    max_tokens: Optional[int] = None


class FactsState(BaseModel):
    """Compact, typed facts that guide routing."""

    task_id: int
    message: str
    conversation_id: Optional[str] = None
    capability: Optional[str] = None
    budgets: BudgetState = Field(default_factory=BudgetState)
    tool_ids: List[str] = Field(default_factory=list)
    tool_calls_used: int = 0
    iterations: int = 0
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form runtime metadata bag. This is the authoritative carrier for "
            "the working-memory snapshot used by post-tool reasoning continuity. "
            "PTR phase ledger fields live under metadata['working_memory']: "
            "'current_turn_phases' (ordered IterationMemoryRecord phase-section "
            "snapshots, not semantic fields), "
            "'current_turn_phase_counter' (monotonic per-turn phase counter), and "
            "'current_turn_phase_turn' (last turn the counter was scoped to). "
            "Runtime stamps turn_sequence/phase_sequence; PTR never invents them."
        ),
    )
    intent_hints: Dict[str, Any] = Field(default_factory=dict)
    risk_flags: List[str] = Field(default_factory=list)
    eligible_routes: List[str] = Field(default_factory=list)
    tool_candidates: List[str] = Field(default_factory=list)
    selected_tool: Optional[str] = None
    tool_parameters: Dict[str, Any] = Field(default_factory=dict)
    last_tool_result_compact: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Compact tool-result envelope (schema_version/tool/status/success/exit_code/"
            "summary/key_findings/errors/report_recommendations/structured_signals/"
            "decision_evidence/lossiness_risk/artifact_refs/compression). "
            "This is the canonical tool-output contract for downstream consumers."
        ),
    )
    
    # Deep Reasoning specific fields
    plan: List[str] = Field(default_factory=list)
    todo_list: Union[List[str], List[TodoItem]] = Field(
        default_factory=list,
        description="Task breakdown (supports legacy strings or rich TodoItems)"
    )
    current_goal: Optional[str] = None
    stuck_counter: int = 0
    decision_history: List[str] = Field(default_factory=list)
    post_reflect_action: Optional[str] = None
    
    # Tool intent hint from post_tool_reasoning
    # When observation says "I'll run X", this field captures that intent
    # so the planner can execute the correct tool instead of repeating the plan
    next_tool_hint: Optional[str] = Field(
        default=None,
        description="Hint from observation about intended next tool action (e.g., 'port scan with nmap -sS -p-')"
    )
    
    # Scope management fields (DR.5)
    scope_goals: List[str] = Field(default_factory=list)
    scope_boundaries: List[str] = Field(default_factory=list)
    achieved_goals: Set[str] = Field(default_factory=set)

    @property
    def safe_metadata(self) -> Dict[str, Any]:
        """Return metadata for read-only access, defaulting to an empty dict."""
        if self.metadata is None:
            return {}
        return self.metadata

    def metadata_copy(self) -> Dict[str, Any]:
        """Return a detached mutable copy of metadata."""
        return dict(self.safe_metadata)

    def ensure_metadata(self) -> Dict[str, Any]:
        """Ensure metadata is a mutable dict stored on this facts object."""
        if self.metadata is None:
            self.metadata = {}
        return self.metadata

    def get_candidate_decision(self) -> Optional[Dict[str, Any]]:
        """Return metadata candidate_decision as a mutable dict when present."""
        candidate = self.safe_metadata.get("candidate_decision")
        if isinstance(candidate, Mapping):
            return dict(candidate)
        return None

    def set_candidate_decision(self, payload: Optional[Mapping[str, Any]]) -> None:
        """Set or clear metadata candidate_decision contract payload."""
        metadata = self.ensure_metadata()
        if payload is None:
            metadata.pop("candidate_decision", None)
            return
        metadata["candidate_decision"] = dict(payload)

    def consume_candidate_decision(self) -> Optional[Dict[str, Any]]:
        """Return and clear metadata candidate_decision contract payload."""
        metadata = self.ensure_metadata()
        candidate = metadata.pop("candidate_decision", None)
        if isinstance(candidate, Mapping):
            return dict(candidate)
        return None

    def get_router_outcome(self) -> Optional[Dict[str, Any]]:
        """Return metadata router_outcome as a mutable dict when present."""
        outcome = self.safe_metadata.get("router_outcome")
        if isinstance(outcome, Mapping):
            return dict(outcome)
        return None

    def set_router_outcome(self, payload: Mapping[str, Any]) -> None:
        """Persist metadata router_outcome payload."""
        self.ensure_metadata()["router_outcome"] = dict(payload)

    def ensure_router_observability(self) -> Dict[str, Any]:
        """Return mutable router_observability map, creating one when missing."""
        metadata = self.ensure_metadata()
        observability = metadata.get("router_observability")
        if isinstance(observability, Mapping):
            normalized = dict(observability)
            metadata["router_observability"] = normalized
            return normalized
        metadata["router_observability"] = {}
        return metadata["router_observability"]

    @property
    def safe_decision_history(self) -> List[str]:
        """Return decision history for read-only access."""
        if self.decision_history is None:
            return []
        return self.decision_history

    def ensure_decision_history(self) -> List[str]:
        """Ensure decision history is a mutable list stored on this facts object."""
        if self.decision_history is None:
            self.decision_history = []
        return self.decision_history

    @property
    def safe_todo_list(self) -> List[Any]:
        """Return todo list for read-only access."""
        if self.todo_list is None:
            return []
        return self.todo_list


class ToolExecutionRecord(BaseModel):
    """Record of a tool execution during the turn.

    This schema is compact-only and does not retain raw stdout/stderr excerpts.
    """

    tool_id: str
    args: Dict[str, Any] = Field(default_factory=dict)
    status: str = "success"  # "success" or "error" - execution outcome
    observation: Optional[str] = None
    reasoning: Optional[str] = None
    approval_granted: Optional[bool] = None
    approval_reason: Optional[str] = None
    approval_metadata: Dict[str, Any] = Field(default_factory=dict)


class TraceState(BaseModel):
    """Verbose reasoning trace (logs, thoughts, observations)."""

    history: List[Dict[str, Any]] = Field(default_factory=list)
    reasoning: List[str] = Field(default_factory=list)
    # NOTE: intentionally kept as List[str]. The current-turn phase ledger
    # used for PTR continuity lives in FactsState.metadata['working_memory']
    # under the current_turn_* phase fields (see
    # agent.graph.utils.iteration_memory).
    # Do not mix structured objects into this field; prose observations
    # remain a compatibility surface until downstream consumers migrate.
    observations: List[str] = Field(default_factory=list)
    executed_tools: List[ToolExecutionRecord] = Field(default_factory=list)
    final_text: Optional[str] = None
    final_error: Optional[str] = None
    
    # Deep Reasoning specific fields
    scratchpad: str = ""
    decision_log: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Token usage tracking (Phase 3)
    # Stores UsageData dicts from LLM calls during this turn
    usage_records: List[Dict[str, Any]] = Field(default_factory=list)


class InteractiveState(BaseModel):
    """Combined state shared between graph nodes."""

    facts: FactsState
    trace: TraceState = Field(default_factory=TraceState)

    @classmethod
    def from_mapping(cls, state: Mapping[str, Any]) -> "InteractiveState":
        """Parse a mapping emitted by LangGraph into an InteractiveState."""
        if isinstance(state, cls):
            return state
        return cls.model_validate(state)

    def as_graph_state(self) -> Dict[str, Any]:
        """Return a full dump suitable for LangGraph graph state."""
        return self.model_dump()

    def as_graph_update(self) -> Dict[str, Any]:
        """Return a dict that can be merged into the graph state."""
        return {
            "facts": self.facts.model_dump(),
            "trace": self.trace.model_dump(),
        }


class InteractiveInput(BaseModel):
    """Input payload provided by the backend when invoking the graph."""

    task_id: int
    message: str
    conversation_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_state(self) -> InteractiveState:
        """Create the initial interactive state for the graph."""
        metadata_copy = dict(self.metadata)
        intent_hints = dict(metadata_copy.get("intent_hints") or {})
        risk_flags = list(metadata_copy.get("risk_flags") or [])
        eligible_routes = list(metadata_copy.get("eligible_routes") or [])
        tool_candidates = list(metadata_copy.get("tool_candidates") or [])
        selected_tool = metadata_copy.get("selected_tool")
        tool_parameters = dict(metadata_copy.get("tool_parameters") or {})

        capability = metadata_copy.get("forced_capability") or metadata_copy.get("initial_capability")

        facts = FactsState(
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            message=self.message,
            capability=capability,
            metadata=metadata_copy,
            intent_hints=intent_hints,
            risk_flags=risk_flags,
            eligible_routes=eligible_routes,
            tool_candidates=tool_candidates,
            selected_tool=selected_tool,
            tool_parameters=tool_parameters,
        )
        interactive = InteractiveState(facts=facts)

        reasoning_log = metadata_copy.get("intent_classifier_reasoning")
        if reasoning_log:
            if isinstance(reasoning_log, str):
                interactive.trace.reasoning.append(reasoning_log)
            elif isinstance(reasoning_log, list):
                for entry in reasoning_log:
                    interactive.trace.reasoning.append(str(entry))

        return interactive


GraphState = MutableMapping[str, Any]


__all__ = [
    "TodoStatus",
    "CompletionType",
    "AgentPauseRequest",
    "TodoItem",
    "BudgetState",
    "FactsState",
    "ToolExecutionRecord",
    "TraceState",
    "InteractiveState",
    "InteractiveInput",
    "GraphState",
    "IterationMemoryRecord",
]
