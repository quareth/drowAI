"""Pure canonical-key helpers for semantic finding identities.

This module defines deterministic token and finding-key builders that are safe
to import from runtime-image code and backend adapters.
"""

from __future__ import annotations

import re

_TOKEN_PATTERN = re.compile(r"[^a-z0-9._/-]+")


def sanitize_finding_token(value: str) -> str:
    """Normalize token text for finding-key compatibility."""
    return _TOKEN_PATTERN.sub("-", str(value or "").strip().lower()).strip("-")


def build_finding_vulnerability_key(
    *,
    subject_key: str,
    detector_id: str,
) -> str:
    """Build canonical key for one vulnerability finding identity."""
    normalized_subject_key = str(subject_key or "").strip().lower()
    if not normalized_subject_key:
        raise ValueError("subject_key cannot be empty")
    normalized_detector_id = sanitize_finding_token(detector_id)
    if not normalized_detector_id:
        raise ValueError("detector_id cannot be empty")
    return f"finding.vulnerability:{normalized_subject_key}:{normalized_detector_id}"
