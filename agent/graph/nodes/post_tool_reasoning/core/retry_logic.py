"""Capability-agnostic retry decision logic.

This module contains pure functions for managing retry attempts and
the canonical retry metadata key constants used by graph builders and
routing predicates. No capability-specific logic, no state mutation
(returns new dicts).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

# Constants
MAX_RETRIES = 4
RETRY_METADATA_KEY = "retry_tracking"
RETRY_SUGGESTED_METADATA_KEY = "retry_suggested"
FAILURE_DETECTED_METADATA_KEY = "failure_detected"


def retry_suggested(metadata: Mapping[str, Any]) -> bool:
    """Return True when PTR metadata marks the last tool failure as retryable.

    Canonical contract:
    - ``metadata["failure_detected"]=True`` indicates PTR detected a tool
      failure on the last execution.
    - ``metadata["retry_suggested"]=True`` is the advisory flag PTR sets
      when that failure should be retried.

    Both flags must be present and ``True`` for the result to be a retry.
    Advisory drift (``retry_suggested=True`` without ``failure_detected``)
    is intentionally treated as non-retry so a stale advisory cannot
    override a fresh non-failure observation.
    """
    return (
        bool(metadata.get(FAILURE_DETECTED_METADATA_KEY))
        and metadata.get(RETRY_SUGGESTED_METADATA_KEY) is True
    )


def get_retry_count(metadata: Dict[str, Any]) -> int:
    """Extract retry count from metadata.
    
    Pure function with no side effects.
    
    Args:
        metadata: Metadata dictionary from state
        
    Returns:
        Current retry count (0 if no retries yet)
    """
    retry_data = metadata.get(RETRY_METADATA_KEY, {}) or {}
    return retry_data.get("count", 0)


def can_retry(retry_count: int, max_retries: int = MAX_RETRIES) -> bool:
    """Check if retry budget is available.
    
    Pure function with no side effects.
    
    Args:
        retry_count: Current retry attempt count
        max_retries: Maximum allowed retries
        
    Returns:
        True if retry is allowed, False if budget exhausted
    """
    return retry_count < max_retries


def increment_retry_count(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Increment retry count and return updated metadata.
    
    Pure function - returns new dict without mutating input.
    
    Args:
        metadata: Original metadata dictionary
        
    Returns:
        New metadata dictionary with incremented retry count
    """
    # Create new dict to avoid mutation
    new_metadata = dict(metadata)
    
    # Get existing retry data
    retry_data = dict(new_metadata.get(RETRY_METADATA_KEY, {}) or {})
    
    # Increment count (cap at max)
    current_count = retry_data.get("count", 0)
    retry_data["count"] = min(current_count + 1, MAX_RETRIES)
    
    # Store updated retry data
    new_metadata[RETRY_METADATA_KEY] = retry_data
    
    return new_metadata


__all__ = [
    "MAX_RETRIES",
    "RETRY_METADATA_KEY",
    "RETRY_SUGGESTED_METADATA_KEY",
    "FAILURE_DETECTED_METADATA_KEY",
    "retry_suggested",
    "get_retry_count",
    "can_retry",
    "increment_retry_count",
]

