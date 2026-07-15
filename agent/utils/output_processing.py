"""Output processing utilities for tool results.

This module provides utilities for processing tool output before sending
to the LLM, including smart truncation, error extraction, and formatting.

The goal is to maximize useful information within context limits while
ensuring critical data (errors, results) is never lost to truncation.

Design update (2026): Truncation thresholds increased significantly to prevent
wasteful file-reading loops. Modern LLMs handle 128K+ tokens easily - aggressive
truncation costs more (in loops) than it saves.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Import centralized configuration
from .truncation_config import (
    get_threshold_for_type,
    get_effective_limit,
    DEFAULT_HEAD_CHARS,
    DEFAULT_TAIL_CHARS,
    DEFAULT_TOTAL_LIMIT,
)

# Error patterns to extract (case-insensitive)
ERROR_PATTERNS = [
    r"error",
    r"failed",
    r"denied",
    r"refused",
    r"timeout",
    r"exception",
    r"warning",
    r"permission",
    r"not found",
    r"no such",
    r"cannot",
    r"unable",
    r"invalid",
    r"fatal",
]

# Noise patterns to strip (save context space)
NOISE_PATTERNS = [
    # Kali welcome message (box-drawing version)
    r"┏━.*?Message from Kali.*?┗━.*?\.hushlogin.*?\n",
    # ANSI escape codes
    r"\x1b\[[0-9;]*m",
    r"\[\d+;\d+m",
]

# Tools known to produce scan output (higher thresholds)
SCAN_TOOLS = frozenset({
    "nmap", "masscan", "gobuster", "dirb", "dirbuster", "feroxbuster",
    "nikto", "sqlmap", "wpscan", "nuclei", "ffuf", "wfuzz",
    "hydra", "medusa", "hashcat", "john",
})


# -----------------------------------------------------------------------------
# Output Type Classification
# -----------------------------------------------------------------------------


def classify_output_type(
    tool_name: str = "",
    command: str = "",
    output: str = "",
) -> str:
    """Classify output to determine appropriate truncation strategy.
    
    Different output types warrant different truncation thresholds:
    - help: Version/help text should never be truncated (causes loops)
    - scan: Scan results need high thresholds (findings are critical)
    - log: Log files benefit from tail-focused truncation
    - default: General output with standard threshold
    
    Args:
        tool_name: Name of the tool that produced the output.
        command: The command that was executed (for flag detection).
        output: The actual output text (for heuristic detection).
        
    Returns:
        Output type string: 'help', 'scan', 'log', or 'default'.
    """
    command_lower = command.lower() if command else ""
    tool_lower = tool_name.lower().split(".")[-1] if tool_name else ""
    
    # Help/version detection - highest priority
    # These outputs should never trigger file-reading loops
    help_flags = ("--help", "-h ", " -h", "--version", "-v ", " -v", "help", "version")
    if any(flag in command_lower for flag in help_flags):
        return "help"
    
    # Scan tool detection
    if tool_lower in SCAN_TOOLS:
        return "scan"
    
    # Log file detection (by command or output characteristics)
    if "log" in command_lower or ".log" in command_lower:
        return "log"
    
    # Large output with many lines is likely a log or scan
    if output and output.count("\n") > 500:
        return "log"
    
    return "default"


# -----------------------------------------------------------------------------
# Core Functions
# -----------------------------------------------------------------------------


def smart_truncate(
    text: str,
    *,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
    total_limit: int = DEFAULT_TOTAL_LIMIT,
    output_type: Optional[str] = None,
    return_was_truncated: bool = False,
) -> str | tuple[str, bool]:
    """Truncate text keeping both head and tail for context.
    
    Unlike simple [:limit] truncation which loses the end (where results
    and errors often appear), this keeps both the beginning (context)
    and end (results/errors).
    
    Includes soft margin logic: if output is within SOFT_MARGIN_PERCENT of 
    the limit, it passes through untouched. This prevents wasteful file-reading
    loops for small overages (e.g., 2500 chars vs 2000 limit).
    
    Args:
        text: The text to truncate.
        head_chars: Characters to keep from the beginning.
        tail_chars: Characters to keep from the end.
        total_limit: Maximum total characters (head + tail + separator).
            If output_type is provided, this may be overridden by type-specific threshold.
        output_type: Optional output type ('help', 'scan', 'log', 'default').
            When provided, uses type-specific thresholds from truncation_config.
        return_was_truncated: If True, return tuple (text, was_truncated).
        
    Returns:
        Truncated text with middle section replaced by indicator,
        or original text if within limit (including soft margin).
        If return_was_truncated=True, returns (text, was_truncated) tuple.
        
    Example:
        >>> smart_truncate("A" * 5000, head_chars=100, tail_chars=100)
        'AAA...AAA\\n\\n... [4800 chars truncated] ...\\n\\nAAA...AAA'
    """
    if not text:
        return ("", False) if return_was_truncated else ""
    
    text = text.strip()
    
    # Apply type-specific threshold if output_type is provided
    if output_type:
        total_limit = get_threshold_for_type(output_type)
        # Scale head/tail proportionally to new limit
        scale = total_limit / DEFAULT_TOTAL_LIMIT
        head_chars = int(DEFAULT_HEAD_CHARS * scale)
        tail_chars = int(DEFAULT_TAIL_CHARS * scale)
    
    # Apply soft margin - don't truncate if within margin of limit
    # This prevents wasteful loops for small overages
    effective_limit = get_effective_limit(total_limit)
    
    if len(text) <= effective_limit:
        return (text, False) if return_was_truncated else text
    
    # Ensure we don't exceed total limit with head + tail
    available = total_limit - 50  # Reserve space for separator
    if head_chars + tail_chars > available:
        # Proportionally reduce both
        ratio = available / (head_chars + tail_chars)
        head_chars = int(head_chars * ratio)
        tail_chars = int(tail_chars * ratio)
    
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    truncated_count = len(text) - head_chars - tail_chars
    
    separator = f"\n\n... [{truncated_count:,} chars truncated] ...\n\n"
    
    result = f"{head}{separator}{tail}"
    return (result, True) if return_was_truncated else result


def sample_head_middle_tail(
    text: str,
    *,
    total_limit: int = DEFAULT_TOTAL_LIMIT,
    return_was_sampled: bool = False,
) -> str | tuple[str, bool]:
    """Sample text using head + middle + tail segments within a fixed budget.

    This preserves early context, mid-body patterns, and terminal outcomes
    without increasing prompt size. If input is within `total_limit`, it is
    returned unchanged.

    Args:
        text: Raw text to sample.
        total_limit: Maximum output characters.
        return_was_sampled: If True, return ``(text, was_sampled)``.

    Returns:
        Sampled text, or original text when not needed. If
        ``return_was_sampled=True``, returns a tuple.
    """
    if not text:
        return ("", False) if return_was_sampled else ""

    text = text.strip()
    if len(text) <= total_limit:
        return (text, False) if return_was_sampled else text

    separator_head_middle = "\n\n... [omitted content before middle sample] ...\n\n"
    separator_middle_tail = "\n\n... [omitted content before tail sample] ...\n\n"
    separator_budget = len(separator_head_middle) + len(separator_middle_tail)

    # If limit is too small for 3-way sampling, fall back to direct truncation.
    if total_limit <= separator_budget + 3:
        truncated = text[:max(total_limit, 0)]
        return (truncated, True) if return_was_sampled else truncated

    content_budget = total_limit - separator_budget
    head_len = max(1, content_budget // 3)
    middle_len = max(1, content_budget // 3)
    tail_len = max(1, content_budget - head_len - middle_len)

    text_len = len(text)
    head_end = head_len
    tail_start = text_len - tail_len

    middle_start = (text_len // 2) - (middle_len // 2)
    min_middle_start = head_end
    max_middle_start = max(min_middle_start, tail_start - middle_len)
    middle_start = max(min_middle_start, min(middle_start, max_middle_start))
    middle_end = middle_start + middle_len

    head = text[:head_end].rstrip()
    middle = text[middle_start:middle_end].strip()
    tail = text[tail_start:].lstrip()

    sampled = (
        f"{head}"
        f"{separator_head_middle}"
        f"{middle}"
        f"{separator_middle_tail}"
        f"{tail}"
    )
    return (sampled, True) if return_was_sampled else sampled


def extract_error_lines(
    text: str,
    *,
    patterns: Optional[List[str]] = None,
    context_lines: int = 1,
    max_matches: int = 10,
) -> str:
    """Extract lines containing error-related patterns with context.
    
    Useful for surfacing errors that might be buried in verbose output.
    Returns matching lines with surrounding context for understanding.
    
    Args:
        text: The text to search.
        patterns: Regex patterns to match (defaults to ERROR_PATTERNS).
        context_lines: Lines of context before/after each match.
        max_matches: Maximum number of matches to return.
        
    Returns:
        Formatted string with matched lines and context,
        or empty string if no matches.
    """
    if not text:
        return ""
    
    patterns = patterns or ERROR_PATTERNS
    lines = text.splitlines()
    
    if not lines:
        return ""
    
    # Compile patterns for efficiency
    compiled = re.compile(
        "|".join(f"({p})" for p in patterns),
        re.IGNORECASE
    )
    
    # Find matching line indices
    match_indices: List[int] = []
    for i, line in enumerate(lines):
        if compiled.search(line):
            match_indices.append(i)
            if len(match_indices) >= max_matches:
                break
    
    if not match_indices:
        return ""
    
    # Expand to include context and deduplicate
    included: set[int] = set()
    for idx in match_indices:
        start = max(0, idx - context_lines)
        end = min(len(lines), idx + context_lines + 1)
        for i in range(start, end):
            included.add(i)
    
    # Build output with line numbers
    result_lines: List[str] = []
    sorted_indices = sorted(included)
    
    prev_idx = -2
    for idx in sorted_indices:
        # Add separator for non-contiguous sections
        if idx > prev_idx + 1 and result_lines:
            result_lines.append("  ...")
        
        # Mark matched lines with arrow
        line = lines[idx]
        is_match = idx in match_indices
        prefix = "→ " if is_match else "  "
        result_lines.append(f"{prefix}{idx + 1:4d}| {line}")
        prev_idx = idx
    
    return "\n".join(result_lines)


def strip_noise(text: str, patterns: Optional[List[str]] = None) -> str:
    """Remove noise patterns from text to save context space.
    
    Strips common noise like ANSI codes, welcome messages, etc.
    that consume context without providing useful information.
    
    Args:
        text: The text to clean.
        patterns: Regex patterns to remove (defaults to NOISE_PATTERNS).
        
    Returns:
        Cleaned text with noise removed.
    """
    if not text:
        return ""
    
    patterns = patterns or NOISE_PATTERNS
    result = text
    
    for pattern in patterns:
        try:
            result = re.sub(pattern, "", result, flags=re.DOTALL)
        except re.error:
            continue
    
    return result.strip()


def suggest_read_strategy(
    total_lines: Optional[int],
    file_size_bytes: Optional[int],
    was_truncated: bool,
    read_mode_used: Optional[str],
    artifact_path: str,
) -> str:
    """Generate actionable read strategy suggestion based on file characteristics.
    
    Decision tree:
    - <1000 lines: Suggest full read
    - 1000-5000 lines: Suggest head+tail or range-based reading
    - >5000 lines: Suggest targeted approaches (grep, tail, range)
    - Already using optimal mode: Suggest navigation tweaks
    """
    def _format_size(size_bytes: Optional[int]) -> Optional[str]:
        if size_bytes is None or size_bytes <= 0:
            return None
        units = ["B", "KB", "MB", "GB"]
        size = float(size_bytes)
        idx = 0
        while size >= 1024 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        return f"~{size:.1f} {units[idx]}"
    
    if artifact_path:
        path_lower = artifact_path.lower()
    else:
        path_lower = ""
    
    size_text = _format_size(file_size_bytes)
    
    suggestions: List[str] = []
    size_clause = f" ({size_text})" if size_text else ""
    
    # If we lack line count, fall back to size-based guidance
    if total_lines is None:
        if file_size_bytes is None:
            return (
                "Line count unknown. Run `shell.exec` with "
                f"`wc -l {artifact_path}` to size the file, then choose a read mode."
            )
        
        if file_size_bytes < 200_000:
            suggestions.append(
                f"File size {size_text}. Use `filesystem.read_file` with "
                "`read_mode='full'` to view the complete file."
            )
        elif file_size_bytes < 1_000_000:
            suggestions.append(
                f"File size {size_text}. Use `filesystem.read_file` with "
                "`read_mode='head'`, `num_lines=200` and `read_mode='tail'`, "
                "`num_lines=200` for a quick survey."
            )
        else:
            # Large file: recommend targeted approach
            if path_lower.endswith(".log") or path_lower.endswith(".txt"):
                suggestions.append(
                    f"File size {size_text}. For large logs, use "
                    "`filesystem.read_file` with `read_mode='tail'`, `num_lines=200-500`, "
                    "or narrow with `read_mode='grep'`, `grep_pattern='<PATTERN>'`."
                )
            else:
                suggestions.append(
                    f"File size {size_text}. Use `filesystem.read_file` with "
                    "`read_mode='range'`, `start_line=<offset>`, `num_lines=200` "
                    "or `read_mode='grep'` to target relevant sections."
                )
        
        if read_mode_used == "range":
            suggestions.append(
                "You used range mode. Adjust `start_line` (e.g., +200) with "
                "`read_mode='range'` to page through the file."
            )
        elif read_mode_used == "grep":
            suggestions.append(
                "You used grep mode. Refine `grep_pattern` or increase `num_lines` "
                "to widen the context around matches."
            )
        
        return "\n".join(suggestions)
    
    # Small files: encourage full read
    if total_lines < 1000:
        suggestions.append(
            f"File has {total_lines:,} lines{size_clause}. Use `filesystem.read_file` with "
            "`read_mode='full'` to view the complete file."
        )
    
    # Medium files
    elif 1000 <= total_lines <= 5000:
        if was_truncated:
            suggestions.append(
                f"File has {total_lines:,} lines{size_clause}. Use `filesystem.read_file` with "
                "`read_mode='head'`, `num_lines=100` and then `read_mode='tail'`, "
                "`num_lines=100` to capture both ends."
            )
        else:
            suggestions.append(
                f"File has {total_lines:,} lines{size_clause}. Navigate with `read_mode='range'`, "
                "`start_line=<offset>`, `num_lines=100` to focus on sections."
            )
    
    # Large files
    else:
        if path_lower.endswith(".log") or path_lower.endswith(".txt"):
            suggestions.append(
                f"File has {total_lines:,} lines{size_clause}. For recent activity, use "
                "`filesystem.read_file` with `read_mode='tail'` and `num_lines=200-500`."
            )
        elif path_lower.endswith(".json") or path_lower.endswith(".xml") or path_lower.endswith(".csv"):
            suggestions.append(
                f"File has {total_lines:,} lines{size_clause}. Use `filesystem.read_file` with "
                "`read_mode='grep'`, `grep_pattern='<PATTERN>'`, `num_lines=200` "
                "to target structured content."
            )
        else:
            suggestions.append(
                f"File has {total_lines:,} lines{size_clause}. Use `filesystem.read_file` with "
                "`read_mode='range'`, `start_line=<offset>`, `num_lines=200` to sample sections."
            )
    
    # Read-mode specific navigation hints
    if read_mode_used == "range":
        suggestions.append(
            "You used range mode. Adjust `start_line` (e.g., +200) with "
            "`read_mode='range'` to page through the file."
        )
    elif read_mode_used == "grep":
        suggestions.append(
            "You used grep mode. Refine `grep_pattern` or increase `num_lines` "
            "to widen the context around matches."
        )
    
    return "\n".join(suggestions)


class ProcessedOutput:
    """Result of processing tool output with truncation tracking."""
    
    __slots__ = ("stdout", "stderr", "artifact_hint", "was_truncated", "file_metadata")
    
    def __init__(
        self,
        stdout: str,
        stderr: str,
        artifact_hint: Optional[str],
        was_truncated: bool,
        file_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.artifact_hint = artifact_hint
        self.was_truncated = was_truncated
        self.file_metadata = file_metadata
    
    def to_tuple(self) -> Tuple[str, str, Optional[str]]:
        """Legacy tuple format for backward compatibility."""
        return (self.stdout, self.stderr, self.artifact_hint)


def process_tool_output(
    stdout: str,
    stderr: str = "",
    *,
    artifact_path: Optional[str] = None,
    total_limit: int = DEFAULT_TOTAL_LIMIT,
    include_errors: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
) -> ProcessedOutput:
    """Process tool output for LLM consumption.
    
    Applies the full processing pipeline:
    1. Strip noise (Kali messages, ANSI codes)
    2. Smart truncate (head + tail)
    3. Extract error lines
    4. Format with artifact reference
    
    Args:
        stdout: Standard output from tool.
        stderr: Standard error from tool.
        artifact_path: Path to full output artifact (if saved).
        total_limit: Maximum characters for processed output.
        include_errors: Whether to append extracted error lines.
        metadata: Optional dict containing tool-specific metadata (e.g., ToolResult.metadata).
            File metadata from fs_read operations is used to enhance artifact hints.
        
    Returns:
        ProcessedOutput with stdout, stderr, artifact_hint, and was_truncated flag.
    """
    # Clean noise from both streams
    clean_stdout = strip_noise(stdout)
    clean_stderr = strip_noise(stderr)
    
    # Calculate limits for each stream
    # Give stderr more space if it has content (errors are important)
    if clean_stderr:
        stdout_limit = int(total_limit * 0.6)
        stderr_limit = int(total_limit * 0.4)
    else:
        stdout_limit = total_limit
        stderr_limit = 0
    
    # Smart truncate with truncation tracking
    processed_stdout, stdout_truncated = smart_truncate(
        clean_stdout,
        total_limit=stdout_limit,
        return_was_truncated=True,
    )
    processed_stderr, stderr_truncated = smart_truncate(
        clean_stderr,
        total_limit=stderr_limit,
        return_was_truncated=True,
    )
    
    was_truncated = stdout_truncated or stderr_truncated
    
    # Extract file metadata if available (from filesystem.read_file operations)
    file_metadata = metadata.get("fs_read") if metadata else None
    
    # Extract and append error lines if enabled and output was truncated
    if include_errors and stdout_truncated:
        error_lines = extract_error_lines(clean_stdout, max_matches=5)
        if error_lines:
            processed_stdout += f"\n\n=== Extracted Error Lines ===\n{error_lines}"
    
    # Build artifact hint with informational (not prescriptive) language
    artifact_hint = None
    if artifact_path:
        if was_truncated:
            # Soft messaging - don't demand file reading for every truncation
            artifact_hint = f"Output condensed. Full output saved to: {artifact_path}"
        else:
            artifact_hint = f"Output saved to: {artifact_path}"
    
    return ProcessedOutput(
        stdout=processed_stdout,
        stderr=processed_stderr,
        artifact_hint=artifact_hint,
        was_truncated=was_truncated,
        file_metadata=file_metadata,
    )


# -----------------------------------------------------------------------------
# Convenience Functions
# -----------------------------------------------------------------------------


def format_output_for_prompt(
    stdout: str,
    stderr: str = "",
    artifact_path: Optional[str] = None,
) -> str:
    """Format tool output for inclusion in LLM prompt.
    
    Convenience function that processes output and formats it
    as a single string suitable for prompt inclusion.
    
    Args:
        stdout: Standard output from tool.
        stderr: Standard error from tool.
        artifact_path: Path to full output artifact.
        
    Returns:
        Formatted string ready for prompt inclusion.
    """
    processed = process_tool_output(
        stdout,
        stderr,
        artifact_path=artifact_path,
    )
    
    parts: List[str] = []
    
    if processed.stdout:
        parts.append(processed.stdout)
    
    if processed.stderr:
        parts.append(f"=== STDERR ===\n{processed.stderr}")
    
    if processed.artifact_hint:
        parts.append(f"\n[{processed.artifact_hint}]")
    
    return "\n\n".join(parts) if parts else "No output"
