"""Cache invalidation utilities for deep reasoning plan management.

This module provides functions to detect when cached plans should be invalidated
due to state changes (capability, goal, findings) or plan degradation.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

from backend.services.metrics.utils import safe_inc

from ..state import InteractiveState

logger = logging.getLogger(__name__)


def create_plan_context(state: InteractiveState) -> Dict[str, Any]:
    """Extract current state snapshot for plan context.
    
    Creates a context dict that captures the state when a plan was created,
    allowing comparison to detect when plans should be invalidated.
    
    Args:
        state: Current interactive state
        
    Returns:
        Dict with keys: capability, goal, findings_count, iteration, created_at
    """
    facts = state.facts
    
    # Count findings from various sources
    findings_count = 0
    
    # Count from trace observations
    if state.trace.observations:
        findings_count += len(state.trace.observations)
    
    # Count from executed tools
    if state.trace.executed_tools:
        findings_count += len(state.trace.executed_tools)
    
    # Count from metadata tool_history
    tool_history = facts.metadata.get("tool_history", [])
    if isinstance(tool_history, list):
        findings_count += len(tool_history)
    
    context = {
        "capability": facts.capability or "",
        "goal": facts.current_goal or "",
        "findings_count": findings_count,
        "iteration": facts.iterations,
        "created_at": time.time(),
    }
    
    logger.debug(
        f"[CACHE] Created plan context: capability={context['capability']}, "
        f"goal={context['goal']}, findings={findings_count}, iteration={facts.iterations}"
    )
    
    return context


def is_plan_degraded(plan: Union[Dict[str, Any], List[str]]) -> bool:
    """Check if plan has degraded to generic steps.
    
    Detects when a plan has been reduced to generic placeholders like
    "step 1", "step 2", etc., which indicates low-quality plan updates.
    
    Args:
        plan: Plan as either Dict with "plan" key or List[str] of steps
        
    Returns:
        True if plan is degraded (>50% generic steps), False otherwise
    """
    # Extract plan steps list
    if isinstance(plan, dict):
        plan_steps = plan.get("plan", [])
    elif isinstance(plan, list):
        plan_steps = plan
    else:
        logger.warning(f"[CACHE] Unknown plan type: {type(plan)}")
        return False
    
    if not plan_steps or len(plan_steps) == 0:
        return True  # Empty plan is considered degraded
    
    # Check for generic step pattern: "step 1", "step 2", etc.
    generic_pattern = re.compile(r"^step\s+\d+$", re.IGNORECASE)
    
    generic_count = 0
    for step in plan_steps:
        if isinstance(step, str):
            step_normalized = step.strip().lower()
            # Check if step matches generic pattern
            if generic_pattern.match(step_normalized):
                generic_count += 1
            # Also check for very short or empty steps
            elif len(step.strip()) < 10:
                generic_count += 0.5  # Partial match
    
    generic_ratio = generic_count / len(plan_steps) if plan_steps else 0.0
    
    is_degraded = generic_ratio > 0.5  # >50% generic
    
    if is_degraded:
        logger.info(
            f"[CACHE] Plan degraded: {generic_count}/{len(plan_steps)} steps are generic "
            f"(ratio: {generic_ratio:.2f})"
        )
    
    return is_degraded


def should_invalidate_plan(state: InteractiveState) -> bool:
    """Determine if cached plan should be invalidated.
    
    Checks multiple invalidation triggers:
    1. Last tool execution failed (CRITICAL - prevents retry with same broken params)
    2. Last tool execution succeeded with new observations (DEEP REASONING - ensures progression)
    3. Capability changed from plan creation context
    4. Goal changed from plan creation context
    5. Significant new findings added (>50% increase)
    6. Plan expired (>3 iterations old)
    7. Plan quality degraded
    
    Args:
        state: Current reasoning state
        
    Returns:
        True if plan should be invalidated, False otherwise
    """
    facts = state.facts
    metadata = facts.safe_metadata

    # Check if plan exists
    planner_plan = metadata.get("planner_plan")
    if not planner_plan:
        return False  # No plan to invalidate
    
    # Get plan context
    plan_context = metadata.get("plan_context")
    if not plan_context:
        logger.info("[CACHE] Invalidating plan: no plan context (should be regenerated)")
        safe_inc("cache_invalidation_no_context")
        return True
    
    # CRITICAL: Invalidate plan if next_tool_hint is present
    # When post_tool_reasoning says "I'll run X", we must generate fresh parameters
    # that respect this intent, not reuse old plan parameters
    next_tool_hint = metadata.get("next_tool_hint") or facts.next_tool_hint
    if next_tool_hint:
        logger.info(
            f"[CACHE] Invalidating plan: next_tool_hint is present "
            f"(hint: '{next_tool_hint[:50]}...')"
        )
        safe_inc("cache_invalidation_tool_hint")
        return True
    
    # CRITICAL: Invalidate plan if last tool execution failed
    # This prevents blindly retrying with the same broken parameters
    # The LLM error synthesis will be in conversation history for next parameter generation
    # 
    # EXCEPTION: If we're on the retry path, the failure_reflection and select_tool_categories
    # nodes have ALREADY updated the cached plan with corrected parameters. In this case,
    # we MUST NOT invalidate, or we'll throw away the corrected params and regenerate
    # the same faulty params from the original user message!
    last_tool_result = metadata.get("last_tool_result", {})
    # Trust the tool's success flag rather than re-interpreting exit codes.
    # Success is resolved centrally via informational exit codes plus hard CLI
    # failure detection in ``execution_outcome.resolve_execution_success``.
    tool_failed = last_tool_result.get("success") is False
    
    # Check if we're on the retry path (retry flow has already corrected the plan).
    # Lazy import: ``nodes.planner`` imports ``create_plan_context`` from this
    # module, so a top-level import of ``RETRY_METADATA_KEY`` from the active
    # retry core (which lives under ``nodes.post_tool_reasoning``) would create
    # a cycle through ``nodes.__init__``. Deferring the import keeps the
    # canonical key access in production code without restructuring the
    # package boundaries.
    from ..nodes.post_tool_reasoning.core.retry_logic import RETRY_METADATA_KEY

    retry_tracking = metadata.get(RETRY_METADATA_KEY, {}) or {}
    retry_count = retry_tracking.get("count", 0)
    plan_retry_corrected = metadata.get("plan_retry_corrected", False)
    is_retry_path = retry_count > 0 or plan_retry_corrected
    
    if tool_failed and not is_retry_path:
        logger.warning(
            "[CACHE] Invalidating plan: last tool execution failed "
            f"(success={last_tool_result.get('success')}, "
            f"exit_code={last_tool_result.get('exit_code')})"
        )
        safe_inc("cache_invalidation_tool_failure")
        return True
    elif tool_failed and is_retry_path:
        logger.info(
            "[CACHE] Skipping tool failure invalidation: on retry path "
            f"(retry_count={retry_count}, plan_retry_corrected={plan_retry_corrected}). "
            "Plan already has corrected parameters from failure_reflection."
        )
        safe_inc("cache_invalidation_skipped_retry_path")
    
    # DEEP REASONING MODE: Invalidate plan after each successful tool execution
    # In deep reasoning loops, each observation should inform the next action's parameters.
    # The planner must consider new findings when generating parameters for the next step.
    # This prevents the loop from repeating identical actions with identical parameters.
    tool_succeeded = last_tool_result and last_tool_result.get("success") is True
    if tool_succeeded:
        # Check if new observations were added since plan creation
        plan_iteration = plan_context.get("iteration", 0)
        current_iteration = facts.iterations
        
        # If we've advanced at least one iteration since plan creation, invalidate
        # This ensures each tool result informs the next parameter generation
        if current_iteration > plan_iteration:
            logger.info(
                f"[CACHE] Invalidating plan: new observations from tool execution "
                f"(plan created at iteration {plan_iteration}, now at {current_iteration})"
            )
            safe_inc("cache_invalidation_new_observations")
            return True
    
    # Check capability change (normalize to CapabilityType for comparison)
    current_capability = facts.capability or ""
    context_capability = plan_context.get("capability", "")
    
    # Normalize both capabilities to CapabilityType for accurate comparison
    capability_changed = False
    try:
        from ..infrastructure.state_models import CapabilityType
        
        if current_capability and context_capability:
            current_normalized = CapabilityType.from_intent(str(current_capability))
            context_normalized = CapabilityType.from_intent(str(context_capability))
            capability_changed = current_normalized != context_normalized
        else:
            # If one is empty, consider it changed
            capability_changed = current_capability != context_capability
    except (ImportError, Exception):
        # Fallback to string comparison if normalization fails
        capability_changed = current_capability != context_capability
    
    if capability_changed:
        logger.info(
            f"[CACHE] Invalidating plan: capability changed "
            f"from '{context_capability}' to '{current_capability}'"
        )
        safe_inc("cache_invalidation_capability_change")
        return True
    
    # Check goal change
    current_goal = facts.current_goal or ""
    context_goal = plan_context.get("goal", "")
    
    if current_goal != context_goal:
        logger.info(
            f"[CACHE] Invalidating plan: goal changed "
            f"from '{context_goal}' to '{current_goal}'"
        )
        safe_inc("cache_invalidation_goal_change")
        return True
    
    # Check findings growth (>50% increase)
    context_findings_count = plan_context.get("findings_count", 0)
    
    # Count current findings
    current_findings_count = 0
    if state.trace.observations:
        current_findings_count += len(state.trace.observations)
    if state.trace.executed_tools:
        current_findings_count += len(state.trace.executed_tools)
    tool_history = metadata.get("tool_history", [])
    if isinstance(tool_history, list):
        current_findings_count += len(tool_history)
    
    if context_findings_count > 0:
        growth_ratio = current_findings_count / context_findings_count
        if growth_ratio > 1.5:  # >50% increase
            logger.info(
                f"[CACHE] Invalidating plan: significant new findings "
                f"({context_findings_count} -> {current_findings_count}, "
                f"growth: {growth_ratio:.2f}x)"
            )
            safe_inc("cache_invalidation_findings_growth")
            return True
    
    # Check plan age (expiration)
    plan_iteration = plan_context.get("iteration", 0)
    current_iteration = facts.iterations
    
    if current_iteration - plan_iteration > 3:
        logger.info(
            f"[CACHE] Invalidating plan: expired "
            f"(created at iteration {plan_iteration}, now {current_iteration})"
        )
        safe_inc("cache_invalidation_age")
        return True
    
    # Check plan quality degradation
    # Check both facts.plan (List[str]) and planner_plan (Dict)
    plan_to_check = facts.plan if facts.plan else None
    if not plan_to_check and isinstance(planner_plan, dict):
        plan_to_check = planner_plan.get("plan", [])
    
    if plan_to_check and is_plan_degraded(plan_to_check):
        logger.info("[CACHE] Invalidating plan: quality degraded")
        safe_inc("cache_invalidation_degradation")
        return True
    
    return False


def invalidate_plan(state: InteractiveState, reason: Optional[str] = None) -> None:
    """Clear cached plan and plan context.
    
    Args:
        state: Current interactive state
        reason: Optional reason for invalidation (for logging)
    """
    facts = state.facts
    metadata = facts.ensure_metadata()

    had_plan = "planner_plan" in metadata
    had_context = "plan_context" in metadata
    
    # Clear plan and context
    if "planner_plan" in metadata:
        del metadata["planner_plan"]
    
    if "plan_context" in metadata:
        del metadata["plan_context"]
    
    if had_plan or had_context:
        log_msg = "[CACHE] Invalidated cached plan"
        if reason:
            log_msg += f": {reason}"
        logger.info(log_msg)


__all__ = [
    "create_plan_context",
    "is_plan_degraded",
    "should_invalidate_plan",
    "invalidate_plan",
]
