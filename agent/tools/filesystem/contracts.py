"""Pydantic contracts for filesystem tools.

This module owns filesystem tool argument/result schemas and local literal
aliases only. It does not resolve host paths, execute commands, build shell
commands, create artifacts, or implement PTY behavior.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from agent.tools.schemas import CONTAINER_TRANSPORT_DESCRIPTION, ContainerTransport


class WorkspacePathArgs(BaseModel):
    """Base arguments that reference a path inside the active Kali runtime."""

    path: str = Field(
        ...,
        description=(
            "Path inside the active Kali runtime. Relative paths resolve from /workspace; "
            "absolute paths such as /, /opt, /tmp, and /workspace are allowed for "
            "container-executed filesystem tools."
        ),
    )


class FsReadArgs(WorkspacePathArgs):
    """
    Read file content from the active Kali runtime.
    
    SIMPLE USAGE (recommended):
        Just provide path - smart detection handles the rest:
        {"path": "config.yaml"}           # Reads full file if small
        {"path": "large_log.txt"}         # Auto-truncates large files with navigation hints
    
    SEARCH (find content):
        {"path": "log.txt", "search": "ERROR"}      # Find lines containing ERROR
        {"path": "log.txt", "search": "error", "case_sensitive": false}  # Case-insensitive
    
    NAVIGATION (read specific parts):
        {"path": "file.txt", "offset": 100, "num_lines": 50}  # Lines 100-149
        {"path": "file.txt", "read_mode": "head", "num_lines": 20}  # First 20 lines
        {"path": "file.txt", "read_mode": "tail", "num_lines": 50}  # Last 50 lines
    
    The tool automatically:
        - Reads small files (<1000 lines) fully
        - Shows first 200 lines for medium files with navigation hints
        - Shows last 100 lines for large files (logs typically have recent data at end)
    
    Path semantics: relative paths resolve from /workspace; absolute paths are
    interpreted inside the active Kali runtime.
    """

    # === SIMPLE PARAMETERS (most common use) ===
    
    search: Optional[str] = Field(
        None,
        description=(
            "Search pattern to find matching lines (auto-triggers grep mode). "
            "Supports regex. Example: 'ERROR|WARN' or 'connection.*failed'."
        ),
    )
    offset: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "Start reading from this line number (1-indexed). "
            "Simpler alternative to read_mode='range' + start_line."
        ),
    )
    num_lines: Optional[int] = Field(
        None,
        ge=1,
        description="Number of lines to read. Used with head/tail/range modes or with offset.",
    )
    case_sensitive: bool = Field(
        True,
        description="For search: when false, match case-insensitively.",
    )
    include_line_numbers: bool = Field(
        False,
        description="Prefix output lines with line numbers for easier reference.",
    )
    
    # === ADVANCED PARAMETERS (usually not needed) ===
    
    read_mode: Optional["ReadMode"] = Field(
        None,
        description=(
            "Advanced: Explicit read strategy (full, head, tail, range, grep). "
            "Usually auto-detected - only specify if you need to override."
        ),
    )
    start_line: Optional[int] = Field(
        None,
        ge=1,
        description="Advanced: Starting line for range mode. Prefer 'offset' for simplicity.",
    )
    grep_pattern: Optional[str] = Field(
        None,
        description="Advanced: Explicit grep pattern. Prefer 'search' for simplicity.",
    )
    encoding: Optional[str] = Field(
        "utf-8",
        description="Advanced: Text encoding. Set to null for binary files (returns base64).",
    )
    auto_detect_encoding: bool = Field(
        False,
        description=(
            "Phase 6: Auto-detect file encoding using BOM/chardet heuristics. "
            "When true, ignores the 'encoding' parameter and detects automatically."
        ),
    )
    hex_dump: bool = Field(
        False,
        description=(
            "Phase 6: For binary files, return a formatted hex dump instead of base64. "
            "Useful for inspecting file headers and structure."
        ),
    )
    include_checksums: bool = Field(
        False,
        description=(
            "Phase 6: Compute and include MD5/SHA256 checksums in metadata. "
            "Useful for verifying file integrity."
        ),
    )
    max_bytes: int = Field(
        200_000,
        ge=1,
        le=2_000_000,
        description="Advanced: Maximum bytes to read. Usually not needed - smart detection handles this.",
    )
    start_byte: int = Field(
        0,
        ge=0,
        description="Advanced: Byte offset for binary reads. For text files, use 'offset' instead.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )

    def model_post_init(self, __context: Any) -> None:
        """Apply alias mappings for simplified parameters.
        
        Maps:
            - search -> grep_pattern (triggers grep mode)
            - offset -> start_line (triggers range mode)
        """
        # Map 'search' to 'grep_pattern' if search is provided and grep_pattern is not
        if self.search and not self.grep_pattern:
            object.__setattr__(self, "grep_pattern", self.search)
        
        # Map 'offset' to 'start_line' if offset is provided and start_line is not
        if self.offset and not self.start_line:
            object.__setattr__(self, "start_line", self.offset)


class FsWriteArgs(WorkspacePathArgs):
    """
    Write content to a file in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Content size validated before write
        - Overwrite protection via 'safe' mode
    
    Reliability Features (Phase 5):
        - atomic: Uses temp file + rename pattern for crash safety
        - backup: Creates .bak file before overwriting
    
    PTY Transport:
        - Command: cat > {path} << 'EOF'\\n{content}\\nEOF
        - Heredoc prevents command injection
        - User can see what's being written (audit trail)
        - Content size limits enforced before PTY execution
    
    Examples:
        {"path": "config.yaml", "content": "key: value"}  # Auto-select
        {"path": "script.sh", "content": "#!/bin/bash\\necho test", "transport": "pty"}  # PTY for visibility
        {"path": "important.conf", "content": "...", "backup": true}  # Create .bak before overwrite
    """

    content: str = Field(..., description="Full file contents to write.")
    encoding: str = Field(
        "utf-8",
        description="Encoding used to persist the content. Binary data should be base64-encoded separately.",
    )
    create_parents: bool = Field(
        True,
        description="Create parent directories automatically when they do not exist.",
    )
    overwrite: Literal["safe", "overwrite"] = Field(
        "safe",
        description="'safe' refuses to clobber existing non-identical files; 'overwrite' replaces them.",
    )
    backup: bool = Field(
        False,
        description=(
            "Create a .bak backup of the existing file before overwriting. "
            "Useful for important files where you might need to restore previous content."
        ),
    )
    atomic: bool = Field(
        True,
        description=(
            "Use atomic write pattern (temp file + rename). "
            "Ensures file is either fully written or unchanged on failure. "
            "Recommended for important files. Disable only for performance-critical bulk writes."
        ),
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsAppendArgs(WorkspacePathArgs):
    """
    Append content to a file in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Content size validated before append
    
    PTY Transport:
        - Command: cat >> {path} << 'EOF'\\n{content}\\nEOF
        - Heredoc prevents command injection
        - User can see what's being appended
    
    Examples:
        {"path": "log.txt", "content": "New log entry\\n"}  # Auto-select
        {"path": "results.txt", "content": "Finding: XSS\\n", "transport": "pty"}  # PTY for visibility
    """

    content: str = Field(..., description="Content to append to the target file.")
    encoding: str = Field(
        "utf-8",
        description="Encoding used to append the content.",
    )
    create_if_missing: bool = Field(
        False,
        description="When true, create the file if it does not already exist (with empty initial content).",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsDeleteArgs(WorkspacePathArgs):
    """
    Delete files/directories in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Irreversible operation (use with caution)
    
    PTY Transport:
        - Command: rm -rf {path} (if recursive) or rm {path}
        - User can see what's being deleted (transparency)
        - Provides audit trail for destructive operations
    
    Examples:
        {"path": "temp.txt"}  # Auto-select
        {"path": "old_results/", "recursive": true, "transport": "pty"}  # PTY for verification
    """

    recursive: bool = Field(
        False,
        description="Allow recursive deletion of directories. Must remain false for safety unless explicitly required.",
    )
    force: bool = Field(
        False,
        description="Ignore missing targets and permission errors where possible.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsMakeDirArgs(WorkspacePathArgs):
    """
    Create a directory in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
    
    PTY Transport:
        - Command: mkdir -p {path}
        - -p flag creates parent directories
        - User can see directory creation
    
    Examples:
        {"path": "results/scan1"}  # Auto-select
        {"path": "artifacts/nmap", "transport": "pty"}  # PTY for visibility
    """

    parents: bool = Field(
        True,
        description="Create intermediate directories if they do not exist.",
    )
    exist_ok: bool = Field(
        False,
        description="When true, do not treat an existing directory as an error.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsListArgs(WorkspacePathArgs):
    """
    List directory contents in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Result count limited to prevent resource exhaustion
    
    PTY Transport:
        - Command: ls -la {path} or find {path} (depending on options)
        - Shows permissions, timestamps, sizes
        - Good for debugging: "What's in this directory?"
    
    Examples:
        {"path": "results"}  # Auto-select
        {"path": "artifacts", "recursive": true, "transport": "pty"}  # PTY for visual inspection
    """

    recursive: bool = Field(
        False,
        description="List entries recursively when true.",
    )
    include_hidden: bool = Field(
        False,
        description="Include hidden files (dot-prefixed) in the response.",
    )
    include_globs: Optional[List[str]] = Field(
        None,
        description="Glob patterns that files must match to be included.",
    )
    exclude_globs: Optional[List[str]] = Field(
        None,
        description="Glob patterns that files must not match.",
    )
    max_results: int = Field(
        2000,
        ge=1,
        le=20_000,
        description="Upper bound on the number of entries to return to prevent runaway traversals.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsMoveArgs(BaseModel):
    """
    Move/rename files in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem paths
        - Relative paths resolve from /workspace
        - Overwrite protection available
    
    PTY Transport:
        - Command: mv {src} {dest}
        - User can verify src → dest mapping
        - Overwrite behavior visible in command
    
    Examples:
        {"src": "temp.txt", "dest": "final.txt"}  # Auto-select
        {"src": "results/old", "dest": "results/new", "transport": "pty"}  # PTY for verification
    """

    src: str = Field(
        ...,
        description="Source path inside the active Kali runtime. Relative paths resolve from /workspace.",
    )
    dest: str = Field(
        ...,
        description="Destination path inside the active Kali runtime. Relative paths resolve from /workspace.",
    )
    overwrite: bool = Field(
        False,
        description="Allow replacing an existing destination when true.",
    )
    create_dest_parents: bool = Field(
        True,
        description="Create destination parent directories when they do not exist.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsCopyArgs(FsMoveArgs):
    """
    Copy files/directories in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem paths
        - Relative paths resolve from /workspace
        - Overwrite protection available
    
    PTY Transport:
        - Command: cp -r {src} {dest}
        - -r flag for recursive copy
        - User can verify src → dest mapping
    
    Examples:
        {"src": "template.txt", "dest": "instance.txt"}  # Auto-select
        {"src": "backup/", "dest": "restore/", "transport": "pty"}  # PTY for verification
    """

    preserve_permissions: bool = Field(
        True,
        description="Attempt to preserve file permissions and metadata where supported.",
    )


class FsStatArgs(WorkspacePathArgs):
    """
    Get file/directory metadata in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Symlink resolution controlled by follow_symlinks
    
    PTY Transport:
        - Command: stat {path}
        - Shows detailed metadata (size, permissions, timestamps)
        - Cross-platform: output format may vary
    
    Examples:
        {"path": "results.txt"}  # Auto-select
        {"path": "scan_output.xml", "transport": "pty"}  # PTY for detailed view
    """

    follow_symlinks: bool = Field(
        False,
        description="Resolve symlinks when true; otherwise report on the link itself.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsFindArgs(BaseModel):
    """
    Search for files/directories in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Result count limited to prevent resource exhaustion
    
    PTY Transport:
        - Command: find {path} -name {pattern} (with additional options)
        - Shows matching paths with full details
        - Good for debugging search patterns
    
    Examples:
        {"filename_glob": "*.xml"}  # Auto-select
        {"path": "results", "filename_glob": "*.txt", "transport": "pty"}  # PTY for visual results
    """

    path: str = Field(
        ".",
        description=(
            "Directory inside the active Kali runtime to begin searching from. "
            "Relative paths resolve from /workspace; absolute paths such as /, /opt, "
            "/tmp, and /workspace are allowed."
        ),
    )
    filename_glob: Optional[str] = Field(
        None,
        description="Glob pattern to match file names (e.g., '*.py').",
    )
    include_globs: Optional[List[str]] = Field(
        None,
        description="Additional glob patterns that matches must satisfy.",
    )
    exclude_globs: Optional[List[str]] = Field(
        None,
        description="Glob patterns that, when matched, exclude a path from the results.",
    )
    max_depth: Optional[int] = Field(
        None,
        ge=0,
        le=25,
        description="Optional recursion depth limit (0 lists only the starting directory).",
    )
    min_depth: int = Field(
        0,
        ge=0,
        description="Minimum depth relative to the starting directory before reporting matches.",
    )
    file_types: Optional[List[Literal["file", "directory", "symlink"]]] = Field(
        None,
        description="Limit results to certain file types.",
    )
    follow_symlinks: bool = Field(
        False,
        description="Follow symbolic links during traversal when true.",
    )
    max_results: int = Field(
        500,
        ge=1,
        le=5_000,
        description="Maximum number of matches to return.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsEditLinesArgs(WorkspacePathArgs):
    """
    Edit specific lines in a file without rewriting entire content.
    
    This is the preferred way to modify files when you know which lines to change.
    Much more efficient than read_file + write_file for targeted edits.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - Line numbers validated (must be >= 1)
        - Backup option available for safety
    
    Modes:
        - replace: Replace lines start_line to end_line with new_content
        - insert: Insert new_content before start_line (end_line ignored)
        - delete: Delete lines start_line to end_line (new_content ignored)
    
    Line numbers are 1-indexed (first line is line 1).
    
    PTY Transport:
        - Delete mode: sed -i '{start},{end}d' {path}
        - Replace/insert modes use Python implementation (heredoc complexity)
    
    Examples:
        Replace lines 10-15:
        {"path": "config.yaml", "start_line": 10, "end_line": 15, 
         "new_content": "new config here", "mode": "replace"}
        
        Insert before line 5:
        {"path": "script.sh", "start_line": 5, "new_content": "# New comment",
         "mode": "insert"}
        
        Delete lines 20-25:
        {"path": "log.txt", "start_line": 20, "end_line": 25, "mode": "delete"}
        
        Single line replace (line 42):
        {"path": "code.py", "start_line": 42, "new_content": "    return True"}
    """

    start_line: int = Field(
        ...,
        ge=1,
        description="First line to edit (1-indexed). For insert mode, content is added before this line.",
    )
    end_line: Optional[int] = Field(
        None,
        ge=1,
        description="Last line to edit (inclusive). If omitted, defaults to start_line (single line edit).",
    )
    new_content: str = Field(
        "",
        description="New content to insert or use as replacement. Ignored for delete mode.",
    )
    mode: "EditMode" = Field(
        "replace",
        description="Edit mode: 'replace' (default), 'insert', or 'delete'.",
    )
    backup: bool = Field(
        False,
        description="Create .bak backup before editing. Recommended for important files.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )


class FsSearchTextArgs(BaseModel):
    """
    Search file contents for text patterns in the active Kali runtime.
    
    Security Guardrails:
        - Container-executed filesystem path
        - Relative paths resolve from /workspace
        - File size limits prevent resource exhaustion
        - Result count limited
    
    PTY Transport:
        - Command: grep -r {pattern} {path} (with case sensitivity flags)
        - Shows matching lines with context
        - Good for debugging search patterns
        - Results may be large (truncation possible)
    
    Examples:
        {"query": "password", "path": "config"}  # Auto-select
        {"query": "CVE-.*-.*", "use_regex": true, "transport": "pty"}  # PTY for visual results
    """

    query: str = Field(..., description="Literal text or regular expression to search for.")
    path: str = Field(
        ".",
        description=(
            "Directory or file inside the active Kali runtime to scope the search. "
            "Relative paths resolve from /workspace; absolute paths are allowed."
        ),
    )
    recursive: bool = Field(
        True,
        description="Search subdirectories recursively when true.",
    )
    case_sensitive: bool = Field(
        True,
        description="Perform a case-sensitive match when true.",
    )
    use_regex: bool = Field(
        False,
        description="Interpret the query as a regular expression when true.",
    )
    include_globs: Optional[List[str]] = Field(
        None,
        description="Only inspect files whose names match one of these glob patterns.",
    )
    exclude_globs: Optional[List[str]] = Field(
        None,
        description="Exclude files whose names match one of these glob patterns.",
    )
    max_file_bytes: int = Field(
        500_000,
        ge=1,
        le=5_000_000,
        description="Skip files larger than this size in bytes.",
    )
    max_results: int = Field(
        200,
        ge=1,
        le=2_000,
        description="Maximum number of matches to return.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )
    context_before: int = Field(
        2,
        ge=0,
        le=20,
        description="Number of context lines to include before each match.",
    )
    context_after: int = Field(
        2,
        ge=0,
        le=20,
        description="Number of context lines to include after each match.",
    )


class FilesystemEntry(BaseModel):
    """Metadata describing a file or directory entry."""

    path: str = Field(
        ...,
        description="Path to the entry; workspace-local entries may be relative, while other Kali runtime entries may be absolute.",
    )
    type: Literal["file", "directory", "symlink"] = Field(
        ..., description="Type of entry that was discovered."
    )
    size_bytes: Optional[int] = Field(
        None, description="Size in bytes when the entry is a regular file."
    )
    modified_ts: Optional[float] = Field(
        None,
        description="POSIX timestamp of the last modification in seconds.",
    )


class FsReadResult(BaseModel):
    """Response payload for fs_read operations."""

    content: Optional[str] = Field(
        None,
        description="File content returned to the caller (text or base64 depending on encoding request).",
    )
    bytes_read: int = Field(
        ..., description="Actual number of bytes processed before truncation or EOF."
    )
    truncated: bool = Field(
        False,
        description="True when the result was truncated because it exceeded max_bytes.",
    )
    encoding: Optional[str] = Field(
        None, description="Encoding used for the returned content when textual."
    )
    total_lines: Optional[int] = Field(
        None, description="Total line count in the file when available."
    )
    lines_read: Optional[int] = Field(
        None,
        description="Number of lines returned for line-oriented reads (head/tail/range/grep).",
    )
    read_mode_used: Optional[str] = Field(
        None,
        description="Resolved read mode that was applied for this request.",
    )
    line_range: Optional[Tuple[int, int]] = Field(
        None,
        description="Start and end line numbers for range-based reads when applicable.",
    )
    line_evidence: List[str] = Field(
        default_factory=list,
        description="Exact line-numbered excerpts returned by read modes that expose source line locators.",
    )
    # Phase 6 additions
    encoding_detected: Optional[str] = Field(
        None,
        description="Phase 6: Auto-detected encoding when auto_detect_encoding=true.",
    )
    encoding_confidence: Optional[float] = Field(
        None,
        description="Phase 6: Confidence score (0-1) for auto-detected encoding.",
    )
    md5_checksum: Optional[str] = Field(
        None,
        description="Phase 6: MD5 checksum when include_checksums=true.",
    )
    sha256_checksum: Optional[str] = Field(
        None,
        description="Phase 6: SHA256 checksum when include_checksums=true.",
    )
    detected_file_type: Optional[str] = Field(
        None,
        description="Phase 6: Detected file type from magic bytes (e.g., 'PNG image', 'PDF document').",
    )
    line_ending: Optional[str] = Field(
        None,
        description="Phase 6: Detected line ending style (lf, crlf, cr, mixed).",
    )


class FsListResult(BaseModel):
    """Directory listing payload."""

    entries: List[FilesystemEntry] = Field(
        default_factory=list,
        description="Collection of directory entries that matched the request.",
    )
    truncated: bool = Field(
        False,
        description="Indicates whether entries were omitted because the max_results limit was reached.",
    )


class FsFindResult(BaseModel):
    """Result payload for fs_find operations."""

    matches: List[FilesystemEntry] = Field(
        default_factory=list,
        description="Ordered list of paths matching the search criteria.",
    )
    truncated: bool = Field(
        False,
        description="True when additional matches were omitted after reaching max_results.",
    )


class TextMatch(BaseModel):
    """Individual match returned by fs_search_text."""

    path: str = Field(
        ...,
        description="File containing the match; may be workspace-relative or an absolute Kali runtime path.",
    )
    line: int = Field(..., ge=1, description="Line number where the match begins.")
    column: Optional[int] = Field(
        None, ge=1, description="Optional column index for the first matched character."
    )
    snippet: str = Field(
        ..., description="Extract of the file around the match, including requested context lines."
    )


class FsSearchTextResult(BaseModel):
    """Aggregated results for fs_search_text."""

    matches: List[TextMatch] = Field(
        default_factory=list,
        description="Ordered list of text matches discovered during the search.",
    )
    truncated: bool = Field(
        False,
        description="True when additional matches were omitted after reaching max_results.",
    )


class FsEditResult(BaseModel):
    """Result payload for filesystem.edit_lines operations."""

    path: str = Field(..., description="Path that was edited inside the active Kali runtime.")
    mode: str = Field(..., description="Edit mode that was applied: replace, insert, or delete.")
    start_line: int = Field(..., ge=1, description="First line affected (1-indexed).")
    end_line: int = Field(..., ge=1, description="Last line affected (1-indexed).")
    lines_affected: int = Field(
        ..., ge=0, description="Number of original lines that were affected."
    )
    new_line_count: int = Field(
        ..., ge=0, description="Total line count in the file after editing."
    )
    backup_created: bool = Field(
        False, description="Whether a backup file was created before editing."
    )
    diff_preview: Optional[str] = Field(
        None, description="Short preview of the changes made."
    )


class FsMutationResult(BaseModel):
    """Generic status payload for filesystem mutations."""

    path: str = Field(..., description="Primary path affected by the operation inside the active Kali runtime.")
    action: Literal[
        "created",
        "updated",
        "appended",
        "deleted",
        "moved",
        "copied",
        "edited",
        "metadata",
    ] = Field(..., description="High-level description of the mutation that occurred.")
    message: Optional[str] = Field(
        None,
        description="Optional human-readable status message with additional details.",
    )
    bytes_changed: Optional[int] = Field(
        None,
        description="Signed number of bytes added or removed when applicable.",
    )
    extra: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured metadata specific to the operation.",
    )


ReadMode = Literal["full", "head", "tail", "range", "grep"]

EditMode = Literal["replace", "insert", "delete"]
"""
Edit operation modes for surgical line-level file editing:
- "replace": Replace lines start_line to end_line with new_content
- "insert": Insert new_content before start_line (end_line ignored)
- "delete": Delete lines start_line to end_line (new_content ignored)
"""
