"""Canonical skill identifier validation shared across package and handoff boundaries."""

from __future__ import annotations

import re
from typing import Any


MAX_SKILL_ID_CHARACTERS = 64
SKILL_ID_PATTERN = r"^[a-z](?:[a-z0-9_]|-[a-z0-9_])*$"
SKILL_ID_RE = re.compile(SKILL_ID_PATTERN)


def normalize_skill_id(value: Any) -> str:
    """Return one canonical skill identifier or raise for invalid input."""

    skill_id = str(value or "").strip().lower()
    if (
        len(skill_id) > MAX_SKILL_ID_CHARACTERS
        or not SKILL_ID_RE.fullmatch(skill_id)
    ):
        raise ValueError("skill identifier is not canonical")
    return skill_id


__all__ = [
    "MAX_SKILL_ID_CHARACTERS",
    "SKILL_ID_PATTERN",
    "SKILL_ID_RE",
    "normalize_skill_id",
]
