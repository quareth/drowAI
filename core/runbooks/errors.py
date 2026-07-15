"""Runbook-specific exceptions for loading, parsing, and validation failures."""

from __future__ import annotations


class RunbookError(Exception):
    """Base class for runbook lifecycle failures."""


class RunbookParseError(RunbookError):
    """Raised when runbook markdown or frontmatter cannot be parsed."""


class RunbookLoadError(RunbookError):
    """Raised when a runbook asset cannot be loaded successfully."""


class RunbookValidationError(RunbookError):
    """Raised when parsed runbook metadata or content fails validation."""


__all__ = [
    "RunbookError",
    "RunbookLoadError",
    "RunbookParseError",
    "RunbookValidationError",
]
