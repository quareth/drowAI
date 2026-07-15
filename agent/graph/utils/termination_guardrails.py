"""Termination guardrails for deep reasoning loops.

This module provides functions to detect when the agent should finalize
based on scope goals, loop detection, budget exhaustion, and progress tracking.

**DEPRECATION NOTICE:**
The hardcoded goal completion checks in this module (are_scope_goals_achieved,
check_goal_completion) are deprecated in favor of post-tool progress tracking.

The legacy functions are maintained for backward compatibility and as fallbacks.
New progress tracking should flow through post-tool reasoning todo progress.

This module will be removed in a future release (planned for v2.0).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.services.metrics.utils import safe_inc

from ..state import InteractiveState
from .scope_progress import calculate_scope_progress

logger = logging.getLogger(__name__)


def are_scope_goals_achieved(state: InteractiveState) -> bool:
    """Check if user's explicit goals are achieved.
    
    Args:
        state: Current reasoning state
        
    Returns:
        True if all scope goals are achieved, False otherwise
    """
    facts = state.facts
    metadata = facts.safe_metadata

    # Get scope goals from facts first (primary source), then metadata (backward compatibility)
    scope_goals = facts.scope_goals or metadata.get("scope_goals", [])
    if not scope_goals:
        return False  # No goals defined, can't be achieved
    
    # Get achieved goals from facts first, then metadata
    achieved_goals = facts.achieved_goals or set()
    if isinstance(achieved_goals, list):
        achieved_goals = set(achieved_goals)
    
    # Fallback to metadata if facts doesn't have it
    if not achieved_goals:
        achieved_goals = metadata.get("achieved_goals", set())
        if isinstance(achieved_goals, list):
            achieved_goals = set(achieved_goals)
    
    # Check if all goals are achieved
    all_achieved = all(goal in achieved_goals for goal in scope_goals)
    
    if all_achieved:
        logger.info(
            f"[ROUTER] All scope goals achieved: {scope_goals} "
            f"(achieved: {achieved_goals})"
        )
    
    return all_achieved


def check_goal_completion(
    goal: str,
    findings: List[Dict[str, Any]],
    observations: List[str],
) -> bool:
    """Check if specific goal has been achieved based on findings.
    
    Args:
        goal: Goal string to check (e.g., "find_vulnerable_services")
        findings: List of finding dicts from tool executions
        observations: List of observation strings from trace
        
    Returns:
        True if goal is achieved, False otherwise
    """
    completion_criteria = {
        "find_vulnerable_services": lambda f, o: (
            # Require actual vulnerability findings, not just mentions
            # Must have vulnerability type AND specific details (CVE, exploit, confirmed vuln)
            any(
                finding.get("type") == "vulnerability" 
                and ("cve" in str(finding).lower() 
                     or "exploit" in str(finding).lower()
                     or "confirmed" in str(finding).lower())
                for finding in f
            )
            or any(
                # Observations must show actual vulnerability scanning happened
                ("cve-" in obs.lower() or "exploit" in obs.lower())
                and ("found" in obs.lower() or "detected" in obs.lower() or "discovered" in obs.lower())
                for obs in o
            )
        ),
        "identify_hosts": lambda f, o: (
            any(
                finding.get("type") == "host_discovered" or "host" in str(finding).lower()
                for finding in f
            )
            or any("host" in obs.lower() for obs in o)
        ),
        "identify_open_ports": lambda f, o: (
            any(
                "open" in str(finding).lower() and "port" in str(finding).lower()
                for finding in f
            )
            or any("open" in obs.lower() and "port" in obs.lower() for obs in o)
        ),
        "identify_services": lambda f, o: (
            any("service" in str(finding).lower() for finding in f)
            or any("service" in obs.lower() for obs in o)
        ),
    }
    
    criteria_fn = completion_criteria.get(goal)
    if not criteria_fn:
        logger.debug(f"[ROUTER] No completion criteria for goal: {goal}")
        return False
    
    is_complete = criteria_fn(findings, observations)
    if is_complete:
        logger.info(f"[ROUTER] Goal achieved: {goal}")
        safe_inc(f"goal_completed_{goal}")
    
    return is_complete


def is_action_loop_detected(state: InteractiveState) -> bool:
    """Detect if last 3 actions are identical AND loop is still active.
    
    Checks if the last 3 tool executions or decisions are identical
    (same tool_id with same key parameters).
    
    A loop is only considered "active" if the cached plan that caused it
    is still valid. If the cache has been invalidated, the historical loop
    is fixed and won't continue.
    
    Args:
        state: Current reasoning state
        
    Returns:
        True if active loop detected, False otherwise
    """
    facts = state.facts
    trace = state.trace
    
    # Get action history from metadata or trace
    action_history = facts.metadata.get("action_history", [])
    
    # If no action history, try to build from executed tools
    if not action_history and trace.executed_tools:
        action_history = [
            {
                "tool_id": tool.tool_id,
                "params": dict(tool.args) if hasattr(tool, "args") else {},
            }
            for tool in trace.executed_tools[-5:]  # Last 5 tools
        ]
    
    if len(action_history) < 3:
        return False
    
    # Get last 3 actions
    last_three = action_history[-3:]
    
    # Normalize actions for comparison (tool_id and key parameters, not timestamps)
    normalized = []
    for action in last_three:
        if isinstance(action, dict):
            tool_id = action.get("tool_id", "")
            # Get key parameters (exclude noise like timestamps)
            params = action.get("params", {})
            key_params = {
                k: v
                for k, v in params.items()
                if k not in ["timestamp", "request_id", "transport"]
            }
            # Make all values hashable (convert lists to tuples)
            hashable_params = {}
            for k, v in key_params.items():
                if isinstance(v, list):
                    hashable_params[k] = tuple(v)
                elif isinstance(v, dict):
                    hashable_params[k] = tuple(sorted(v.items()))
                else:
                    hashable_params[k] = v
            normalized.append((tool_id, tuple(sorted(hashable_params.items()))))
        else:
            # If action is just a string, use it as-is
            normalized.append((str(action), ()))
    
    # Check if all three are identical
    if len(set(normalized)) == 1:
        # Historical loop detected, but check if it's still active
        # A loop is only active if the cached plan that caused it still exists
        metadata = facts.safe_metadata
        cached_plan = metadata.get("planner_plan")
        
        if not cached_plan:
            # No cached plan means cache was invalidated - loop is fixed
            logger.info(
                "[LOOP] Historical loop detected but cache invalidated - loop is resolved"
            )
            return False
        
        # Loop is active (same actions AND cache still exists)
        logger.warning(
            f"[LOOP] Active loop detected: last 3 actions identical "
            f"({normalized[0][0]} with params {normalized[0][1]}) AND cached plan still active"
        )
        safe_inc("router_loop_detected")
        return True
    
    # Also check for "same tool, same failure" pattern with minor parameter variations
    # This catches cases where LLM tries different formats but same tool keeps failing
    if _is_same_tool_failure_loop(action_history, facts.metadata):
        logger.warning(
            "[LOOP] Same-tool-failure loop detected: same tool failing repeatedly with variations"
        )
        safe_inc("router_loop_same_tool_failure")
        return True
    
    return False


def _is_same_tool_failure_loop(
    action_history: list,
    metadata: dict,
) -> bool:
    """Detect if same tool is failing repeatedly with minor parameter variations.
    
    This catches patterns like:
    - nmap with target="a,b,c" → 0 hosts
    - nmap with target="a b c" → 0 hosts  
    - nmap with target="a,b,c" → 0 hosts
    
    Args:
        action_history: List of action records with tool_id and params
        metadata: Facts metadata containing tool results
        
    Returns:
        True if same-tool-failure loop detected
    """
    if len(action_history) < 3:
        return False
    
    last_three = action_history[-3:]
    
    # Check if all three use the same tool
    tool_ids = [
        action.get("tool_id", "") if isinstance(action, dict) else str(action)
        for action in last_three
    ]
    
    if len(set(tool_ids)) != 1:
        return False  # Different tools, not a same-tool loop
    
    # Check if tool is consistently failing (success=False or 0 hosts)
    tool_history = metadata.get("tool_history", [])
    if len(tool_history) < 3:
        return False
    
    recent_results = tool_history[-3:]
    failure_count = 0
    
    for result in recent_results:
        # Check explicit failure
        if result.get("success") is False:
            failure_count += 1
            continue
        
        # Check "0 hosts scanned" (false success)
        result_metadata = result.get("metadata", {})
        if result_metadata.get("hosts_total", -1) == 0:
            failure_count += 1
            continue
        
        # Check warning flags
        if result_metadata.get("warning") == "zero_hosts_scanned":
            failure_count += 1
            continue
    
    # If 3 consecutive failures with same tool, it's a loop
    if failure_count >= 3:
        logger.info(
            f"[LOOP] Detected {failure_count} consecutive failures "
            f"with same tool: {tool_ids[0]}"
        )
        return True
    
    return False


def is_stuck_without_progress(state: InteractiveState) -> bool:
    """Check if no new findings for 2+ iterations.
    
    Compares observation hashes to detect when observations are
    identical across iterations, indicating no progress.
    
    Args:
        state: Current reasoning state
        
    Returns:
        True if stuck without progress, False otherwise
    """
    facts = state.facts
    metadata = facts.safe_metadata

    # Get observation hashes from metadata
    observation_hashes = metadata.get("observation_hashes", [])
    
    if len(observation_hashes) < 2:
        return False
    
    # Check if last 2 observations are identical
    if observation_hashes[-1] == observation_hashes[-2]:
        logger.info(
            "[ROUTER] No progress detected: last 2 observations are identical"
        )
        safe_inc("router_finalize_no_progress")
        return True
    
    return False


def has_sufficient_findings(state: InteractiveState) -> bool:
    """Check if agent has sufficient findings to finalize.
    
    Used when loop is detected to decide between reflect and finalize.
    
    Args:
        state: Current reasoning state
        
    Returns:
        True if sufficient findings exist, False otherwise
    """
    trace = state.trace
    
    # Check if we have meaningful observations or tool results
    has_observations = len(trace.observations or []) > 0
    has_tool_results = len(trace.executed_tools or []) > 0
    
    # Check if findings are substantial (not just empty results)
    substantial_observations = any(
        len(obs.strip()) > 20 for obs in (trace.observations or [])
    )
    
    return (has_observations and substantial_observations) or has_tool_results


def calculate_termination_bias(state: InteractiveState) -> float:
    """Calculate bias toward finalization based on state.
    
    Returns a value between 0.0 (no bias) and 1.0 (strong bias to finalize).
    
    Args:
        state: Current reasoning state
        
    Returns:
        Termination bias value (0.0-1.0)
    """
    facts = state.facts
    metadata = facts.safe_metadata

    bias = 0.0
    
    # Bias increases if scope goals achieved
    if are_scope_goals_achieved(state):
        bias += 0.5
    
    # Bias increases with iteration count (closer to budget)
    runtime_budgets = metadata.get("runtime_budgets", {})
    remaining_iterations = runtime_budgets.get("remaining_iterations", 999)
    max_iterations = facts.budgets.max_iterations or 15
    
    if remaining_iterations is not None and max_iterations > 0:
        used_ratio = (max_iterations - remaining_iterations) / max_iterations
        if used_ratio > 0.75:
            bias += 0.3  # High iteration usage
        elif used_ratio > 0.5:
            bias += 0.15  # Medium iteration usage
    
    # Bias increases if no progress detected
    if is_stuck_without_progress(state):
        bias += 0.4
    
    # Bias increases if we have substantial findings
    if has_sufficient_findings(state):
        bias += 0.2
    
    # DR.5.5: Bias increases with scope progress
    scope_progress = calculate_scope_progress(state)
    if scope_progress >= 0.75:
        bias += 0.3  # High progress (75%+)
    elif scope_progress >= 0.5:
        bias += 0.15  # Medium progress (50-75%)
    elif scope_progress >= 0.25:
        bias += 0.05  # Low progress (25-50%)
    
    # DR.6.4: Bias increases with no_progress_count
    no_progress_count = metadata.get("no_progress_count", 0)
    if no_progress_count >= 2:
        bias += 0.4  # Strong bias when no progress for 2+ observations
    elif no_progress_count >= 1:
        bias += 0.2  # Moderate bias when no progress for 1 observation
    
    # Cap at 1.0
    return min(bias, 1.0)


def check_iteration_budget_warnings(state: InteractiveState) -> None:
    """Log budget warnings at 75%, 90%, 100% usage.
    
    Args:
        state: Current reasoning state
    """
    facts = state.facts
    metadata = facts.safe_metadata
    runtime_budgets = metadata.get("runtime_budgets", {})
    
    remaining_iterations = runtime_budgets.get("remaining_iterations", 999)
    max_iterations = facts.budgets.max_iterations or 15
    
    if remaining_iterations is not None and max_iterations > 0:
        used_ratio = (max_iterations - remaining_iterations) / max_iterations
        
        if used_ratio >= 1.0:
            logger.warning(
                f"[ROUTER] Budget exhausted: {max_iterations - remaining_iterations}/{max_iterations} iterations used"
            )
            safe_inc("router_iteration_budget_warning")
        elif used_ratio >= 0.9:
            logger.warning(
                f"[ROUTER] Budget warning: {max_iterations - remaining_iterations}/{max_iterations} iterations used (90%)"
            )
            safe_inc("router_iteration_budget_warning")
        elif used_ratio >= 0.75:
            logger.warning(
                f"[ROUTER] Budget warning: {max_iterations - remaining_iterations}/{max_iterations} iterations used (75%)"
            )
            safe_inc("router_iteration_budget_warning")


__all__ = [
    "are_scope_goals_achieved",
    "check_goal_completion",
    "is_action_loop_detected",
    "is_stuck_without_progress",
    "has_sufficient_findings",
    "calculate_termination_bias",
    "check_iteration_budget_warnings",
]
