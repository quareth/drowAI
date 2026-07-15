"""Goal completion tracking for scope management.

**DEPRECATION NOTICE:**
The hardcoded goal completion criteria in this module (check_goal_completion,
get_completion_criteria) are deprecated in favor of post-tool progress tracking.

The legacy functions are maintained for backward compatibility. New progress
tracking should flow through post-tool reasoning todo progress.

This module will be removed in a future release (planned for v2.0).
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, Callable, Dict, List, Optional

from backend.services.metrics.utils import safe_inc

from ..state import InteractiveState

logger = logging.getLogger(__name__)


def check_goal_completion(
    goal: str,
    findings: List[Dict[str, Any]],
    observations: List[str],
) -> bool:
    """
    Check if specific goal has been achieved based on findings.
    
    .. deprecated:: 1.0
        Use post-tool reasoning todo progress instead. This function will be
        removed in v2.0.

    Args:
        goal: Goal string to check (e.g., "find_vulnerable_services")
        findings: List of finding dicts from tool executions
        observations: List of observation strings from trace

    Returns:
        True if goal is achieved, False otherwise
    """
    warnings.warn(
        "check_goal_completion() is deprecated. "
        "Use post-tool reasoning todo progress instead. "
        "This function will be removed in v2.0.",
        DeprecationWarning,
        stacklevel=2
    )
    
    completion_criteria = _get_completion_criteria()

    criteria_fn = completion_criteria.get(goal)
    if not criteria_fn:
        logger.debug(f"[GOAL] No completion criteria for goal: {goal}")
        return False

    is_complete = criteria_fn(findings, observations)
    if is_complete:
        logger.info(f"[GOAL] Goal achieved: {goal}")
        safe_inc(f"goal_completed_{goal}")

    return is_complete


def get_completion_criteria(goal: str) -> Optional[Callable[[List[Dict], List[str]], bool]]:
    """
    Get completion criteria function for a goal type.

    Args:
        goal: Goal string

    Returns:
        Criteria function or None if goal not recognized
    """
    criteria = _get_completion_criteria()
    return criteria.get(goal)


def _get_completion_criteria() -> Dict[str, Callable[[List[Dict], List[str]], bool]]:
    """Get all completion criteria functions.
    
    Criteria are designed to require actual findings, not just mentions:
    - find_vulnerable_services: Requires actual CVE/exploit findings, not just "may have vulnerabilities"
    - identify_hosts: Requires host discovery
    - identify_open_ports: Requires actual open ports
    - identify_services: Requires service enumeration
    """
    return {
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
                finding.get("type") == "host_discovered"
                or "host" in str(finding).lower()
                or "machine" in str(finding).lower()
                for finding in f
            )
            or any("host" in obs.lower() or "machine" in obs.lower() for obs in o)
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


def update_achieved_goals(state: InteractiveState) -> None:
    """
    Update achieved goals in state based on current findings and observations.

    This function:
    1. Gets scope goals from state
    2. Checks each goal against findings/observations
    3. Updates achieved_goals set in state
    4. Logs newly achieved goals

    Args:
        state: Current reasoning state
    """
    facts = state.facts
    trace = state.trace
    metadata = facts.safe_metadata

    # Get scope goals
    scope_goals = facts.scope_goals or []
    if not scope_goals:
        # Try to get from metadata (for backward compatibility)
        user_scope = metadata.get("user_scope")
        if user_scope:
            if isinstance(user_scope, dict):
                from .scope_parser import UserScope

                user_scope = UserScope.from_dict(user_scope)
            scope_goals = user_scope.goals if hasattr(user_scope, "goals") else []

    if not scope_goals:
        return  # No goals to track

    # Get current achieved goals
    achieved_goals = facts.achieved_goals or set()
    if isinstance(achieved_goals, list):
        achieved_goals = set(achieved_goals)

    # Extract findings from executed tools
    findings = []
    for tool_record in trace.executed_tools or []:
        if hasattr(tool_record, "observation") and tool_record.observation:
            findings.append({"type": "tool_output", "content": tool_record.observation})

    # Also check synthesized output from metadata
    synthesized = metadata.get("synthesized_output") or {}
    if synthesized:
        key_findings = synthesized.get("key_findings", [])
        vulnerabilities = synthesized.get("vulnerabilities", [])
        for finding in key_findings:
            findings.append({"type": "finding", "content": str(finding)})
        for vuln in vulnerabilities:
            findings.append({"type": "vulnerability", "content": str(vuln)})

    # Get observations
    observations = trace.observations or []

    # Check each goal
    newly_achieved = []
    for goal in scope_goals:
        if goal not in achieved_goals:
            if check_goal_completion(goal, findings, observations):
                achieved_goals.add(goal)
                newly_achieved.append(goal)

    # Update state
    facts.achieved_goals = achieved_goals
    metadata["achieved_goals"] = list(achieved_goals)  # Store as list for JSON serialization
    facts.metadata = metadata

    # Log newly achieved goals
    if newly_achieved:
        logger.info(
            f"[GOAL] Newly achieved goals: {newly_achieved} "
            f"(total: {len(achieved_goals)}/{len(scope_goals)})"
        )


__all__ = [
    "check_goal_completion",
    "get_completion_criteria",
    "update_achieved_goals",
]
