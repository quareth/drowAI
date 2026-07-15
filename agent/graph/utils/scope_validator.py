"""Plan validation against user scope boundaries and goals."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.services.metrics.utils import safe_inc

from .scope_parser import UserScope

logger = logging.getLogger(__name__)


def validate_plan_against_scope(
    plan: List[str],
    scope: UserScope,
) -> Dict[str, Any]:
    """
    Validate that plan respects scope boundaries.

    Args:
        plan: List of plan steps
        scope: User scope with boundaries

    Returns:
        Dict with keys: valid (bool), violations (List[str])
    """
    violations = []

    if not plan:
        return {"valid": True, "violations": []}  # Empty plan is valid

    # Combine plan steps into single text for pattern matching
    plan_text = " ".join(plan).lower()

    # Check boundary violations
    boundary_violations = _check_boundary_violations(plan_text, scope.boundaries)
    violations.extend(boundary_violations)

    # Check goal coverage
    goal_violations = _check_goal_coverage(plan_text, scope.goals)
    violations.extend(goal_violations)

    if violations:
        logger.warning(f"[SCOPE] Plan validation failed: {len(violations)} violations")
        safe_inc("plan_scope_violations")
        for violation in violations:
            logger.debug(f"[SCOPE] Violation: {violation}")

    return {"valid": len(violations) == 0, "violations": violations}


def _check_boundary_violations(plan_text: str, boundaries: List[str]) -> List[str]:
    """Check for boundary violations in plan text."""
    violations = []

    if "no_exploitation" in boundaries:
        # Check for exploitation keywords
        exploitation_keywords = ["exploit", "metasploit", "attack", "penetrate", "gain access", "payload"]
        found_keywords = [kw for kw in exploitation_keywords if kw in plan_text]
        if found_keywords:
            violations.append(
                f"Plan includes exploitation ({', '.join(found_keywords)}) but user did not request it"
            )

    if "no_brute_force" in boundaries:
        # Check for brute force keywords
        brute_keywords = ["brute", "force", "crack", "password", "dictionary", "wordlist"]
        found_keywords = [kw for kw in brute_keywords if kw in plan_text]
        if found_keywords:
            violations.append(
                f"Plan includes brute force ({', '.join(found_keywords)}) but user forbade it"
            )

    if "no_dos" in boundaries:
        # Check for DoS keywords
        dos_keywords = ["dos", "denial of service", "flood", "overload", "overwhelm"]
        found_keywords = [kw for kw in dos_keywords if kw in plan_text]
        if found_keywords:
            violations.append(
                f"Plan includes DoS attacks ({', '.join(found_keywords)}) but user forbade it"
            )

    if "no_data_modification" in boundaries:
        # Check for data modification keywords
        mod_keywords = ["modify", "change", "delete", "write", "update", "alter"]
        found_keywords = [kw for kw in mod_keywords if kw in plan_text]
        if found_keywords:
            violations.append(
                f"Plan includes data modification ({', '.join(found_keywords)}) but user forbade it"
            )

    return violations


def _check_goal_coverage(plan_text: str, goals: List[str]) -> List[str]:
    """Check if plan addresses all goals."""
    violations = []

    if not goals:
        return violations  # No goals to check

    # Map goals to keywords that should appear in plan
    goal_keywords = {
        "find_vulnerable_services": ["vulnerabilit", "vuln", "security issue", "cve"],
        "identify_hosts": ["host", "machine", "device", "discover", "scan network"],
        "identify_open_ports": ["port", "scan port", "open port"],
        "identify_services": ["service", "banner", "version", "enumeration"],
    }

    for goal in goals:
        keywords = goal_keywords.get(goal, [])
        if keywords:
            # Check if any keyword appears in plan
            found = any(kw in plan_text for kw in keywords)
            if not found:
                violations.append(f"Plan does not address goal: {goal}")

    return violations


__all__ = ["validate_plan_against_scope"]

