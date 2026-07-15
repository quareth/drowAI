"""Workspace-scoped file reader with bounded line, byte, and grep modes.

This module implements ``filesystem.read_file``. It resolves paths inside the
task workspace, reads text or binary content with explicit bounds, and emits
structured metadata for downstream compression and reasoning.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsReadArgs, FsReadResult
from ._error_helpers import build_llm_error
from ._helpers import (
    DEFAULT_LINE_LIMIT,
    build_cat_command,
    build_grep_command,
    build_head_command,
    build_sed_range_command,
    build_tail_command,
    build_tool_result,
    create_output_artifact,
    resolve_read_mode,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)
from ._smart_read import (
    get_line_count_python,
    resolve_read_mode_smart,
)
# Phase 6: Cross-platform implementations
from ._platform import (
    read_head_python,
    read_tail_python,
    read_range_python,
    read_grep_python,
    detect_encoding,
    compute_checksums,
    generate_hex_dump,
    detect_line_ending,
)

logger = logging.getLogger(__name__)


def _get_line_count(target: Path) -> Optional[int]:
    """Return total line count using pure Python (cross-platform).
    
    This function delegates to the shared helper for consistency.
    """
    return get_line_count_python(target)


def _read_head(target: Path, num_lines: int, encoding: str) -> Tuple[str, int]:
    """Read first N lines using pure Python (Phase 6 cross-platform)."""
    return read_head_python(target, num_lines, encoding)


def _read_tail(target: Path, num_lines: int, encoding: str) -> Tuple[str, int]:
    """Read last N lines using pure Python (Phase 6 cross-platform)."""
    return read_tail_python(target, num_lines, encoding)


def _read_range(target: Path, start_line: int, num_lines: int, encoding: str) -> Tuple[str, int]:
    """Read line range using pure Python (Phase 6 cross-platform)."""
    return read_range_python(target, start_line, num_lines, encoding)


def _read_grep(
    target: Path, pattern: str, case_sensitive: bool, encoding: str, max_lines: int
) -> Tuple[str, int]:
    """Search for pattern using pure Python (Phase 6 cross-platform)."""
    return read_grep_python(target, pattern, case_sensitive, encoding, max_lines)


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    lines = content.splitlines()
    return "\n".join(f"{start_line + idx}| {line}" for idx, line in enumerate(lines))


_LINE_EVIDENCE_RE = re.compile(r"^\d{1,9}(?::|\|)\s?.+")


def _extract_line_evidence(content: str, *, limit: int = 5) -> List[str]:
    """Return exact numbered excerpts already emitted by read modes."""

    evidence: List[str] = []
    seen: set[str] = set()
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line or not _LINE_EVIDENCE_RE.match(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        evidence.append(line)
        if len(evidence) >= limit:
            break
    return evidence


class FsReadTool(BaseTool):
    """
    Read file content from workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `cat {path}` in PTY session (visible to users)
    - None: Executor auto-selects based on availability
    
    PTY routing is handled by the executor layer, which converts the tool
    call to a shell command and executes it in the agent's PTY session.
    
    Progressive Reading Modes:
        - full: read entire file (text path, up to safety cap)
        - head: first N lines (num_lines)
        - tail: last N lines (num_lines)
        - range: specific slice (start_line + num_lines)
        - grep: pattern match with optional case sensitivity
    
    PTY Use Cases:
        - Debugging: "Why can't I read this file?" (see permissions, path errors)
        - Verification: User wants to see file contents in real-time
        - Troubleshooting: Agent needs to diagnose file access issues
    
    Security Guardrails:
        - All paths validated with resolve_workspace_path_safe()
        - Workspace isolation enforced (cannot read outside task workspace)
        - Path traversal blocked (.., absolute paths)
        - Symlinks resolved and validated
        - Size limits enforced (max_bytes parameter)
    
    PTY Command Equivalents:
        - full:  cat {path}
        - head:  head -n {num_lines|100} {path}
        - tail:  tail -n {num_lines|100} {path}
        - range: sed -n '{start_line|1},{end_line}p' {path}
        - grep:  grep -n [-i] {pattern} {path}
        - with include_line_numbers: ... | awk '{print NR"| "$0}'
    
    Limitations:
        - Binary mode (encoding=None) is not executed via PTY; executor falls back
        - PTY output matches direct execution semantics but is text-oriented
    
    Examples:
        {"path": "results.txt"}  # Auto-select
        {"path": "config.yaml", "transport": "pty"}  # Force PTY for debugging
        {"path": "log.txt", "read_mode": "tail", "num_lines": 100}  # Tail mode
        {"path": "scan.txt", "read_mode": "range", "start_line": 50, "num_lines": 25}  # Range mode
        {"path": "results.txt", "read_mode": "grep", "grep_pattern": "ERROR|CRITICAL"}  # Grep mode
    """

    args_model = FsReadArgs

    def is_success_exit_code(
        self,
        exit_code: int,
        args: Any,
        *,
        stdout: str = "",
        stderr: str = "",
        parsed_metadata: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Treat grep no-match as a completed read while preserving hard failures."""

        if exit_code == 1 and isinstance(args, FsReadArgs):
            read_mode = resolve_read_mode(
                read_mode=args.read_mode,
                grep_pattern=args.grep_pattern,
                start_line=args.start_line,
                num_lines=args.num_lines,
                start_byte=args.start_byte,
                max_bytes=args.max_bytes,
                encoding=args.encoding,
            )
            if read_mode == "grep":
                if str(stderr or "").strip():
                    return False
                from ..execution_outcome import detect_hard_cli_failure

                if not detect_hard_cli_failure(stdout=stdout, stderr=stderr):
                    return True
        return super().is_success_exit_code(
            exit_code,
            args,
            stdout=stdout,
            stderr=stderr,
            parsed_metadata=parsed_metadata,
        )

    def build_command(self, args: FsReadArgs) -> List[str]:
        """Build a PTY-safe command for reading file content."""

        if args.encoding is None:
            raise ValueError("Binary reads are not supported via PTY transport.")

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        read_mode = resolve_read_mode(
            read_mode=args.read_mode,
            grep_pattern=args.grep_pattern,
            start_line=args.start_line,
            num_lines=args.num_lines,
            start_byte=args.start_byte,
            max_bytes=args.max_bytes,
            encoding=args.encoding,
        )

        if read_mode == "byte":
            raise ValueError("Byte-mode reads are not supported via PTY transport.")

        path_str = str(target)
        if read_mode == "full":
            return build_cat_command(path_str)
        if read_mode == "head":
            return build_head_command(path_str, args.num_lines or DEFAULT_LINE_LIMIT)
        if read_mode == "tail":
            return build_tail_command(path_str, args.num_lines or DEFAULT_LINE_LIMIT)
        if read_mode == "range":
            start_line = args.start_line or 1
            num_lines = args.num_lines or DEFAULT_LINE_LIMIT
            end_line = start_line + num_lines - 1
            return build_sed_range_command(path_str, start_line, end_line)
        if read_mode == "grep":
            return build_grep_command(path_str, args.grep_pattern or "", args.case_sensitive)
        raise ValueError(f"Unsupported read mode: {read_mode}")

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsReadArgs,
    ) -> Dict[str, object]:
        """Parse read output into structured metadata."""

        read_mode = resolve_read_mode(
            read_mode=args.read_mode,
            grep_pattern=args.grep_pattern,
            start_line=args.start_line,
            num_lines=args.num_lines,
            start_byte=args.start_byte,
            max_bytes=args.max_bytes,
            encoding=args.encoding,
        )
        lines_read = len(stdout.splitlines()) if stdout else 0
        line_range: Optional[Tuple[int, int]] = None
        if read_mode == "range":
            start_line = args.start_line or 1
            end_line = start_line + max(lines_read, 1) - 1
            line_range = (start_line, end_line)

        result = FsReadResult(
            content=stdout,
            bytes_read=len(stdout.encode(args.encoding or "utf-8", errors="replace")),
            truncated=False,
            encoding=args.encoding,
            total_lines=None,
            lines_read=lines_read or None,
            read_mode_used=read_mode,
            line_range=line_range,
            line_evidence=(
                _extract_line_evidence(stdout)
                if read_mode == "grep" or args.include_line_numbers
                else []
            ),
        )
        return {"fs_read": result.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsReadArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist large read output as artifacts."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_read", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_read_errors", timestamp))
        return created

    def run(self, args: FsReadArgs) -> ToolResult:
        start = time.time()
        workspace = workspace_root()

        try:
            target: Path = resolve_workspace_path_safe(args.path, workspace=workspace)
        except ValueError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="path_out_of_workspace",
                    path=args.path,
                    workspace=workspace,
                    message=str(exc),
                ),
                metadata={"error": "path_out_of_workspace"},
            )

        if not target.exists():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_found",
                    path=args.path,
                    workspace=workspace,
                    message=f"File '{args.path}' does not exist.",
                ),
                metadata={"error": "not_found"},
                exit_code=1,
            )

        if target.is_dir():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="is_directory",
                    path=args.path,
                    workspace=workspace,
                    message=f"Path '{args.path}' is a directory.",
                ),
                metadata={"error": "is_directory"},
                exit_code=1,
            )

        # Use smart read mode detection (Phase 4 enhancement)
        read_mode, smart_result = resolve_read_mode_smart(
            target,
            read_mode=args.read_mode,
            grep_pattern=args.grep_pattern,
            start_line=args.start_line,
            num_lines=args.num_lines,
            start_byte=args.start_byte,
            max_bytes=args.max_bytes,
            encoding=args.encoding,
            use_smart_detection=True,
        )
        
        # Use smart detection's total_lines if available, otherwise count manually
        total_lines: Optional[int] = None
        if smart_result is not None:
            total_lines = smart_result.total_lines
        if total_lines is None:
            total_lines = _get_line_count(target)
        
        # Smart detection may suggest a specific num_lines
        effective_num_lines = args.num_lines
        if smart_result is not None and smart_result.num_lines is not None and args.num_lines is None:
            effective_num_lines = smart_result.num_lines

        encoding_used: Optional[str] = args.encoding
        content = ""
        bytes_read = 0
        truncated = False
        lines_read: Optional[int] = None
        read_mode_used = read_mode
        line_number_start = 1
        
        # Phase 6: Encoding auto-detection
        encoding_detected: Optional[str] = None
        encoding_confidence: Optional[float] = None
        if args.auto_detect_encoding:
            try:
                detection_result = detect_encoding(target)
                encoding_detected = detection_result.encoding
                encoding_confidence = detection_result.confidence
                encoding_used = encoding_detected
                logger.debug(
                    f"Auto-detected encoding for {args.path}: {encoding_detected} "
                    f"(confidence: {encoding_confidence:.2f}, method: {detection_result.method})"
                )
            except Exception as e:
                logger.warning(f"Encoding detection failed for {args.path}: {e}, using default utf-8")
                encoding_used = "utf-8"
        
        # Phase 6: Checksums
        md5_checksum: Optional[str] = None
        sha256_checksum: Optional[str] = None
        if args.include_checksums:
            try:
                md5_checksum, sha256_checksum = compute_checksums(target)
            except Exception as e:
                logger.warning(f"Checksum computation failed for {args.path}: {e}")
        
        # Phase 6: Line ending detection (for text files)
        line_ending_detected: Optional[str] = None
        detected_file_type: Optional[str] = None

        def _read_bytes_legacy(stderr_hint: Optional[str] = None) -> ToolResult:
            nonlocal truncated, bytes_read, encoding_used, content, read_mode_used
            try:
                with target.open("rb") as handle:
                    if args.start_byte:
                        handle.seek(args.start_byte)
                    data = handle.read(args.max_bytes + 1)
            except OSError as exc:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="io_error",
                        path=args.path,
                        workspace=workspace,
                        message=f"Failed to read '{args.path}': {exc}",
                    ),
                    metadata={"error": "io_error"},
                    exit_code=1,
                )

            truncated = len(data) > args.max_bytes
            if truncated:
                data = data[: args.max_bytes]

            bytes_read = len(data)
            
            # Phase 6: Detect file type from magic bytes
            nonlocal detected_file_type, line_ending_detected
            try:
                from ._platform import _detect_file_type, detect_line_ending as _detect_le
                detected_file_type = _detect_file_type(data)
                line_ending_detected = _detect_le(data)
            except Exception:
                pass
            
            if args.encoding:
                encoding_used = args.encoding
                content_local = data.decode(args.encoding, errors="replace")
            else:
                encoding_used = None
                # Phase 6: Use hex dump or base64 based on request
                if args.hex_dump:
                    content_local = generate_hex_dump(data, offset=args.start_byte, max_lines=64)
                else:
                    content_local = base64.b64encode(data).decode("ascii")

            if encoding_used:
                stdout_local = content_local
                if truncated:
                    stdout_local += (
                        f"\n\n[TRUNCATED - Read {bytes_read} bytes from {args.path}, file continues...]"
                    )
            else:
                if args.hex_dump:
                    stdout_local = f"Binary file: {args.path} ({bytes_read} bytes)\n"
                    if detected_file_type:
                        stdout_local += f"Detected type: {detected_file_type}\n"
                    if md5_checksum:
                        stdout_local += f"MD5: {md5_checksum}\n"
                    if sha256_checksum:
                        stdout_local += f"SHA256: {sha256_checksum}\n"
                    stdout_local += f"\nHex dump:\n{content_local}"
                else:
                    stdout_local = (
                        f"Read {bytes_read} bytes from {args.path} (binary, base64 encoded in metadata)"
                    )

            # Build enhanced metadata for Phase 6
            metadata = self.parse_output(content_local, "", 0, args)
            if "fs_read" in metadata:
                metadata["fs_read"]["md5_checksum"] = md5_checksum
                metadata["fs_read"]["sha256_checksum"] = sha256_checksum
                metadata["fs_read"]["detected_file_type"] = detected_file_type
                metadata["fs_read"]["line_ending"] = line_ending_detected
                metadata["fs_read"]["encoding_detected"] = encoding_detected
                metadata["fs_read"]["encoding_confidence"] = encoding_confidence
            
            artifacts = self.create_artifacts(content_local, args)
            return build_tool_result(
                success=True,
                start=start,
                stdout=stdout_local,
                metadata=metadata,
                artifacts=artifacts,
                stderr=stderr_hint or "",
            )

        try:
            if read_mode == "head":
                encoding_used = args.encoding or "utf-8"
                content, lines_read = _read_head(target, effective_num_lines or 100, encoding_used)
                line_number_start = 1
            elif read_mode == "tail":
                encoding_used = args.encoding or "utf-8"
                content, lines_read = _read_tail(target, effective_num_lines or 100, encoding_used)
                if total_lines is not None and lines_read is not None:
                    line_number_start = max(total_lines - lines_read + 1, 1)
            elif read_mode == "range":
                encoding_used = args.encoding or "utf-8"
                start_line = args.start_line or 1
                num_lines_for_range = effective_num_lines or 100
                content, lines_read = _read_range(target, start_line, num_lines_for_range, encoding_used)
                line_number_start = start_line
            elif read_mode == "grep":
                encoding_used = args.encoding or "utf-8"
                pattern = args.grep_pattern or ""
                content, lines_read = _read_grep(
                    target,
                    pattern=pattern,
                    case_sensitive=args.case_sensitive,
                    encoding=encoding_used,
                    max_lines=effective_num_lines or 200,
                )
            elif read_mode == "full":
                max_full_bytes = 10_000_000
                limit = min(args.max_bytes, max_full_bytes)
                try:
                    with target.open("rb") as handle:
                        data = handle.read(limit + 1)
                except OSError as exc:
                    return build_tool_result(
                        success=False,
                        start=start,
                        stderr=f"Failed to read '{args.path}': {exc}",
                        metadata={"error": "io_error"},
                        exit_code=1,
                    )
                truncated = len(data) > limit
                if truncated:
                    data = data[:limit]
                encoding_used = args.encoding or "utf-8"
                content = data.decode(encoding_used, errors="replace")
                bytes_read = len(data)
                lines_read = len(content.splitlines())
            else:
                read_mode_used = "byte"
                return _read_bytes_legacy()
        except Exception as exc:
            read_mode_used = "byte"
            return _read_bytes_legacy(stderr_hint=f"Line-mode read failed: {exc}")

        if args.include_line_numbers and content:
            content = _add_line_numbers(content, start_line=line_number_start)

        if lines_read is None and content:
            lines_read = len(content.splitlines())

        if bytes_read == 0 and content:
            bytes_read = len(content.encode(encoding_used or "utf-8", errors="replace"))
        
        # Phase 6: Detect line endings for text content
        if content and not line_ending_detected:
            try:
                line_ending_detected = detect_line_ending(content.encode(encoding_used or "utf-8"))
            except Exception:
                pass

        summary_parts = []
        if lines_read is not None:
            summary = f"Read {lines_read} lines from {args.path} (mode: {read_mode_used}"
            if total_lines is not None:
                summary += f", total lines: {total_lines}"
            summary += ")"
            summary_parts.append(summary)
        
        # Phase 6: Include encoding detection info if auto-detected
        if encoding_detected and encoding_confidence:
            summary_parts.append(f"Encoding: {encoding_detected} (confidence: {encoding_confidence:.0%})")
        
        # Include smart detection suggestion when available
        if smart_result is not None and smart_result.suggestion:
            summary_parts.append(smart_result.suggestion)
        elif total_lines is not None and lines_read is not None and lines_read < total_lines:
            # Fallback to generic suggestion if smart detection didn't provide one
            summary_parts.append(
                f"Showing {lines_read} of {total_lines} lines. Use read_mode='range' with start_line to read more."
            )

        stdout = content
        if summary_parts:
            summary_text = " ".join(summary_parts)
            stdout = f"{content}\n\n{summary_text}" if content else summary_text

        if truncated and read_mode_used == "full":
            stdout += f"\n\n[TRUNCATED - Read {bytes_read} bytes from {args.path}, file continues...]"

        metadata = self.parse_output(content, "", 0, args)
        
        # Phase 6: Enrich metadata with additional info
        if "fs_read" in metadata:
            metadata["fs_read"]["md5_checksum"] = md5_checksum
            metadata["fs_read"]["sha256_checksum"] = sha256_checksum
            metadata["fs_read"]["detected_file_type"] = detected_file_type
            metadata["fs_read"]["line_ending"] = line_ending_detected
            metadata["fs_read"]["encoding_detected"] = encoding_detected
            metadata["fs_read"]["encoding_confidence"] = encoding_confidence
        
        artifacts = self.create_artifacts(content, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )
