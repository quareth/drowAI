"""Shared dependency-neutral contracts for connection catalog and target resolution.

This module owns the registry exception and pure validation primitives only; it
does not load manifests, read environment values, own operation matrices, or
compose registered targets.
"""

from __future__ import annotations

import re


class OperationRegistryError(ValueError):
    """Raised when an operation/provider pair is not code-owned and supported."""


def _valid_base_path(path: str) -> bool:
    """Return whether a declared URL base path is safe to compose."""

    if not path.startswith("/") or "\\" in path or "//" in path:
        return False
    if re.search(r"%(?:2e|2f|5c)", path, flags=re.IGNORECASE):
        return False
    return not any(segment in {".", ".."} for segment in path.split("/"))
