"""Common deterministic helpers for Knowledge web-path behavior.

This module owns backend Knowledge web identity helpers shared by projection,
query, and temporary adapter code. It does not read execution payloads,
artifacts, databases, or invoke adapter dispatch.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from runtime_shared.semantic.web_common import normalize_url


def build_web_origin_key(url: Any) -> str:
    """Return canonical web origin key derived from URL input."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"
