"""Generic CLI execution outcome resolution for tool success classification.

This module distinguishes *informational* non-zero exit codes (partial or
negative findings that still mean the tool ran correctly) from *hard failures*
(parse errors, usage failures, missing binaries) using transport-agnostic
stdout/stderr evidence. Individual tools declare informational codes; hard
failure detection stays centralized here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Set

_HARD_CLI_EXIT_CODES = frozenset({126, 127})

# Lines that indicate the CLI never completed meaningful work.
_HARD_FAILURE_LINE = re.compile(
    r"(?i)^(?:[\w./+-]+:\s*)?"
    r"(?:can't|cannot|usage:|command not found|not found:|no such file|"
    r"invalid argument|invalid option|unknown option|unrecognized option|"
    r"permission denied|operation not permitted|error:|fatal error:|"
    r"syntax error|missing (?:required )?argument|bad address|"
    r"unable to resolve|failed to (?:resolve|parse|open|bind|connect))"
)

# Some tools emit a single-line ``tool: message`` failure without the keywords
# above; ``can't parse`` is the canonical fping-style example but the pattern is
# intentionally broad for ``cannot``/``unable`` variants.
_HARD_FAILURE_PHRASE = re.compile(
    r"(?i)(?:can't parse|cannot parse|unable to parse|invalid target|"
    r"invalid address|name or service not known)"
)

_SHELL_COMMAND_NOT_FOUND = re.compile(
    r"(?i)^(?:[\w./+-]+:\s*)?(?:(?:line\s+)?\d+:\s*)?"
    r"(?:[\w./+-]+:\s*)?(?:command\s+not\s+found|not\s+found)(?::\s*[\w./+-]+)?$"
)


def detect_hard_cli_failure(*, stdout: str = "", stderr: str = "") -> bool:
    """Return True when combined CLI output indicates a hard execution failure."""
    combined = _normalize_cli_text(stdout, stderr)
    if not combined:
        return False

    for line in combined.splitlines():
        text = line.strip()
        if not text:
            continue
        if _HARD_FAILURE_LINE.search(text):
            return True
        if _HARD_FAILURE_PHRASE.search(text):
            return True
        if _SHELL_COMMAND_NOT_FOUND.search(text):
            return True
    return False


def is_hard_cli_exit_code(exit_code: int) -> bool:
    """Return True for shell-level command invocation failures."""
    return int(exit_code) in _HARD_CLI_EXIT_CODES


def resolve_execution_success(
    *,
    exit_code: int,
    informational_exit_codes: Set[int] | frozenset[int],
    stdout: str = "",
    stderr: str = "",
    parsed_metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Resolve whether a completed CLI invocation succeeded at the tool layer.

    Resolution order:
    1. Explicit ``execution_outcome`` from ``parse_output`` metadata when present.
    2. Hard CLI failure evidence in stdout/stderr always fails the run.
    3. Exit code ``0`` succeeds.
    4. Exit codes listed in ``informational_exit_codes`` succeed (partial /
       negative findings with a completed run).
    5. All other non-zero exit codes fail.
    """
    metadata = parsed_metadata if isinstance(parsed_metadata, Mapping) else {}
    explicit = str(metadata.get("execution_outcome") or "").strip().lower()
    if explicit == "failed":
        return False
    if explicit in {"succeeded", "informational", "success"}:
        return True

    if detect_hard_cli_failure(stdout=stdout, stderr=stderr):
        return False

    if is_hard_cli_exit_code(exit_code):
        return False

    if exit_code == 0:
        return True

    if exit_code in informational_exit_codes:
        return True

    return False


def _normalize_cli_text(stdout: str, stderr: str) -> str:
    parts = [str(stdout or ""), str(stderr or "")]
    return "\n".join(part.replace("\r\n", "\n").replace("\r", "\n") for part in parts if part)


__all__ = [
    "detect_hard_cli_failure",
    "is_hard_cli_exit_code",
    "resolve_execution_success",
]
