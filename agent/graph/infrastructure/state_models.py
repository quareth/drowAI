"""Extended state models for advanced LangGraph orchestration."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..state import FactsState, InteractiveState, TraceState

logger = logging.getLogger(__name__)


class IntentSignals(BaseModel):
    """Bundle classifier and heuristic outputs used for routing decisions."""

    classifier_label: Optional[str] = None
    classifier_confidence: Optional[float] = None
    heuristic_labels: List[str] = Field(default_factory=list)
    suggested_capabilities: List[str] = Field(default_factory=list)
    safety: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CapabilityType(str, Enum):
    """Canonical capability taxonomy for deep reasoning.
    
    This enum provides a single source of truth for capability types,
    preventing routing inconsistencies and cache key mismatches.
    """

    HOST_DISCOVERY = "host_discovery"
    PORT_SCAN = "port_scan"
    SERVICE_ENUM = "service_enum"
    VULN_SCAN = "vuln_scan"
    VULN_EXPLOIT = "vuln_exploit"
    REPORT = "report"
    RESPOND = "respond"  # Simple chat response

    @classmethod
    def from_intent(cls, user_intent: str) -> "CapabilityType":
        """Parse capability from string (advisory hint, not enforced).
        
        This method provides an advisory capability hint for routing and logging,
        but does NOT enforce tool filtering. Unknown capabilities are treated as
        RESPOND (chat-only) but the planner will still have access to the full
        tool catalog for LLM-based selection.
        
        Approach:
        - LLM provides structured capability enum values (e.g., "host_discovery")
        - Simple normalization for case/format variations
        - Unknown capabilities → RESPOND (advisory only)
        - Tool selection happens via LLM reasoning, not capability filtering
        
        Args:
            user_intent: Capability enum value or close variation
        
        Returns:
            CapabilityType enum value (advisory hint for routing/logging)
        """
        if not user_intent:
            logger.debug("[CAPABILITY] Empty intent, using RESPOND (advisory)")
            return cls.RESPOND
        
        # Normalize: lowercase, strip whitespace, replace separators
        normalized = user_intent.lower().strip().replace("-", "_").replace(" ", "_")
        
        # Try direct enum match
        try:
            return cls(normalized)
        except (ValueError, AttributeError):
            # Not a valid enum value - log as info (not a problem)
            # The capability is advisory only; LLM will see full tool catalog
            logger.info(
                f"[CAPABILITY] '{user_intent}' (normalized: '{normalized}') not in enum. "
                f"Using RESPOND as advisory hint. LLM will see full tool catalog for selection."
            )
            return cls.RESPOND
    
    def get_tool_categories(self) -> List[str]:
        """Get tool categories that support this capability.
        
        Returns:
            List of tool category names
        """
        mapping = {
            CapabilityType.HOST_DISCOVERY: ["information_gathering"],
            CapabilityType.PORT_SCAN: ["information_gathering"],
            CapabilityType.SERVICE_ENUM: ["information_gathering", "system_services"],
            CapabilityType.VULN_SCAN: [
                "vulnerability_analysis",
                "web_applications",
                "database_assessment",
            ],
            CapabilityType.VULN_EXPLOIT: ["exploitation_tools", "password_attacks"],
            CapabilityType.REPORT: ["reporting_tools"],
            CapabilityType.RESPOND: [],
        }
        return mapping.get(self, [])


class PersonaState(BaseModel):
    """Represents optional persona or profile metadata for a task.

    Required fields for orchestration:
    - `name` / `description` provide tone guidance for prompts.
    - `tone` influences system prompts within deep reasoning flows.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    tone: Optional[str] = None


class BudgetEnvelope(BaseModel):
    """Tracks timing and iteration budgets for a turn (deep reasoning requirement).

    Required slots:
    - `time_budget_ms` controls wall-clock budget for cooperative cancellation.
    - `remaining_iterations` decrements per node visit to prevent infinite loops.
    - `remaining_tool_calls` guards expensive executor usage.
    """

    time_budget_ms: Optional[int] = None
    remaining_iterations: Optional[int] = None
    remaining_tool_calls: Optional[int] = None


class CancellationTokens(BaseModel):
    """Cooperative cancellation handles referenced by graph nodes."""

    cancelled: bool = False
    reason: Optional[str] = None


class ExtendedFactsState(FactsState):
    """Facts extended with persona, workspace, and budget slots.

    Required additions:
    - `available_tools`: identifiers available to discovery/enumeration/reporting.
    - `runtime_budgets`: instance of `BudgetEnvelope` tracking remaining resources.
    - `cancellation`: cooperative cancellation state shared across nodes.
    """

    persona: PersonaState = Field(default_factory=PersonaState)
    workspace_root: Optional[str] = None
    history_summary: Optional[str] = None
    runtime_budgets: BudgetEnvelope = Field(default_factory=BudgetEnvelope)
    cancellation: CancellationTokens = Field(default_factory=CancellationTokens)
    available_tools: List[str] = Field(default_factory=list)


class ExtendedTraceState(TraceState):
    """Trace enriched with structured tool snippets and citations.

    Required additions:
    - `iteration_history`: ordered list of orchestration decisions.
    - `tool_summaries`: structured snapshots consumed by prompt builders.
    - `citations`: placeholders for later report compilation.
    """

    tool_summaries: List[Dict[str, str]] = Field(default_factory=list)
    citations: List[Dict[str, str]] = Field(default_factory=list)
    iteration_history: List[str] = Field(default_factory=list)


class GraphRuntimeContext(BaseModel):
    """Context object passed to nodes via LangGraph metadata."""

    task_id: int
    user_id: Optional[int] = None
    graph_thread_id: Optional[str] = None
    tenant_id: Optional[int] = None
    runtime_placement_mode: Optional[str] = None
    workspace_id: Optional[str] = None
    actor_type: Optional[str] = None
    actor_id: Optional[str] = None
    runner_id: Optional[str] = None
    execution_site_id: Optional[str] = None
    workspace_path: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    credential_ref: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    feature_flags: Dict[str, bool] = Field(default_factory=dict)
    turn_id: Optional[str] = None
    turn_sequence: Optional[int] = None
    reserved_message_id: Optional[int] = None

    def normalized_runtime_placement_mode(self) -> Optional[str]:
        """Return normalized runtime placement mode when present."""
        if not isinstance(self.runtime_placement_mode, str):
            return None
        normalized = self.runtime_placement_mode.strip().lower()
        return normalized or None

    def is_user_originated(self) -> bool:
        """Return whether the context represents a user-originated actor."""
        actor_type = str(self.actor_type or "").strip().lower()
        return actor_type in {"user", "agent"}

    def missing_tool_runtime_identity_fields(self) -> List[str]:
        """Return required runtime identity fields missing for tool execution."""
        missing: List[str] = []
        if self.tenant_id is None:
            missing.append("tenant_id")
        if self.normalized_runtime_placement_mode() is None:
            missing.append("runtime_placement_mode")
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            missing.append("workspace_id")
        if not isinstance(self.actor_type, str) or not self.actor_type.strip():
            missing.append("actor_type")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            missing.append("actor_id")
        if self.is_user_originated() and self.user_id is None:
            missing.append("user_id")
        return missing

    def requires_local_workspace_path(self) -> bool:
        """Return whether local execution requires a local workspace path."""
        return self.normalized_runtime_placement_mode() == "local"


def build_budget_envelope(
    *,
    time_budget_ms: Optional[int] = 300_000,
    remaining_iterations: Optional[int] = 15,
    remaining_tool_calls: Optional[int] = 10,
) -> BudgetEnvelope:
    """Create a ``BudgetEnvelope`` with shared LangGraph turn defaults.

    Used by deep-reasoning planner initialization and by the shared
    runtime-budget bootstrap that runs at turn start for all tool graphs.
    """

    return BudgetEnvelope(
        time_budget_ms=time_budget_ms,
        remaining_iterations=remaining_iterations,
        remaining_tool_calls=remaining_tool_calls,
    )


def build_cancellation_tokens(
    *,
    cancelled: bool = False,
    reason: Optional[str] = None,
) -> CancellationTokens:
    """Return cooperative cancellation tokens shared across LangGraph nodes."""

    return CancellationTokens(cancelled=cancelled, reason=reason)


def initialize_extended_state(
    *,
    task_id: int,
    message: str,
    conversation_id: Optional[str] = None,
    available_tools: Optional[List[str]] = None,
    time_budget_ms: Optional[int] = None,
) -> InteractiveState:
    """Helper to initialize an InteractiveState with extended defaults.

    Ensures required structures (`available_tools`, `runtime_budgets`, `iteration_history`)
    are present even before real orchestration logic is wired up.
    """

    facts = ExtendedFactsState(
        task_id=task_id,
        message=message,
        conversation_id=conversation_id,
        available_tools=list(available_tools or []),
        runtime_budgets=build_budget_envelope(time_budget_ms=time_budget_ms),
        cancellation=build_cancellation_tokens(),
    )
    trace = ExtendedTraceState()
    return InteractiveState(facts=facts, trace=trace)


__all__ = [
    "BudgetEnvelope",
    "CancellationTokens",
    "CapabilityType",
    "ExtendedFactsState",
    "ExtendedTraceState",
    "GraphRuntimeContext",
    "IntentSignals",
    "PersonaState",
    "build_budget_envelope",
    "build_cancellation_tokens",
    "initialize_extended_state",
]
