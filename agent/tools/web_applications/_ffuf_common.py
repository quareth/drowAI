"""Shared helpers for first-class ffuf tools.

This module centralizes safe wordlist handling, ffuf-specific validation,
inline input materialization, and output parsing so the crawler and fuzzer
variants stay consistent and honest with the LLM-facing schema.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

from agent.utils.workspace_helpers import resolve_container_path
from runtime_shared.workspace_files import RuntimeWorkspaceFile

from ..filesystem._helpers import (
    resolve_workspace_path_safe,
    workspace_root,
)
from ..filesystem._reliability import atomic_write_text

DELAY_RE = re.compile(r"^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?$")
WORDLIST_KEYWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SYSTEM_WORDLIST_PREFIXES = ("/usr/share/seclists/", "/usr/share/wordlists/")
EXECUTOR_WORKSPACE_ROOT = "/workspace"


def _effective_workspace_root() -> Path:
    """Return the task workspace, falling back to the current directory in tests."""

    try:
        return workspace_root()
    except OSError:
        fallback = Path(os.getenv("WORKSPACE") or Path.cwd()).resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "artifacts").mkdir(parents=True, exist_ok=True)
        return fallback


def validate_http_target(target: str) -> str:
    """Validate that ``target`` is an absolute HTTP(S) URL."""

    value = (target or "").strip()
    if not value:
        raise ValueError("target URL is required")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("target scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("target must be an absolute URL with hostname")
    return value


def validate_delay(value: Optional[str]) -> Optional[str]:
    """Validate ffuf ``-p`` delay syntax (float or float range)."""

    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if not DELAY_RE.fullmatch(normalized):
        raise ValueError(
            "delay must be a float or float range accepted by ffuf -p, for example '0.1' or '0.1-2.0'"
        )
    return normalized


def basic_auth_header(user: str, password: str) -> str:
    """Return an ``Authorization: Basic`` header string."""

    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {token}"


def validate_input_cmd(value: Optional[str]) -> Optional[str]:
    """Validate ffuf ``-input-cmd`` usage for LLM-safe one-liners."""

    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(
            "input_cmd must be a single-line command. Do not use heredocs or multiline scripts; "
            "prefer inline_wordlist for generated sequences."
        )
    if "<<" in normalized:
        raise ValueError(
            "input_cmd does not support heredoc syntax. Use a one-liner like 'seq 0 200' or prefer inline_wordlist."
        )
    return normalized


def materialize_inline_wordlist(
    lines: Sequence[str],
    task_id: int | None = None,
    *,
    prefix: str = "ffuf",
) -> Path:
    """Write inline entries to a workspace-scoped wordlist file."""

    _ = task_id
    workspace = _effective_workspace_root()
    timestamp = _runtime_artifact_stamp() or int(time.time() * 1000)
    relative_path = f"wordlists/{prefix}_{timestamp}.txt"
    target = resolve_workspace_path_safe(relative_path, workspace=workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(str(line) for line in lines)
    if content and not content.endswith("\n"):
        content += "\n"
    atomic_write_text(target, content, "utf-8")
    return target


def inline_wordlist_relative_path(*, prefix: str) -> str:
    """Return a workspace-relative path for a generated ffuf inline wordlist."""

    timestamp = _runtime_artifact_stamp() or int(time.time() * 1000)
    return f"wordlists/{prefix}_{timestamp}.txt"


def inline_wordlist_workspace_file(
    lines: Sequence[str],
    *,
    relative_path: str,
    description: str,
) -> RuntimeWorkspaceFile:
    """Return a runtime workspace file declaration for inline ffuf entries."""

    content = "\n".join(str(line) for line in lines)
    if content and not content.endswith("\n"):
        content += "\n"
    return RuntimeWorkspaceFile.from_text(
        relative_path=relative_path,
        content=content,
        description=description,
    )


def _runtime_artifact_stamp() -> Optional[int]:
    """Return a runtime-supplied artifact stamp when parallel PTY binds one."""
    raw = os.getenv("DROWAI_ARTIFACT_STAMP")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def split_wordlist_reference(value: str) -> tuple[str, Optional[str]]:
    """Split a ffuf wordlist reference into path and optional keyword."""

    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("wordlist path must not be empty")
    if ":" not in normalized:
        return normalized, None

    path_part, keyword = normalized.rsplit(":", 1)
    if "/" in keyword or "\\" in keyword or not WORDLIST_KEYWORD_RE.fullmatch(keyword):
        return normalized, None
    return path_part, keyword


def resolve_wordlist_reference(value: str, *, workspace: Optional[Path] = None) -> str:
    """Resolve a user-supplied ffuf wordlist reference to a safe path."""

    path_part, keyword = split_wordlist_reference(value)
    normalized_path = path_part.strip()
    if normalized_path.startswith(SYSTEM_WORDLIST_PREFIXES):
        resolved = normalized_path
    elif Path(normalized_path).is_absolute():
        raise ValueError(
            "Absolute wordlist paths are only allowed under /usr/share/seclists or /usr/share/wordlists"
        )
    else:
        root = workspace or _effective_workspace_root()
        resolved = str(resolve_workspace_path_safe(normalized_path, workspace=root))
    return f"{resolved}:{keyword}" if keyword else resolved


def resolve_wordlist_reference_for_execution(value: str, *, workspace: Optional[Path] = None) -> str:
    """Resolve a wordlist reference to the path ffuf should use inside the executor workspace."""

    path_part, keyword = split_wordlist_reference(value)
    normalized_path = path_part.strip()
    if normalized_path.startswith(SYSTEM_WORDLIST_PREFIXES):
        resolved = normalized_path
    elif Path(normalized_path).is_absolute():
        raise ValueError(
            "Absolute wordlist paths are only allowed under /usr/share/seclists or /usr/share/wordlists"
        )
    else:
        root = workspace or _effective_workspace_root()
        host_path = resolve_workspace_path_safe(normalized_path, workspace=root)
        resolved = resolve_container_path(
            str(host_path),
            host_workspace=str(root),
            container_workspace=EXECUTOR_WORKSPACE_ROOT,
        )
    return f"{resolved}:{keyword}" if keyword else resolved


def resolve_workspace_file_path(relative_path: str, *, workspace: Optional[Path] = None) -> str:
    """Resolve a workspace-relative file path and return its absolute string path."""

    root = workspace or _effective_workspace_root()
    return str(resolve_workspace_path_safe(relative_path, workspace=root))


def resolve_workspace_file_path_for_execution(
    relative_path: str,
    *,
    workspace: Optional[Path] = None,
) -> str:
    """Resolve a workspace-relative file path to the executor-visible container path."""

    root = workspace or _effective_workspace_root()
    host_path = resolve_workspace_path_safe(relative_path, workspace=root)
    return resolve_container_path(
        str(host_path),
        host_workspace=str(root),
        container_workspace=EXECUTOR_WORKSPACE_ROOT,
    )


def extract_keyword_from_wordlist(value: str, default_keyword: str = "FUZZ") -> str:
    """Return the keyword declared by a wordlist reference or the ffuf default."""

    _, keyword = split_wordlist_reference(value)
    return keyword or default_keyword


def to_executor_path(path: str | Path, *, workspace: Optional[Path] = None) -> str:
    """Translate a host workspace path to the executor's container-visible path."""

    root = workspace or _effective_workspace_root()
    return resolve_container_path(
        str(path),
        host_workspace=str(root),
        container_workspace=EXECUTOR_WORKSPACE_ROOT,
    )


def validate_fuzz_keyword_present(
    target: str,
    headers: Sequence[str],
    data: Optional[str],
    cookies: Optional[str],
    extra_keywords: Iterable[str],
    *,
    raw_request_file: Optional[str] = None,
) -> None:
    """Ensure all declared ffuf keywords appear in one of the fuzzable inputs."""

    search_space = [target or "", data or "", cookies or "", *list(headers or [])]
    if raw_request_file:
        request_path = resolve_workspace_file_path(raw_request_file)
        try:
            search_space.append(Path(request_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"raw_request_file could not be read: {raw_request_file}") from exc
    missing = []
    for keyword in list(extra_keywords or []) or ["FUZZ"]:
        if not any(keyword in candidate for candidate in search_space):
            missing.append(keyword)
    if missing:
        joined = ", ".join(sorted(set(missing)))
        raise ValueError(
            f"ffuf keyword(s) missing from target, headers, data, or cookies: {joined}. "
            "Example: target='https://host/item/FUZZ' or headers=['Host: FUZZ']."
        )


def build_matcher_filter_args(args: Any) -> list[str]:
    """Build ffuf matcher and filter arguments from canonical schema names."""

    command: list[str] = []
    mappings = [
        ("match_status", "-mc"),
        ("match_lines", "-ml"),
        ("match_words", "-mw"),
        ("match_size", "-ms"),
        ("match_time", "-mt"),
        ("match_regex", "-mr"),
        ("filter_status", "-fc"),
        ("filter_lines", "-fl"),
        ("filter_words", "-fw"),
        ("filter_size", "-fs"),
        ("filter_time", "-ft"),
        ("filter_regex", "-fr"),
    ]
    for field_name, flag in mappings:
        value = getattr(args, field_name, None)
        if value:
            command.extend([flag, str(value)])

    matcher_mode = getattr(args, "matcher_mode", None)
    if matcher_mode and matcher_mode != "or":
        command.extend(["-mmode", matcher_mode])

    filter_mode = getattr(args, "filter_mode", None)
    if filter_mode and filter_mode != "or":
        command.extend(["-fmode", filter_mode])

    return command


def parse_ffuf_text(text_output: str, *, target_template: Optional[str] = None) -> dict[str, Any]:
    """Parse ffuf terminal output into a structured metadata dict."""

    metadata: dict[str, Any] = {
        "results": [],
        "config": {},
        "stats": {},
        "raw_output": text_output,
    }
    if not text_output.strip():
        return metadata

    result_re = re.compile(
        r"^(?P<url>\S+)\s+(?P<status>\d{3})\s+(?P<size>\d+)\s+(?P<words>\d+)(?:\s+(?P<lines>\d+))?"
    )
    terminal_result_re = re.compile(
        r"^(?P<input>\S+)\s+\[Status:\s*(?P<status>\d{3}),\s*"
        r"Size:\s*(?P<size>\d+),\s*Words:\s*(?P<words>\d+),\s*"
        r"Lines:\s*(?P<lines>\d+)"
    )
    for raw_line in text_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("::") or line.startswith("="):
            continue
        match = result_re.match(line)
        if match:
            parsed = match.groupdict()
            metadata["results"].append(
                {
                    "url": parsed["url"],
                    "status": int(parsed["status"]),
                    "length": int(parsed["size"]),
                    "words": int(parsed["words"]),
                    "lines": int(parsed["lines"]) if parsed.get("lines") else None,
                }
            )
            continue
        terminal_match = terminal_result_re.match(line)
        if terminal_match:
            parsed = terminal_match.groupdict()
            input_value = parsed["input"]
            url = (
                target_template.replace("FUZZ", input_value)
                if target_template and "FUZZ" in target_template
                else input_value
            )
            metadata["results"].append(
                {
                    "url": url,
                    "input": {"FUZZ": input_value},
                    "status": int(parsed["status"]),
                    "length": int(parsed["size"]),
                    "words": int(parsed["words"]),
                    "lines": int(parsed["lines"]),
                }
            )
    return metadata

def parse_ffuf_json_text(json_text: str) -> dict[str, Any]:
    """Parse ffuf JSON output into a normalized metadata shape."""

    metadata: dict[str, Any] = {
        "results": [],
        "config": {},
        "commandline": [],
        "time": {},
        "raw_output": json_text,
    }
    if not json_text.strip():
        return metadata

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        records: list[dict[str, Any]] = []
        for raw_line in json_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed_line = json.loads(line)
            except json.JSONDecodeError:
                metadata["error"] = f"Failed to parse ffuf JSON: {exc}"
                return metadata
            if isinstance(parsed_line, dict):
                records.append(parsed_line)

        if not records:
            metadata["error"] = f"Failed to parse ffuf JSON: {exc}"
            return metadata

        metadata["results"] = records
        metadata["stream_format"] = "jsonl"
        return metadata

    if isinstance(payload, list):
        metadata["results"] = payload
        return metadata

    if not isinstance(payload, dict):
        metadata["error"] = "Unexpected ffuf JSON payload shape"
        return metadata

    results = payload.get("results")
    if isinstance(results, list):
        metadata["results"] = results
    elif results is not None:
        metadata["results"] = [results]

    config = payload.get("config")
    if isinstance(config, dict):
        metadata["config"] = config

    commandline = payload.get("commandline")
    if isinstance(commandline, list):
        metadata["commandline"] = [str(part) for part in commandline]
    elif isinstance(commandline, str):
        metadata["commandline"] = [commandline]

    timing = payload.get("time")
    if isinstance(timing, dict):
        metadata["time"] = timing

    for key in ("input", "position", "scraper", "error"):
        if key in payload:
            metadata[key] = payload[key]

    return metadata
