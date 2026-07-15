"""Text search for directories and single files in the active Kali runtime.

This module powers ``filesystem.search_text``. Container transports search
paths inside the active Kali runtime, where relative paths resolve from
``/workspace`` and absolute paths are allowed. The direct Python compatibility
path remains workspace-local.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsSearchTextArgs, FsSearchTextResult, TextMatch
from ._error_helpers import build_llm_error
from ._helpers import (
    build_tool_result,
    create_output_artifact,
    describe_filesystem_scope,
    resolve_workspace_path_safe,
    should_create_artifact,
    to_workspace_relative,
    workspace_root,
)


def _matches_globs(name: str, patterns: Iterable[str] | None) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


class FsSearchTextTool(BaseTool):
    """
    Search file contents for text patterns in the active Kali runtime.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `grep -r {pattern} {path}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Debugging: See matching lines in context
        - Verification: Visual confirmation of search results
        - Troubleshooting: Diagnose search pattern issues
    
    PTY Command Equivalent:
        grep -r '{pattern}' {path}
        # or with case-insensitive flag:
        grep -ri '{pattern}' {path}
    
    Note: Results may be large; PTY output may be truncated.
    """

    args_model = FsSearchTextArgs
    informational_exit_codes = frozenset({1})

    def render_result_output(
        self,
        *,
        args: FsSearchTextArgs,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        """Render scoped empty grep results for command transports."""
        if stdout.strip() or stderr.strip():
            return stdout, stderr

        scope = describe_filesystem_scope(args.path)
        return (
            "\n".join(
                [
                    f"No text matches found for {args.query!r} under {scope}.",
                    "Search completed with match_count=0.",
                ]
            ),
            stderr,
        )

    def build_command(self, args: FsSearchTextArgs) -> List[str]:
        """Build a PTY-safe command for text searching."""

        workspace = workspace_root()
        base_path = resolve_workspace_path_safe(args.path, workspace=workspace)
        flags: List[str] = ["-n", "-H"]
        if args.recursive:
            flags.append("-r")
        if not args.case_sensitive:
            flags.append("-i")
        if args.use_regex:
            flags.append("-E")
        else:
            flags.append("-F")

        context_span = max(args.context_before, args.context_after)
        if context_span:
            flags.extend(["-C", str(context_span)])

        return ["grep", *flags, "--", args.query, str(base_path)]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsSearchTextArgs,
    ) -> Dict[str, object]:
        """Parse grep output into structured metadata."""

        matches: List[TextMatch] = []
        if exit_code == 0 and stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"^(.*?):(\d+):(.*)$", line)
                if not match:
                    continue
                path_str, line_no, snippet = match.groups()
                matches.append(
                    TextMatch(
                        path=path_str,
                        line=int(line_no),
                        column=None,
                        snippet=snippet,
                    )
                )

        result = FsSearchTextResult(matches=matches, truncated=False)
        metadata = result.model_dump()
        metadata["exit_code"] = exit_code
        if exit_code not in (0, 1):
            metadata["error"] = stderr or "Search failed"
        return {"fs_search_text": metadata}

    def create_artifacts(
        self,
        stdout: str,
        args: FsSearchTextArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist search output as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_search_text", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_search_text_errors", timestamp))
        return created

    def run(self, args: FsSearchTextArgs) -> ToolResult:
        start = time.time()
        workspace = workspace_root()

        try:
            base_path: Path = resolve_workspace_path_safe(args.path, workspace=workspace)
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

        if not base_path.exists():
            metadata = self.parse_output("", "", 0, args)
            metadata["fs_search_text"]["not_found"] = True
            metadata["fs_search_text"]["searched_path"] = args.path
            return build_tool_result(
                success=True,
                start=start,
                stdout=f"Located 0 matches (search root '{args.path}' does not exist)",
                metadata=metadata,
            )

        matches: List[TextMatch] = []
        truncated = False

        regex: Optional[re.Pattern[str]] = None
        query = args.query
        if args.use_regex:
            flags = 0 if args.case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(query, flags)
            except re.error as exc:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=(
                        f"Invalid regular expression: {exc}\n"
                        "Check your regex syntax. Common issues:\n"
                        "  - Unescaped special characters: . * + ? [ ] ( ) { } | \\ ^ $\n"
                        "  - Unbalanced parentheses or brackets\n"
                        "Suggestion: Set use_regex=false for literal string search."
                    ),
                    metadata={"error": "invalid_regex"},
                    exit_code=1,
                )
        else:
            if not args.case_sensitive:
                query = query.lower()

        def should_visit(name: str) -> bool:
            if not _matches_globs(name, args.include_globs):
                return False
            if args.exclude_globs and any(
                fnmatch.fnmatch(name, pattern) for pattern in args.exclude_globs
            ):
                return False
            return True

        def scan_file(path: Path) -> bool:
            nonlocal truncated
            try:
                if path.stat().st_size > args.max_file_bytes:
                    return True
            except OSError:
                return True

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return True

            lines = text.splitlines()
            for idx, line in enumerate(lines, start=1):
                haystack = line if args.case_sensitive else line.lower()
                matches_iter: Iterable[re.Match[str]] | Iterable[int]

                if regex is not None:
                    matches_iter = list(regex.finditer(line))
                else:
                    if query not in haystack:
                        continue
                    # Find all occurrences for substring search
                    matches_iter = []
                    start_idx = 0
                    search_term = query
                    while True:
                        pos = haystack.find(search_term, start_idx)
                        if pos == -1:
                            break
                        matches_iter.append(pos)
                        start_idx = pos + 1

                for match_obj in matches_iter:
                    if len(matches) >= args.max_results:
                        truncated = True
                        return False

                    if regex is not None:
                        span = match_obj.span()  # type: ignore[attr-defined]
                        column = span[0] + 1
                    else:
                        column = int(match_obj) + 1  # type: ignore[arg-type]

                    start_context = max(0, idx - 1 - args.context_before)
                    end_context = min(len(lines), idx + args.context_after)
                    snippet = "\n".join(lines[start_context:end_context])

                    matches.append(
                        TextMatch(
                            path=to_workspace_relative(path, workspace),
                            line=idx,
                            column=column,
                            snippet=snippet,
                        )
                    )
                if truncated:
                    return False
            return True

        if base_path.is_file():
            if should_visit(base_path.name):
                scan_file(base_path)
        else:
            iterator: Iterable[Path]
            if args.recursive:
                iterator = (
                    Path(root) / name
                    for root, _, files in os.walk(base_path)
                    for name in files
                )
            else:
                iterator = (child for child in base_path.iterdir() if child.is_file())

            for candidate in iterator:
                name = candidate.name
                if not should_visit(name):
                    continue
                if not candidate.is_file():
                    continue
                if not scan_file(candidate):
                    break

        result = FsSearchTextResult(matches=matches, truncated=truncated)
        listing_text = "\n".join(
            f"{match.path}:{match.line}:{match.snippet}" for match in matches
        )
        stdout = f"Located {len(matches)} matches"
        if truncated:
            stdout += " (truncated)"
        if listing_text:
            stdout = f"{stdout}\n{listing_text}"
        elif not matches:
            scope = describe_filesystem_scope(args.path)
            stdout = "\n".join(
                [
                    f"No text matches found for {args.query!r} under {scope}.",
                    "Search completed with match_count=0.",
                ]
            )

        metadata = self.parse_output(listing_text, "", 0, args)
        metadata["fs_search_text"]["matches"] = result.model_dump()["matches"]
        metadata["fs_search_text"]["truncated"] = truncated
        artifacts = self.create_artifacts(listing_text, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )
