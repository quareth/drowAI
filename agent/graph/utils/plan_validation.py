"""Plan validation and merge utilities for deep reasoning.

This module provides functions to validate plan quality, merge plans intelligently,
and reject low-quality plan updates.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def validate_plan_quality(plan: List[str]) -> Dict[str, Any]:
    """Check plan quality metrics.
    
    Evaluates plan for:
    - Generic step count (pattern matching "step 1", "step 2", etc.)
    - Minimum plan length (>= 2 steps)
    - Specificity (non-generic steps)
    
    Args:
        plan: List of plan step strings
        
    Returns:
        Dict with keys: valid (bool), generic_count (int), specificity_score (float)
    """
    if not plan:
        return {
            "valid": False,
            "generic_count": 0,
            "specificity_score": 0.0,
        }
    
    # Check minimum length
    if len(plan) < 2:
        return {
            "valid": False,
            "generic_count": 0,
            "specificity_score": 0.0,
        }
    
    # Check for generic step pattern
    generic_pattern = re.compile(r"^step\s+\d+$", re.IGNORECASE)
    generic_count = 0
    
    for step in plan:
        if isinstance(step, str):
            step_normalized = step.strip().lower()
            if generic_pattern.match(step_normalized):
                generic_count += 1
            # Also penalize very short steps
            elif len(step.strip()) < 10:
                generic_count += 0.5
    
    # Calculate specificity score (0.0 = all generic, 1.0 = all specific)
    total_steps = len(plan)
    specific_count = total_steps - generic_count
    specificity_score = specific_count / total_steps if total_steps > 0 else 0.0
    
    # Plan is valid if >50% specific and has minimum length
    is_valid = specificity_score > 0.5 and total_steps >= 2
    
    return {
        "valid": is_valid,
        "generic_count": int(generic_count),
        "specificity_score": specificity_score,
    }


def merge_plans(old_plan: List[str], new_plan: List[str]) -> List[str]:
    """Merge new plan with old plan intelligently.
    
    Preserves specific steps from old plan if new plan is generic.
    Replaces old steps only if new steps are more specific.
    
    Strategy:
    - If new plan is high quality (>50% specific), use it
    - If new plan is low quality but old plan is high quality, merge:
      - Keep specific steps from old plan
      - Replace generic steps with new plan steps if they're more specific
      - Preserve order where possible
    
    Args:
        old_plan: Existing plan steps
        new_plan: New plan steps to merge
        
    Returns:
        Merged plan list
    """
    if not old_plan:
        return new_plan if new_plan else []
    
    if not new_plan:
        return old_plan
    
    # Validate both plans
    old_quality = validate_plan_quality(old_plan)
    new_quality = validate_plan_quality(new_plan)
    
    # If new plan is high quality, use it
    if new_quality["specificity_score"] > 0.7:
        logger.debug(
            f"[CACHE] Merging plans: new plan is high quality "
            f"(specificity: {new_quality['specificity_score']:.2f}), using new plan"
        )
        return new_plan
    
    # If old plan is high quality and new is low, prefer old with selective updates
    if old_quality["specificity_score"] > 0.7 and new_quality["specificity_score"] < 0.5:
        logger.debug(
            f"[CACHE] Merging plans: old plan is high quality, preserving it "
            f"(old: {old_quality['specificity_score']:.2f}, "
            f"new: {new_quality['specificity_score']:.2f})"
        )
        return old_plan
    
    # Merge strategy: combine both, preferring specific steps
    merged = []
    generic_pattern = re.compile(r"^step\s+\d+$", re.IGNORECASE)
    
    # Create sets of specific steps from both plans
    old_specific = [
        step
        for step in old_plan
        if isinstance(step, str)
        and not generic_pattern.match(step.strip().lower())
        and len(step.strip()) >= 10
    ]
    
    new_specific = [
        step
        for step in new_plan
        if isinstance(step, str)
        and not generic_pattern.match(step.strip().lower())
        and len(step.strip()) >= 10
    ]
    
    # Start with specific steps from old plan
    merged.extend(old_specific)
    
    # Add new specific steps that aren't duplicates
    for step in new_specific:
        # Check for similarity (simple check: same first 30 chars)
        step_prefix = step[:30].lower().strip()
        is_duplicate = any(
            existing[:30].lower().strip() == step_prefix for existing in merged
        )
        
        if not is_duplicate:
            merged.append(step)
    
    # If merged is empty, fall back to new plan
    if not merged:
        merged = new_plan
    
    logger.debug(
        f"[CACHE] Merged plans: {len(old_plan)} old + {len(new_plan)} new -> {len(merged)} merged"
    )
    
    return merged


def should_reject_plan_update(old_plan: List[str], new_plan: List[str]) -> bool:
    """Check if new plan is lower quality than old plan.
    
    Rejects plan updates that degrade plan quality, such as:
    - New plan is >50% generic and old plan was <50% generic
    - New plan is significantly shorter and less specific
    
    Args:
        old_plan: Existing plan steps
        new_plan: New plan steps to evaluate
        
    Returns:
        True if update should be rejected, False otherwise
    """
    if not old_plan:
        return False  # No old plan to compare, accept new
    
    if not new_plan:
        return True  # Empty new plan should be rejected
    
    old_quality = validate_plan_quality(old_plan)
    new_quality = validate_plan_quality(new_plan)
    
    # Reject if new plan is degraded and old was good
    if (
        new_quality["specificity_score"] < 0.5
        and old_quality["specificity_score"] >= 0.5
    ):
        logger.info(
            f"[CACHE] Rejecting plan update: new plan is degraded "
            f"(old specificity: {old_quality['specificity_score']:.2f}, "
            f"new specificity: {new_quality['specificity_score']:.2f})"
        )
        return True
    
    # Reject if new plan is significantly shorter and less specific
    if (
        len(new_plan) < len(old_plan) * 0.5
        and new_quality["specificity_score"] < old_quality["specificity_score"]
    ):
        logger.info(
            f"[CACHE] Rejecting plan update: new plan is too short and less specific "
            f"(old: {len(old_plan)} steps, new: {len(new_plan)} steps)"
        )
        return True
    
    return False


__all__ = [
    "validate_plan_quality",
    "merge_plans",
    "should_reject_plan_update",
]

