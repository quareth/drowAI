"""Shared helpers for workspace-scoped filesystem tools and filesystem PTY behavior."""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.utils.workspace_helpers import (
    ensure_workspace_directories,
    resolve_workspace_path,
)

from ..schemas import ToolResult

logger = logging.getLogger(__name__)

ARTIFACT_SIZE_THRESHOLD_BYTES = 10 * 1024
DEFAULT_LINE_LIMIT = 100
DEFAULT_LIST_DIR_MAX_RESULTS = 2000
MAX_TEXT_MATCHES = 200
MAX_FIND_MATCHES = 500
# Default heredoc marker - may be modified if content collision detected
DEFAULT_HEREDOC_MARKER = "DROWAI_EOF"
BYTE_READ_MODE_THRESHOLD = 2_000_000

# Smart read mode thresholds
SMALL_FILE_LINE_THRESHOLD = 1000       # Files <= this: full read
MEDIUM_FILE_LINE_THRESHOLD = 5000      # Files <= this: head with suggestion
SMART_DEFAULT_HEAD_LINES = 200         # Default lines to show for medium files
SMART_DEFAULT_TAIL_LINES = 100         # Default lines for large file tail

# PTY safety caps mirror schema upper bounds where available.
PTY_READ_MAX_BYTES = 2_000_000
PTY_READ_MAX_LINES = 10_000
PTY_LIST_MAX_RESULTS = 20_000
PTY_FIND_MAX_RESULTS = 5_000
PTY_SEARCH_MAX_RESULTS = 2_000


def workspace_root() -> Path:
    """Return the resolved workspace root and ensure core directories exist."""

    root = Path(resolve_workspace_path()).resolve()
    ensure_workspace_directories(str(root))
    return root


def resolve_workspace_path_safe(relative_path: str, *, workspace: Optional[Path] = None) -> Path:
    """Resolve a user-supplied path into the workspace, rejecting escapes."""

    if os.path.isabs(relative_path):
        raise ValueError("Absolute paths are not allowed; use workspace-relative paths.")

    workspace_root_path = workspace or workspace_root()
    candidate = (workspace_root_path / relative_path).resolve(strict=False)

    try:
        candidate.relative_to(workspace_root_path)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise ValueError(
            f"Path '{relative_path}' escapes the workspace boundary."
        ) from exc

    return candidate


def resolve_filesystem_command_path(
    path: Any,
    *,
    resolve_container_path: Callable[[str], str],
) -> str:
    """Resolve a filesystem tool path for container command execution.

    Filesystem tools inspect or mutate the active Kali runtime filesystem.
    Relative paths keep the existing `/workspace` default, while absolute
    POSIX paths are preserved so the model can intentionally target `/`,
    `/opt`, `/tmp`, `/workspace`, and similar in-container locations.
    """

    raw = "" if path is None else str(path)
    if "\x00" in raw:
        raise ValueError("Path contains a null byte")

    value = raw.strip()
    if value in ("", "."):
        return resolve_container_path(".")

    normalized = value.replace("\\", "/")
    if posixpath.isabs(normalized):
        try:
            return resolve_container_path(normalized)
        except ValueError:
            return posixpath.normpath(normalized)

    return resolve_container_path(normalized)


def describe_filesystem_scope(path: Any) -> str:
    """Return the Kali-runtime path label used in empty-result messages."""
    raw = "" if path is None else str(path)
    value = raw.strip()
    if value in ("", "."):
        return "/workspace"

    normalized = value.replace("\\", "/")
    if posixpath.isabs(normalized):
        return posixpath.normpath(normalized)

    relative = posixpath.normpath(normalized)
    if relative in ("", "."):
        return "/workspace"
    return posixpath.normpath(posixpath.join("/workspace", relative))


def to_workspace_relative(path: Path, workspace: Optional[Path] = None) -> str:
    """Convert an absolute path inside the workspace back to a relative POSIX string."""

    workspace_root_path = workspace or workspace_root()
    try:
        return path.relative_to(workspace_root_path).as_posix()
    except ValueError:
        return path.as_posix()


def build_tool_result(
    *,
    success: bool,
    start: float,
    stdout: str = "",
    stderr: str = "",
    metadata: Optional[Dict[str, object]] = None,
    exit_code: Optional[int] = None,
    artifacts: Optional[List[str]] = None,
) -> ToolResult:
    """Create a ``ToolResult`` with consistent defaults."""

    duration = time.time() - start
    return ToolResult(
        success=success,
        exit_code=0 if success else (exit_code or -1),
        stdout=stdout,
        stderr=stderr,
        artifacts=artifacts or [],
        metadata=metadata or {},
        execution_time=duration,
    )


def _coerce_int_with_bounds(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Coerce a value to int and clamp it to a safe inclusive range."""
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def validate_fs_read_params_for_pty(
    params: Dict[str, Any],
    *,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """Validate PTY read-file parameters without changing current failure semantics."""

    def _log(level: str, message: str) -> None:
        if logger is None:
            return
        try:
            logger.log_operation(level, message)
        except Exception:
            pass

    def _safe_inc_metric(name: str) -> None:
        try:
            from backend.services.metrics.utils import safe_inc

            safe_inc(name)
        except Exception:
            pass

    supported_modes = {None, "", "full", "head", "tail", "range", "grep", "byte"}
    read_mode = params.get("read_mode") or "full"
    if read_mode not in supported_modes:
        _safe_inc_metric("executor_pty_read_invalid_params")
        _log("WARNING", f"[PTY] Unsupported read_mode={read_mode} requested")
        raise ValueError(f"Unsupported read_mode: {read_mode}")

    updated_params = dict(params)
    if updated_params.get("search") and not updated_params.get("grep_pattern"):
        updated_params["grep_pattern"] = updated_params.get("search")
    if updated_params.get("offset") and not updated_params.get("start_line"):
        updated_params["start_line"] = updated_params.get("offset")
    if read_mode == "grep" and not updated_params.get("grep_pattern"):
        _safe_inc_metric("executor_pty_read_invalid_params")
        _log("WARNING", "[PTY] grep_pattern required for read_mode=grep")
        raise ValueError("grep_pattern is required for read_mode=grep")

    if read_mode == "byte":
        start_byte = updated_params.get("start_byte", 0)
        max_bytes = updated_params.get("max_bytes", 200_000)
        try:
            start_byte_int = int(start_byte)
            max_bytes_int = int(max_bytes)
        except Exception:
            _safe_inc_metric("executor_pty_read_invalid_params")
            raise ValueError("start_byte and max_bytes must be integers for byte mode")
        if start_byte_int < 0 or max_bytes_int <= 0:
            _safe_inc_metric("executor_pty_read_invalid_params")
            raise ValueError("start_byte must be >=0 and max_bytes >0 for byte mode")
        if updated_params.get("include_line_numbers"):
            _safe_inc_metric("executor_pty_read_invalid_params")
            _log(
                "WARNING",
                "[PTY] include_line_numbers is not supported for byte-range reads; ignoring flag",
            )
            updated_params["include_line_numbers"] = False
    return updated_params


def build_cat_command(path: str) -> List[str]:
    """Build a cat command for full-file reads."""

    return ["cat", shlex.quote(path)]


def build_head_command(path: str, lines: int) -> List[str]:
    """Build a head command for the first N lines."""

    return ["head", "-n", str(lines), shlex.quote(path)]


def build_tail_command(path: str, lines: int) -> List[str]:
    """Build a tail command for the last N lines."""

    return ["tail", "-n", str(lines), shlex.quote(path)]


def build_sed_range_command(path: str, start_line: int, end_line: int) -> List[str]:
    """Build a sed command for a specific line range."""

    return ["sed", "-n", f"{start_line},{end_line}p", shlex.quote(path)]


def build_grep_command(path: str, pattern: str, case_sensitive: bool) -> List[str]:
    """Build a grep command for pattern matching."""

    flags = ["-E", "-n"]
    if not case_sensitive:
        flags.append("-i")
    return ["grep", *flags, "--", pattern, shlex.quote(path)]


def build_ls_command(path: str, long_format: bool = True) -> List[str]:
    """Build an ls command for directory listings."""

    flags = ["-la"] if long_format else ["-a"]
    return ["ls", *flags, shlex.quote(path)]


def build_stat_command(path: str) -> List[str]:
    """Build a stat command for file metadata."""

    return ["stat", shlex.quote(path)]


def build_find_command(
    path: str,
    name_pattern: Optional[str],
    max_depth: Optional[int],
) -> List[str]:
    """Build a find command for file search."""

    command = ["find", shlex.quote(path)]
    if max_depth is not None:
        command.extend(["-maxdepth", str(max_depth)])
    if name_pattern:
        command.extend(["-name", name_pattern])
    return command


def build_mkdir_command(path: str, parents: bool = True) -> List[str]:
    """Build a mkdir command."""

    flags = ["-p"] if parents else []
    return ["mkdir", *flags, shlex.quote(path)]


def build_rm_command(path: str, recursive: bool, force: bool) -> List[str]:
    """Build an rm command."""

    flags: List[str] = []
    if recursive:
        flags.append("-r")
    if force:
        flags.append("-f")
    return ["rm", *flags, shlex.quote(path)]


def build_mv_command(src: str, dest: str) -> List[str]:
    """Build a mv command."""

    return ["mv", shlex.quote(src), shlex.quote(dest)]


def build_cp_command(src: str, dest: str, recursive: bool) -> List[str]:
    """Build a cp command."""

    flags = ["-r"] if recursive else []
    return ["cp", *flags, shlex.quote(src), shlex.quote(dest)]


def build_transfer_command(
    operation: str,
    src: str,
    dest: str,
    recursive: bool,
    create_parents: bool,
) -> List[str]:
    """Build a move or copy command with optional parent creation."""

    if operation not in {"mv", "cp"}:
        raise ValueError(f"Unsupported transfer operation: {operation}")

    if operation == "mv":
        command = build_mv_command(src, dest)
    else:
        command = build_cp_command(src, dest, recursive=recursive)

    if not create_parents:
        return command

    mkdir_command = f"mkdir -p {shlex.quote(str(Path(dest).parent))}"
    combined = f"{mkdir_command}\n{' '.join(command)}"
    return ["bash", "-lc", combined]


def build_pty_filesystem_command(
    tool_id: str,
    parameters: Dict[str, Any],
    *,
    resolve_container_path: Callable[[str], str],
    logger: Optional[Any] = None,
) -> str:
    """Build container PTY command for filesystem tools.

    This function owns the filesystem-specific PTY conversion that previously
    lived in executor orchestration.
    """
    import time
    import hashlib

    def _log(level: str, message: str) -> None:
        if logger is None:
            return
        try:
            logger.log_operation(level, message)
        except Exception:
            pass

    def safe_inc_metric(name: str) -> None:
        try:
            from backend.services.metrics.utils import safe_inc

            safe_inc(name)
        except Exception:
            pass

    def pipefail_wrap(body: str) -> str:
        return f"bash -o pipefail -c {shlex.quote(body)}"

    def cap_output_rows(command: str, max_rows: int) -> str:
        return pipefail_wrap(f"{command} | head -n {max_rows}")

    if tool_id == "filesystem.read_tail":
        translated = dict(parameters)
        translated["read_mode"] = "tail"
        translated["num_lines"] = translated.pop(
            "lines", translated.get("num_lines", DEFAULT_LINE_LIMIT)
        )
        translated["include_line_numbers"] = translated.pop(
            "show_line_numbers", False
        )
        return build_pty_filesystem_command(
            "filesystem.read_file",
            translated,
            resolve_container_path=resolve_container_path,
            logger=logger,
        )

    if tool_id == "filesystem.read_head":
        translated = dict(parameters)
        translated["read_mode"] = "head"
        translated["num_lines"] = translated.pop(
            "lines", translated.get("num_lines", DEFAULT_LINE_LIMIT)
        )
        translated["include_line_numbers"] = translated.pop(
            "show_line_numbers", False
        )
        return build_pty_filesystem_command(
            "filesystem.read_file",
            translated,
            resolve_container_path=resolve_container_path,
            logger=logger,
        )

    if tool_id == "filesystem.grep":
        translated = dict(parameters)
        translated["read_mode"] = "grep"
        translated["search"] = translated.pop("pattern", translated.get("search"))
        translated["case_sensitive"] = not bool(translated.pop("ignore_case", False))
        translated["num_lines"] = translated.pop(
            "max_matches", translated.get("num_lines", MAX_TEXT_MATCHES)
        )
        translated["include_line_numbers"] = translated.pop(
            "show_line_numbers", True
        )
        return build_pty_filesystem_command(
            "filesystem.read_file",
            translated,
            resolve_container_path=resolve_container_path,
            logger=logger,
        )

    if tool_id == "filesystem.read_file":
        updated_params = validate_fs_read_params_for_pty(parameters, logger=logger)
        path = resolve_filesystem_command_path(
            updated_params.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        read_mode = resolve_read_mode(
            read_mode=updated_params.get("read_mode"),
            grep_pattern=updated_params.get("grep_pattern"),
            start_line=updated_params.get("start_line"),
            num_lines=updated_params.get("num_lines"),
            start_byte=updated_params.get("start_byte"),
            max_bytes=updated_params.get("max_bytes"),
            encoding=updated_params.get("encoding", "utf-8"),
        )
        include_line_numbers = bool(updated_params.get("include_line_numbers"))

        safe_inc_metric(f"executor_pty_read_mode_{read_mode or 'full'}")
        if include_line_numbers:
            safe_inc_metric("executor_pty_read_with_line_numbers")

        if read_mode == "full":
            max_bytes = _coerce_int_with_bounds(
                updated_params.get("max_bytes"),
                default=200_000,
                minimum=1,
                maximum=PTY_READ_MAX_BYTES,
            )
            base_cmd = f"head -c {max_bytes} {shlex.quote(path)}"
        elif read_mode == "head":
            num_lines = _coerce_int_with_bounds(
                updated_params.get("num_lines"),
                default=DEFAULT_LINE_LIMIT,
                minimum=1,
                maximum=PTY_READ_MAX_LINES,
            )
            base_cmd = f"head -n {num_lines} {shlex.quote(path)}"
        elif read_mode == "tail":
            num_lines = _coerce_int_with_bounds(
                updated_params.get("num_lines"),
                default=DEFAULT_LINE_LIMIT,
                minimum=1,
                maximum=PTY_READ_MAX_LINES,
            )
            base_cmd = f"tail -n {num_lines} {shlex.quote(path)}"
        elif read_mode == "range":
            start_line = _coerce_int_with_bounds(
                updated_params.get("start_line"),
                default=1,
                minimum=1,
                maximum=1_000_000_000,
            )
            num_lines = _coerce_int_with_bounds(
                updated_params.get("num_lines"),
                default=DEFAULT_LINE_LIMIT,
                minimum=1,
                maximum=PTY_READ_MAX_LINES,
            )
            end_line = start_line + num_lines - 1
            base_cmd = f"sed -n '{start_line},{end_line}p' {shlex.quote(path)}"
        elif read_mode == "grep":
            pattern = updated_params.get("grep_pattern")
            max_matches = _coerce_int_with_bounds(
                updated_params.get("num_lines"),
                default=MAX_TEXT_MATCHES,
                minimum=1,
                maximum=PTY_READ_MAX_LINES,
            )
            flags = ["-E", "-n", "-m", str(max_matches)]
            case_sensitive = updated_params.get("case_sensitive", True)
            if not case_sensitive:
                flags.append("-i")
            base_cmd = (
                f"grep {' '.join(flags)} -- {shlex.quote(pattern)} {shlex.quote(path)}"
            )
        elif read_mode == "byte":
            start_byte = _coerce_int_with_bounds(
                updated_params.get("start_byte"),
                default=0,
                minimum=0,
                maximum=1_000_000_000,
            )
            max_bytes = _coerce_int_with_bounds(
                updated_params.get("max_bytes"),
                default=200_000,
                minimum=1,
                maximum=PTY_READ_MAX_BYTES,
            )
            if start_byte > 0:
                base_cmd = (
                    f"dd if={shlex.quote(path)} bs=1 "
                    f"skip={start_byte} count={max_bytes} 2>/dev/null"
                )
            else:
                base_cmd = f"head -c {max_bytes} {shlex.quote(path)}"
            safe_inc_metric("executor_pty_read_byte_range")
        else:
            _log("ERROR", f"[PTY] Unsupported read_mode={read_mode}")
            raise ValueError(f"Unsupported read_mode: {read_mode}")

        if include_line_numbers and read_mode not in {"byte", "grep"}:
            if read_mode == "range":
                base_cmd = pipefail_wrap(
                    f"{base_cmd} | awk '{{print NR+{start_line}-1\"| \" $0}}'"
                )
            elif read_mode == "tail":
                start_calc = (
                    f"start_line=$(( $(wc -l < {shlex.quote(path)}) - {num_lines} + 1 )); "
                    'if [ "$start_line" -lt 1 ]; then start_line=1; fi; '
                    f"{base_cmd} | awk -v offset=\"$start_line\" '{{print NR+offset-1\"| \" $0}}'"
                )
                base_cmd = pipefail_wrap(start_calc)
            else:
                base_cmd = pipefail_wrap(
                    f"{base_cmd} | awk '{{print NR+1-1\"| \" $0}}'"
                )

        _log("DEBUG", f"[PTY] Generated read command ({read_mode or 'full'}): {base_cmd}")
        return base_cmd

    if tool_id == "filesystem.write_file":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        content = parameters.get("content", "")
        delimiter = f"EOF_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]}"
        return f"cat > {shlex.quote(path)} << '{delimiter}'\n{content}\n{delimiter}"

    if tool_id == "filesystem.append_file":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        content = parameters.get("content", "")
        delimiter = f"EOF_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]}"
        return f"cat >> {shlex.quote(path)} << '{delimiter}'\n{content}\n{delimiter}"

    if tool_id == "filesystem.delete_path":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        return f"rm -rf {shlex.quote(path)}"

    if tool_id == "filesystem.make_dir":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        return f"mkdir -p {shlex.quote(path)}"

    if tool_id == "filesystem.list_dir":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        max_results = _coerce_int_with_bounds(
            parameters.get("max_results"),
            default=DEFAULT_LIST_DIR_MAX_RESULTS,
            minimum=1,
            maximum=PTY_LIST_MAX_RESULTS,
        )
        if parameters.get("recursive"):
            base_cmd = f"find {shlex.quote(path)}"
        else:
            base_cmd = f"ls -la {shlex.quote(path)}"
        return cap_output_rows(base_cmd, max_results)

    if tool_id == "filesystem.move_path":
        src = resolve_filesystem_command_path(
            parameters.get("src", ""),
            resolve_container_path=resolve_container_path,
        )
        dest = resolve_filesystem_command_path(
            parameters.get("dest", ""),
            resolve_container_path=resolve_container_path,
        )
        return f"mv {shlex.quote(src)} {shlex.quote(dest)}"

    if tool_id == "filesystem.copy_path":
        src = resolve_filesystem_command_path(
            parameters.get("src", ""),
            resolve_container_path=resolve_container_path,
        )
        dest = resolve_filesystem_command_path(
            parameters.get("dest", ""),
            resolve_container_path=resolve_container_path,
        )
        return f"cp -r {shlex.quote(src)} {shlex.quote(dest)}"

    if tool_id == "filesystem.stat_path":
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        return f"stat {shlex.quote(path)}"

    if tool_id == "filesystem.find_paths":
        path = resolve_filesystem_command_path(
            parameters.get("path", "."),
            resolve_container_path=resolve_container_path,
        )
        pattern = parameters.get("filename_glob", "*")
        max_results = _coerce_int_with_bounds(
            parameters.get("max_results"),
            default=MAX_FIND_MATCHES,
            minimum=1,
            maximum=PTY_FIND_MAX_RESULTS,
        )
        base_cmd = f"find {shlex.quote(path)} -name {shlex.quote(pattern)}"
        return cap_output_rows(base_cmd, max_results)

    if tool_id == "filesystem.search_text":
        path = resolve_filesystem_command_path(
            parameters.get("path", "."),
            resolve_container_path=resolve_container_path,
        )
        query = parameters.get("query", "")
        max_results = _coerce_int_with_bounds(
            parameters.get("max_results"),
            default=MAX_TEXT_MATCHES,
            minimum=1,
            maximum=PTY_SEARCH_MAX_RESULTS,
        )
        flags = ["-n", "-H"]
        if parameters.get("recursive", True):
            flags.append("-r")
        if not parameters.get("case_sensitive", True):
            flags.append("-i")
        if parameters.get("use_regex", False):
            flags.append("-E")
        else:
            flags.append("-F")
        grep_cmd = f"grep {' '.join(flags)} -- {shlex.quote(query)} {shlex.quote(path)}"
        if parameters.get("recursive", True):
            base_cmd = grep_cmd
        else:
            nonrecursive_flags = " ".join(flags)
            quoted_query = shlex.quote(query)
            quoted_path = shlex.quote(path)
            directory_cmd = (
                f"find {quoted_path} -maxdepth 1 -type f "
                f"-exec grep {nonrecursive_flags} -- {quoted_query} {{}} +"
            )
            base_cmd = (
                f"if [ -d {quoted_path} ]; then {directory_cmd}; "
                f"else {grep_cmd}; fi"
            )
        return cap_output_rows(base_cmd, max_results)

    if tool_id == "filesystem.edit_lines":
        mode = parameters.get("mode", "replace")
        if mode != "delete":
            raise ValueError(
                f"PTY transport only supports delete mode for filesystem.edit_lines. "
                f"Mode '{mode}' requires direct execution."
            )
        path = resolve_filesystem_command_path(
            parameters.get("path", ""),
            resolve_container_path=resolve_container_path,
        )
        start_line = parameters.get("start_line", 1)
        end_line = parameters.get("end_line", start_line)
        return f"sed -i '{start_line},{end_line}d' {shlex.quote(path)}"

    raise ValueError(
        f"Tool {tool_id} does not support PTY execution. "
        f"PTY is only available for shell (shell.exec, shell.script) and filesystem (filesystem.*) tools."
    )

def _generate_safe_heredoc_delimiter(content: str, base: str = DEFAULT_HEREDOC_MARKER) -> str:
    """Generate a HEREDOC delimiter that doesn't appear in content.
    
    This prevents content truncation when using heredoc syntax in shell
    commands. If the base delimiter appears in content, we append a
    counter until we find a safe variant.
    
    Args:
        content: The content that will be written
        base: Base delimiter string to start with
        
    Returns:
        A delimiter string guaranteed not to appear in content
    """
    import hashlib
    
    delimiter = base
    counter = 0
    
    def delimiter_in_content(delim: str) -> bool:
        # Check if delimiter appears on its own line (causes heredoc termination)
        if f"\n{delim}\n" in content:
            return True
        if content.startswith(f"{delim}\n"):
            return True
        if content.endswith(f"\n{delim}"):
            return True
        if content == delim:
            return True
        return False
    
    while delimiter_in_content(delimiter):
        counter += 1
        delimiter = f"{base}_{counter}"
        
        # Safety limit to prevent infinite loop on adversarial input
        if counter > 1000:
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            delimiter = f"{base}_{content_hash}"
            break
    
    return delimiter


def build_heredoc_command(path: str, content: str, append: bool) -> List[str]:
    """Build a heredoc command for writing file content.
    
    Uses safe delimiter generation to prevent content truncation when
    the content contains the heredoc marker.
    """
    # Generate safe delimiter
    delimiter = _generate_safe_heredoc_delimiter(content)
    
    redirect = ">>" if append else ">"
    command = (
        f"cat {redirect} {shlex.quote(path)} << '{delimiter}'\n"
        f"{content}\n"
        f"{delimiter}"
    )
    return ["bash", "-lc", command]


def build_write_command(
    path: str,
    content: str,
    create_parents: bool,
    append: bool,
) -> List[str]:
    """Build a write or append command with optional parent creation."""

    heredoc_command = build_heredoc_command(path, content, append)
    if not create_parents:
        return heredoc_command

    mkdir_command = f"mkdir -p {shlex.quote(str(Path(path).parent))}"
    combined = f"{mkdir_command}\n{heredoc_command[2]}"
    return ["bash", "-lc", combined]

def should_create_artifact(output: str) -> bool:
    """Check if output should be persisted as an artifact."""

    return len(output) > ARTIFACT_SIZE_THRESHOLD_BYTES


def create_output_artifact(output: str, tool_name: str, timestamp: Optional[int] = None) -> str:
    """Persist tool output to the artifacts directory."""

    ts = timestamp or _runtime_artifact_stamp() or int(time.time())
    os.makedirs("artifacts", exist_ok=True)
    path = f"artifacts/{tool_name}_{ts}.txt"
    with open(path, "w", encoding="utf-8", errors="ignore") as handle:
        handle.write(output)
    return path


def _runtime_artifact_stamp() -> Optional[int]:
    """Return a runtime-supplied artifact stamp when available."""
    raw = os.getenv("DROWAI_ARTIFACT_STAMP")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def resolve_read_mode(
    *,
    read_mode: Optional[str],
    grep_pattern: Optional[str],
    start_line: Optional[int],
    num_lines: Optional[int],
    start_byte: int,
    max_bytes: int,
    encoding: Optional[str],
) -> str:
    """Resolve the read mode for file content retrieval."""

    if read_mode:
        return read_mode
    if grep_pattern:
        return "grep"
    if start_line is not None:
        return "range"
    if num_lines is not None:
        return "head"
    if encoding is None or start_byte:
        return "byte"
    return "full"


# =============================================================================
# Smart read mode detection
# =============================================================================


def get_line_count_python(target: Path) -> Optional[int]:
    """Count lines using pure Python (cross-platform).
    
    This is more reliable than `wc -l` as it works on Windows and handles
    edge cases like files without trailing newlines.
    
    Args:
        target: Path to the file to count lines in
        
    Returns:
        Number of lines in the file, or None if the file cannot be read
        
    Performance:
        Uses a buffered approach that's efficient for files up to ~100MB.
        For very large files, consider using approximate methods.
    """
    try:
        count = 0
        with target.open("rb") as f:
            # Read in chunks for memory efficiency
            buffer_size = 1024 * 1024  # 1MB chunks
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                count += chunk.count(b"\n")
        return count
    except (OSError, PermissionError) as exc:
        logger.debug(f"Failed to count lines in {target}: {exc}")
        return None


def get_file_size_bytes(target: Path) -> Optional[int]:
    """Get file size in bytes.
    
    Args:
        target: Path to the file
        
    Returns:
        File size in bytes, or None if stat fails
    """
    try:
        return target.stat().st_size
    except (OSError, PermissionError):
        return None


@dataclass
class SmartReadResult:
    """Result of smart read mode detection.
    
    Attributes:
        mode: Recommended read mode (full, head, tail)
        num_lines: Recommended number of lines for head/tail modes
        suggestion: LLM-friendly suggestion for next action
        total_lines: Total lines in the file (if counted)
        file_size_bytes: File size in bytes
    """
    mode: str
    num_lines: Optional[int]
    suggestion: Optional[str]
    total_lines: Optional[int]
    file_size_bytes: Optional[int]


def smart_read_mode_detection(
    target: Path,
    *,
    explicit_mode: Optional[str] = None,
    explicit_num_lines: Optional[int] = None,
) -> SmartReadResult:
    """Determine optimal read mode based on file size and content.
    
    This function implements intelligent file reading that:
    - Reads small files (<= 1000 lines) fully
    - Shows first N lines for medium files (1001-5000 lines) with suggestion
    - Shows last N lines for large files (> 5000 lines) with context
    
    The explicit parameters allow users to override smart detection.
    
    Args:
        target: Path to the file to analyze
        explicit_mode: User-specified mode (overrides smart detection)
        explicit_num_lines: User-specified line count
        
    Returns:
        SmartReadResult with recommended mode and LLM-friendly suggestions
        
    Examples:
        >>> result = smart_read_mode_detection(Path("small.txt"))
        >>> result.mode
        'full'
        
        >>> result = smart_read_mode_detection(Path("large_log.txt"))
        >>> result.mode
        'tail'
        >>> result.suggestion
        'Large file (8500 lines). Showing last 100 lines. Use read_mode="range" with start_line to read specific sections.'
    """
    # If user explicitly specifies mode, honor it
    if explicit_mode:
        return SmartReadResult(
            mode=explicit_mode,
            num_lines=explicit_num_lines,
            suggestion=None,
            total_lines=None,
            file_size_bytes=get_file_size_bytes(target),
        )
    
    file_size = get_file_size_bytes(target)
    
    # For very small files (< 50KB), skip line counting - just read fully
    if file_size is not None and file_size < 50 * 1024:
        return SmartReadResult(
            mode="full",
            num_lines=None,
            suggestion=None,
            total_lines=None,
            file_size_bytes=file_size,
        )
    
    # Count lines for larger files
    total_lines = get_line_count_python(target)
    
    if total_lines is None:
        # Cannot determine line count, fall back to byte-based heuristic
        if file_size is not None and file_size > BYTE_READ_MODE_THRESHOLD:
            return SmartReadResult(
                mode="head",
                num_lines=SMART_DEFAULT_HEAD_LINES,
                suggestion=(
                    f"Large file ({file_size:,} bytes). "
                    f"Showing first {SMART_DEFAULT_HEAD_LINES} lines. "
                    f"Use read_mode='tail' or read_mode='range' to see other sections."
                ),
                total_lines=None,
                file_size_bytes=file_size,
            )
        return SmartReadResult(
            mode="full",
            num_lines=None,
            suggestion=None,
            total_lines=None,
            file_size_bytes=file_size,
        )
    
    # Small files: read fully
    if total_lines <= SMALL_FILE_LINE_THRESHOLD:
        return SmartReadResult(
            mode="full",
            num_lines=None,
            suggestion=None,
            total_lines=total_lines,
            file_size_bytes=file_size,
        )
    
    # Medium files: head with suggestion
    if total_lines <= MEDIUM_FILE_LINE_THRESHOLD:
        lines_to_show = explicit_num_lines or SMART_DEFAULT_HEAD_LINES
        return SmartReadResult(
            mode="head",
            num_lines=lines_to_show,
            suggestion=(
                f"File has {total_lines:,} lines. "
                f"Showing first {lines_to_show}. "
                f"Use read_mode='range' with start_line to continue reading, "
                f"or read_mode='tail' to see the end."
            ),
            total_lines=total_lines,
            file_size_bytes=file_size,
        )
    
    # Large files: tail with suggestion (logs typically have recent data at the end)
    lines_to_show = explicit_num_lines or SMART_DEFAULT_TAIL_LINES
    return SmartReadResult(
        mode="tail",
        num_lines=lines_to_show,
        suggestion=(
            f"Large file ({total_lines:,} lines). "
            f"Showing last {lines_to_show} lines. "
            f"Use read_mode='head' to see the beginning, "
            f"or read_mode='range' with start_line for specific sections."
        ),
        total_lines=total_lines,
        file_size_bytes=file_size,
    )


def resolve_read_mode_smart(
    target: Path,
    *,
    read_mode: Optional[str],
    grep_pattern: Optional[str],
    start_line: Optional[int],
    num_lines: Optional[int],
    start_byte: int,
    max_bytes: int,
    encoding: Optional[str],
    use_smart_detection: bool = True,
) -> Tuple[str, Optional[SmartReadResult]]:
    """Enhanced read mode resolution with optional smart detection.
    
    This function combines the original resolve_read_mode logic with
    the new smart detection capability.
    
    Args:
        target: Path to the file (for smart detection)
        read_mode: Explicit read mode from user
        grep_pattern: Grep pattern (implies grep mode)
        start_line: Start line for range mode
        num_lines: Number of lines to read
        start_byte: Byte offset for binary reads
        max_bytes: Maximum bytes to read
        encoding: Text encoding (None for binary)
        use_smart_detection: Enable smart mode detection
        
    Returns:
        Tuple of (resolved_mode, smart_result or None)
    """
    # Explicit mode specifications take precedence
    if grep_pattern:
        return "grep", None
    if start_line is not None:
        return "range", None
    if encoding is None or start_byte:
        return "byte", None
    
    # If mode is explicitly specified, use it
    if read_mode:
        return read_mode, None
    
    # If num_lines is specified without mode, interpret as head
    if num_lines is not None:
        return "head", None
    
    # Use smart detection if enabled
    if use_smart_detection and target.exists() and target.is_file():
        smart_result = smart_read_mode_detection(target)
        return smart_result.mode, smart_result
    
    # Fallback to full read
    return "full", None
