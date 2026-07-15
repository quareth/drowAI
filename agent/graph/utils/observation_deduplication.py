"""Observation deduplication and progress detection utilities (DR.6)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from backend.services.metrics.utils import safe_inc

logger = logging.getLogger(__name__)


def _safe_record(metric_name: str, value: float) -> None:
    """Safely record a metric value, handling missing metrics module."""
    try:
        from backend.services.metrics.utils import safe_record

        safe_record(metric_name, value)
    except Exception:
        pass  # Metrics module may not be available in all contexts


def hash_observation(observation: Dict[str, Any]) -> str:
    """
    Create stable hash of observation content.

    Normalizes observation to ignore noise (timestamps, order)
    and hashes semantic content only.

    Args:
        observation: Observation dict from synthesis node (synthesized_output)

    Returns:
        SHA256 hex digest
    """
    # Extract semantic content only
    normalized = {
        "summary": str(observation.get("summary", "")).strip().lower(),
        "key_findings": sorted(
            [str(f).strip().lower() for f in observation.get("key_findings", [])]
        ),
        "vulnerabilities": sorted(
            [str(v).strip().lower() for v in observation.get("vulnerabilities", [])]
        ),
        "next_actions": sorted(
            [str(a).strip().lower() for a in observation.get("next_actions", [])]
        ),
    }

    # Serialize and hash
    content = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


def calculate_observation_similarity(
    obs1: Optional[Dict[str, Any]],
    obs2: Optional[Dict[str, Any]],
) -> float:
    """
    Calculate similarity between two observations (0.0-1.0).

    Uses Jaccard similarity on key fields (key_findings, vulnerabilities, next_actions).

    Args:
        obs1: First observation dict
        obs2: Second observation dict

    Returns:
        Similarity score (0.0-1.0), where 1.0 is identical
    """
    if not obs1 or not obs2:
        return 0.0

    # Compare key fields
    fields = ["key_findings", "vulnerabilities", "next_actions"]
    matches = 0
    total = 0

    for field in fields:
        list1 = set(str(item).strip().lower() for item in obs1.get(field, []))
        list2 = set(str(item).strip().lower() for item in obs2.get(field, []))

        if not list1 and not list2:
            continue  # Both empty, skip

        # Jaccard similarity
        intersection = len(list1 & list2)
        union = len(list1 | list2)

        if union > 0:
            matches += intersection
            total += union

    return matches / total if total > 0 else 0.0


def score_observation_progress(
    observation: Dict[str, Any],
    previous_observation: Optional[Dict[str, Any]],
) -> float:
    """
    Score observation for new information (0.0-1.0).

    Low score if mostly duplicates previous observations.
    High score if significant new findings.

    Args:
        observation: Current observation dict
        previous_observation: Previous observation dict (if any)

    Returns:
        Progress score (0.0-1.0)
    """
    if not previous_observation:
        # First observation is always new
        return 1.0

    # Calculate similarity
    similarity = calculate_observation_similarity(observation, previous_observation)

    # Progress score is inverse of similarity
    # High similarity (0.9+) → low progress (0.1)
    # Low similarity (0.0) → high progress (1.0)
    progress_score = 1.0 - similarity

    # Boost score if there are new findings
    key_findings = observation.get("key_findings", [])
    vulnerabilities = observation.get("vulnerabilities", [])
    has_new_content = len(key_findings) > 0 or len(vulnerabilities) > 0

    if has_new_content and similarity < 0.5:
        # Significant new content with low similarity = high progress
        progress_score = min(progress_score + 0.2, 1.0)

    return progress_score


def check_observation_duplicate(
    observation: Dict[str, Any],
    observation_hashes: List[str],
    last_observation: Optional[Dict[str, Any]] = None,
) -> tuple[bool, float, str]:
    """
    Check if observation is duplicate or near-duplicate.

    Args:
        observation: Current observation dict
        observation_hashes: List of previous observation hashes
        last_observation: Previous observation dict (for similarity check)

    Returns:
        Tuple of (is_duplicate: bool, similarity: float, hash: str)
    """
    # Hash current observation
    current_hash = hash_observation(observation)

    # Check for exact duplicate
    if len(observation_hashes) > 0 and current_hash == observation_hashes[-1]:
        logger.info("[DEDUPE] Exact duplicate observation detected")
        safe_inc("observation_duplicate_skipped")
        return True, 1.0, current_hash

    # Check for near-duplicate (>90% similarity)
    similarity = 0.0
    if last_observation:
        similarity = calculate_observation_similarity(observation, last_observation)
        if similarity > 0.9:
            logger.info(
                f"[DEDUPE] Near-duplicate observation detected (similarity: {similarity:.2f})"
            )
            _safe_record("observation_similarity", similarity)

    return False, similarity, current_hash


def detect_tool_output_change(
    tool_id: str,
    current_output: str,
    previous_outputs: Dict[str, str],
) -> tuple[bool, str]:
    """
    Detect if tool output has meaningful changes.

    Args:
        tool_id: Tool identifier
        current_output: Current tool output
        previous_output: Previous tool output for this tool (if any)

    Returns:
        Tuple of (has_meaningful_change: bool, change_summary: str)
    """
    if tool_id not in previous_outputs:
        return True, "First execution of this tool"

    previous_output = previous_outputs[tool_id]

    # Normalize outputs (remove timestamps, whitespace differences)
    current_normalized = _normalize_tool_output(current_output)
    previous_normalized = _normalize_tool_output(previous_output)

    # Check if identical after normalization
    if current_normalized == previous_normalized:
        return False, "Output identical to previous execution (only timestamp/noise differences)"

    # Check if only minor differences (e.g., line count changed slightly)
    if _is_minor_difference(current_normalized, previous_normalized):
        return False, "Only minor differences detected (likely noise)"

    return True, "Significant changes detected in tool output"


def _normalize_tool_output(output: str) -> str:
    """Normalize tool output by removing timestamps and noise."""
    import re

    # Remove common timestamp patterns
    output = re.sub(r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.\d]*[Z+-]?\d*", "", output)
    output = re.sub(r"\[.*?\]", "", output)  # Remove bracketed prefixes

    # Normalize whitespace
    output = " ".join(output.split())

    return output.strip().lower()


def _is_minor_difference(current: str, previous: str) -> bool:
    """Check if differences are minor (e.g., only line count or small text changes)."""
    # If outputs are very similar in length and content
    length_diff = abs(len(current) - len(previous))
    max_length = max(len(current), len(previous))

    if max_length == 0:
        return True

    # If length difference is <5% and content is >95% similar
    length_ratio = length_diff / max_length
    if length_ratio < 0.05:
        # Check character-level similarity
        from difflib import SequenceMatcher

        similarity = SequenceMatcher(None, current, previous).ratio()
        return similarity > 0.95

    return False


__all__ = [
    "hash_observation",
    "calculate_observation_similarity",
    "score_observation_progress",
    "check_observation_duplicate",
    "detect_tool_output_change",
]