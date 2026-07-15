"""Pause request handling for decision router.

This module handles agent-initiated pause logic for user confirmation
before continuing with potentially long or risky operations.

Uses AgentPauseRequest from agent.graph.state - DO NOT recreate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from agent.config import AgentConfig

if TYPE_CHECKING:
    from ...infrastructure.state_models import GraphRuntimeContext
    from ...state import InteractiveState

# Import AgentPauseRequest from state - DO NOT duplicate
from ...state import AgentPauseRequest, TodoStatus

logger = logging.getLogger(__name__)


# =============================================================================
# Pause Condition Checks
# =============================================================================


def should_pause_for_confirmation(
    interactive: "InteractiveState",
    config: AgentConfig,
) -> tuple[bool, Optional[str]]:
    """Determine if agent should pause for user confirmation.
    
    Pause conditions (evaluated in order):
    1. Many todos remaining (>= configured threshold)
    2. Context getting long (>= configured observation count)
    3. About to use risky tools (exploit, attack keywords)
    4. Budget concerns (many tools used, many todos remaining)
    
    Args:
        interactive: Current agent state
        config: Agent configuration with pause thresholds
        
    Returns:
        Tuple of (should_pause: bool, reason: Optional[str])
    """
    from .helpers import get_current_todo
    
    facts = interactive.facts
    trace = interactive.trace
    
    # Get remaining todos
    remaining = [
        t for t in facts.safe_todo_list
        if hasattr(t, 'status') and t.status == TodoStatus.PENDING
    ]
    
    # Condition 1: Many todos remaining
    if len(remaining) >= config.pause_min_remaining_todos:
        return True, f"many_todos_remaining ({len(remaining)} pending)"
    
    # Condition 2: Context getting long
    observations = trace.observations or []
    if len(observations) >= config.pause_context_length_threshold:
        return True, "context_length"
    
    # Condition 3: About to use risky tools
    current_todo = get_current_todo(facts)
    if current_todo:
        risky_keywords = ["exploit", "attack", "penetrate", "breach", "inject"]
        description_lower = current_todo.description.lower()
        if any(keyword in description_lower for keyword in risky_keywords):
            return True, "risky_action"
    
    # Condition 4: Budget concerns (many tools used + many todos remaining)
    if facts.tool_calls_used >= config.pause_budget_concern_tools and len(remaining) >= 3:
        return True, "budget_concerns"
    
    return False, None


# =============================================================================
# Pause Request Building
# =============================================================================


def build_pause_request(
    interactive: "InteractiveState",
    reason: str,
) -> AgentPauseRequest:
    """Build AgentPauseRequest from current state.
    
    Args:
        interactive: Current agent state
        reason: Pause reason code
        
    Returns:
        AgentPauseRequest with progress summary and question
    """
    from .helpers import get_current_todo
    
    facts = interactive.facts
    trace = interactive.trace
    
    # Extract progress summary
    completed_todos = [
        t for t in facts.safe_todo_list
        if hasattr(t, 'is_complete') and t.is_complete()
    ]
    remaining_todos = [
        t for t in facts.safe_todo_list
        if hasattr(t, 'status') and t.status == TodoStatus.PENDING
    ]
    
    progress_summary = {
        "completed_todos": len(completed_todos),
        "remaining_todos": len(remaining_todos),
        "tools_executed": facts.tool_calls_used,
        "iterations": facts.iterations,
        "observations_count": len(trace.observations or []),
        "findings_count": len([
            t for t in (trace.executed_tools or [])
            if hasattr(t, 'observation') and t.observation and len(t.observation) > 50
        ]),
    }
    
    # Extract remaining todo descriptions
    remaining_descriptions = [
        t.description for t in remaining_todos
        if hasattr(t, 'description')
    ]
    
    # Build human-readable question based on reason
    estimated_time: Optional[int] = None
    estimated_tool_calls: Optional[int] = None
    
    if reason.startswith("many_todos_remaining"):
        question = (
            f"I have {len(remaining_todos)} tasks still pending. "
            "Should I continue with the remaining tasks?"
        )
        estimated_time = len(remaining_todos) * 60  # Rough estimate: 1 min per todo
        estimated_tool_calls = len(remaining_todos) * 2  # Rough estimate: 2 tools per todo
        
    elif reason == "context_length":
        question = (
            f"I've gathered {len(trace.observations)} observations so far. "
            "The context is getting quite long. Should I continue or finalize?"
        )
        
    elif reason == "risky_action":
        current = get_current_todo(facts)
        question = (
            f"Next task involves potentially risky actions: "
            f"'{current.description if current else 'unknown'}'. "
            "Should I proceed with this task?"
        )
        estimated_time = 120  # Risky actions might take 2 minutes
        estimated_tool_calls = 3
        
    elif reason == "budget_concerns":
        question = (
            f"I've used {facts.tool_calls_used} tools and have "
            f"{len(remaining_todos)} tasks remaining. "
            "Should I continue or wrap up?"
        )
        estimated_time = len(remaining_todos) * 60
        estimated_tool_calls = len(remaining_todos) * 2
        
    else:
        question = "Should I continue with the remaining tasks?"
    
    return AgentPauseRequest(
        reason=reason,
        current_progress=progress_summary,
        remaining_todos=remaining_descriptions,
        question=question,
        estimated_time=estimated_time,
        estimated_tool_calls=estimated_tool_calls,
    )


# =============================================================================
# Pause Response Handling
# =============================================================================


async def emit_and_wait_for_pause_response(
    pause_request: AgentPauseRequest,
    context: Optional["GraphRuntimeContext"],
    interactive: "InteractiveState",
    config: AgentConfig,
) -> bool:
    """Emit pause request and wait for user response.
    
    Emits pause request to AgentLog for streaming to frontend,
    then polls workspace for response file with timeout.
    
    Args:
        pause_request: Pause request to emit
        context: Runtime context
        interactive: Current agent state
        config: Agent configuration with timeout
        
    Returns:
        True if user approved continuation, False otherwise
    """
    task_id = interactive.facts.task_id
    
    # Log pause request
    logger.info(
        f"[PAUSE] Requesting user confirmation for task {task_id}: {pause_request.reason}"
    )
    
    # Emit to trace for state persistence
    interactive.trace.reasoning.append(
        f"[PAUSE] Requesting user confirmation: {pause_request.question}"
    )
    
    # Emit pause request event (will be picked up by streaming adapters)
    # We add it to metadata so the streaming adapter can emit it
    metadata = interactive.facts.ensure_metadata()
    metadata["agent_pause_request"] = pause_request.model_dump()
    interactive.facts.metadata = metadata
    
    # Determine workspace path for response file from provider-projected runtime context.
    if context and getattr(context, "workspace_path", None):
        workspace_path = Path(context.workspace_path)
    else:
        raise RuntimeError(
            "decision_router.pause: runtime workspace_path is required for task-scoped "
            "pause responses; upstream runtime provider projection is missing workspace identity."
        )
    
    response_file = workspace_path / "agent_pause_response.json"
    
    # Clean up any old response files
    if response_file.exists():
        try:
            response_file.unlink()
        except Exception as exc:
            logger.warning(f"[PAUSE] Could not remove old response file: {exc}")
    
    # Wait for response with timeout
    timeout_seconds = config.pause_response_timeout
    poll_interval = 1.0  # Poll every second
    elapsed = 0.0
    
    logger.info(
        f"[PAUSE] Waiting for user response (timeout: {timeout_seconds}s)"
    )
    
    while elapsed < timeout_seconds:
        # Check if response file exists
        if response_file.exists():
            try:
                with open(response_file, "r") as f:
                    response = json.load(f)
                
                # Clean up response file
                try:
                    response_file.unlink()
                except Exception:
                    pass
                
                approved = response.get("approved", False)
                user_message = response.get("message", "")
                
                logger.info(
                    f"[PAUSE] User response received: approved={approved}, "
                    f"message='{user_message}'"
                )
                
                # Record response in trace
                interactive.trace.reasoning.append(
                    f"[PAUSE] User response: {'Continue' if approved else 'Stop'} - {user_message}"
                )
                
                return approved
                
            except Exception as exc:
                logger.error(
                    f"[PAUSE] Error reading response file: {exc}"
                )
                # Default to continue on read error (don't block agent)
                return True
        
        # Wait before next poll
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    
    # Timeout - default to continue (don't block agent indefinitely)
    logger.warning(
        f"[PAUSE] Response timeout after {timeout_seconds}s, defaulting to continue"
    )
    interactive.trace.reasoning.append(
        "[PAUSE] Timeout waiting for user response, continuing"
    )
    
    return True


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "should_pause_for_confirmation",
    "build_pause_request",
    "emit_and_wait_for_pause_response",
]
