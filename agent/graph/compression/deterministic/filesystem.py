"""Filesystem-specific deterministic compression helpers.

This module is reserved for pure projection of filesystem tool metadata into
compact evidence. It must not read host files, execute tools, or call runtime
providers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS

from .common import as_int, compact_evidence_line, dedupe_string_list
from .contracts import CompressionInput, DeterministicCompressionResult

_FILESYSTEM_TOOL_IDS: tuple[str, ...] = (
    "filesystem.read_file",
    "filesystem.read_head",
    "filesystem.read_tail",
    "filesystem.grep",
    "filesystem.search_text",
    "filesystem.find_paths",
    "filesystem.list_dir",
    "filesystem.stat_path",
    "filesystem.write_file",
    "filesystem.append_file",
    "filesystem.edit_lines",
    "filesystem.copy_path",
    "filesystem.move_path",
    "filesystem.delete_path",
    "filesystem.make_dir",
)
_READ_METADATA_KEY = "fs_read"
_SEARCH_METADATA_KEY = "fs_search_text"
_FIND_METADATA_KEY = "fs_find"
_LIST_METADATA_KEY = "fs_list"
_STAT_METADATA_KEY = "fs_stat"
_MUTATION_METADATA_BY_TOOL: Mapping[str, str] = {
    "filesystem.write_file": "fs_write",
    "filesystem.append_file": "fs_append",
    "filesystem.edit_lines": "fs_edit",
    "filesystem.copy_path": "fs_copy",
    "filesystem.move_path": "fs_move",
    "filesystem.delete_path": "fs_delete",
    "filesystem.make_dir": "fs_mkdir",
}
_READ_TOOL_IDS = frozenset(
    {
        "filesystem.read_file",
        "filesystem.read_head",
        "filesystem.read_tail",
        "filesystem.grep",
    }
)
_ENTRY_LIMIT = 5


def _extract_locator_evidence_from_metadata(
    raw_result: Mapping[str, Any],
    *,
    limit: int = 5,
) -> List[str]:
    """Build locator evidence from structured filesystem metadata."""

    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return []

    evidence: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if len(evidence) >= limit:
            return
        compact = compact_evidence_line(raw)
        if compact and compact not in seen:
            seen.add(compact)
            evidence.append(compact)

    fs_search = runtime_metadata.get("fs_search_text")
    if isinstance(fs_search, Mapping):
        matches = fs_search.get("matches")
        if isinstance(matches, list):
            for item in matches:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                line = as_int(item.get("line"))
                snippet = str(item.get("snippet") or "").strip()
                if not path or line is None or not snippet:
                    continue
                add(f"{path}:{line}:{snippet}")

    fs_read = runtime_metadata.get("fs_read")
    if isinstance(fs_read, Mapping):
        line_evidence = fs_read.get("line_evidence")
        if isinstance(line_evidence, list):
            for item in line_evidence:
                add(item)

    return evidence


def filesystem_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project filesystem tool metadata into compact deterministic facts."""

    tool_name = input_data.tool_name
    runtime_metadata = input_data.raw_result.get("metadata")
    metadata = runtime_metadata if isinstance(runtime_metadata, Mapping) else {}
    parameters = _mapping_or_empty(input_data.raw_result.get("parameters"))

    if tool_name in _READ_TOOL_IDS:
        return _adapt_read_tool(
            tool_name=tool_name,
            metadata=metadata,
            parameters=parameters,
            raw_result=input_data.raw_result,
        )
    if tool_name == "filesystem.search_text":
        return _adapt_search_text(metadata=metadata, parameters=parameters)
    if tool_name == "filesystem.find_paths":
        return _adapt_path_collection(
            operation="find_paths",
            metadata_key=_FIND_METADATA_KEY,
            item_key="matches",
            metadata=metadata,
            parameters=parameters,
        )
    if tool_name == "filesystem.list_dir":
        return _adapt_path_collection(
            operation="list_dir",
            metadata_key=_LIST_METADATA_KEY,
            item_key="entries",
            metadata=metadata,
            parameters=parameters,
        )
    if tool_name == "filesystem.stat_path":
        return _adapt_stat_path(metadata=metadata, parameters=parameters)

    mutation_key = _MUTATION_METADATA_BY_TOOL.get(tool_name)
    if mutation_key is not None:
        return _adapt_mutation_tool(
            tool_name=tool_name,
            metadata_key=mutation_key,
            metadata=metadata,
            parameters=parameters,
            raw_result=input_data.raw_result,
        )

    return DeterministicCompressionResult.none(
        fallback_reason="unsupported_filesystem_tool",
    )


def register_filesystem_adapters() -> None:
    """Register deterministic filesystem adapters for visible filesystem tools."""

    from .registry import register_adapter

    for tool_id in _FILESYSTEM_TOOL_IDS:
        register_adapter(tool_id, filesystem_adapter)


def _adapt_read_tool(
    *,
    tool_name: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> DeterministicCompressionResult:
    fs_read = _mapping_or_empty(metadata.get(_READ_METADATA_KEY))
    path = _first_text(
        parameters.get("path"),
        fs_read.get("path"),
    )
    mode = _read_mode_for_tool(tool_name=tool_name, fs_read=fs_read, parameters=parameters)
    error = _compact_error(metadata=metadata, raw_result=raw_result)

    if error:
        return _error_result(
            operation=_operation_label(tool_name),
            path=path,
            error=error,
        )

    if not fs_read and not path:
        return DeterministicCompressionResult.none(
            fallback_reason="no_filesystem_read_metadata",
        )

    lines_read = as_int(fs_read.get("lines_read"))
    bytes_read = as_int(fs_read.get("bytes_read"))
    truncated = bool(fs_read.get("truncated"))
    summary_bits = [f"Read {path or 'filesystem path'}"]
    if mode:
        summary_bits.append(f"with {mode} mode")
    count_bits = []
    if lines_read is not None:
        count_bits.append(f"{lines_read} lines")
    if bytes_read is not None:
        count_bits.append(f"{bytes_read} bytes")
    if count_bits:
        summary_bits.append(f"({', '.join(count_bits)})")
    if truncated:
        summary_bits.append("(truncated)")

    findings = _read_key_findings(path=path, mode=mode, fs_read=fs_read)
    evidence = tuple(
        _prefix_path_to_line_evidence(
            path=path,
            values=fs_read.get("line_evidence"),
        )
    )
    signals = _compact_signal(
        kind="filesystem_read",
        operation=_operation_label(tool_name),
        path=path,
        mode=mode,
        lines_read=lines_read,
        bytes_read=bytes_read,
        truncated=truncated,
    )

    return DeterministicCompressionResult(
        summary=_summary(" ".join(summary_bits)),
        key_findings=tuple(findings),
        structured_signals=(signals,),
        decision_evidence=evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def _adapt_search_text(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> DeterministicCompressionResult:
    fs_search = _mapping_or_empty(metadata.get(_SEARCH_METADATA_KEY))
    error = _metadata_error(metadata, fs_search)
    path = _first_text(parameters.get("path"), fs_search.get("searched_path"))
    query = _first_text(parameters.get("query"))

    if error:
        return _error_result(operation="search_text", path=path, error=error)
    if not fs_search and not path:
        return DeterministicCompressionResult.none(
            fallback_reason="no_filesystem_search_metadata",
        )

    matches = _mapping_list(fs_search.get("matches"))
    truncated = bool(fs_search.get("truncated"))
    summary = f"Searched text under {path or 'filesystem path'}"
    if query:
        summary += f" for {query!r}"
    summary += f"; {len(matches)} matches found"
    if truncated:
        summary += " (truncated)"

    evidence = tuple(
        _format_match_evidence(matches, include_column=True, limit=_ENTRY_LIMIT)
    )
    signals = _compact_signal(
        kind="filesystem_search",
        operation="search_text",
        path=path,
        query=query,
        match_count=len(matches),
        truncated=truncated,
    )
    return DeterministicCompressionResult(
        summary=_summary(summary),
        key_findings=tuple(_entry_findings(matches, label="match")),
        structured_signals=(signals,),
        decision_evidence=evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def _adapt_path_collection(
    *,
    operation: str,
    metadata_key: str,
    item_key: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> DeterministicCompressionResult:
    collection = _mapping_or_empty(metadata.get(metadata_key))
    error = _metadata_error(metadata, collection)
    path = _first_text(parameters.get("path"))
    if error:
        return _error_result(operation=operation, path=path, error=error)
    if not collection and not path:
        return DeterministicCompressionResult.none(
            fallback_reason=f"no_filesystem_{operation}_metadata",
        )

    entries = _mapping_list(collection.get(item_key))
    entry_count = as_int(collection.get("entry_count"))
    count = entry_count if entry_count is not None else len(entries)
    truncated = bool(collection.get("truncated"))
    verb = "Found" if operation == "find_paths" else "Listed"
    summary = f"{verb} {count} filesystem paths under {path or 'filesystem path'}"
    if truncated:
        summary += " (truncated)"

    signals = _compact_signal(
        kind="filesystem_paths",
        operation=operation,
        path=path,
        entry_count=count,
        truncated=truncated,
    )
    return DeterministicCompressionResult(
        summary=_summary(summary),
        key_findings=tuple(_entry_findings(entries, label="path")),
        structured_signals=(signals,),
        completeness="partial",
        lossiness_risk="low",
    )


def _adapt_stat_path(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> DeterministicCompressionResult:
    fs_stat = _mapping_or_empty(metadata.get(_STAT_METADATA_KEY))
    error = _metadata_error(metadata, fs_stat)
    path = _first_text(fs_stat.get("path"), parameters.get("path"))
    if error:
        return _error_result(operation="stat_path", path=path, error=error)
    if not fs_stat and not path:
        return DeterministicCompressionResult.none(
            fallback_reason="no_filesystem_stat_metadata",
        )

    entry_type = _first_text(fs_stat.get("type"))
    size_bytes = as_int(fs_stat.get("size_bytes"))
    details = []
    if entry_type:
        details.append(entry_type)
    if size_bytes is not None:
        details.append(f"{size_bytes} bytes")
    summary = f"Stat inspected {path or 'filesystem path'}"
    if details:
        summary += f" ({', '.join(details)})"

    signals = _compact_signal(
        kind="filesystem_stat",
        operation="stat_path",
        path=path,
        entry_type=entry_type,
        size_bytes=size_bytes,
    )
    return DeterministicCompressionResult(
        summary=_summary(summary),
        key_findings=tuple(dedupe_string_list(details, limit=_ENTRY_LIMIT)),
        structured_signals=(signals,),
        completeness="partial",
        lossiness_risk="low",
    )


def _adapt_mutation_tool(
    *,
    tool_name: str,
    metadata_key: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> DeterministicCompressionResult:
    mutation = _mapping_or_empty(metadata.get(metadata_key))
    error = _compact_error(metadata=metadata, raw_result=raw_result, operation=mutation)
    path = _first_text(mutation.get("path"), parameters.get("path"), parameters.get("dest"))
    action = _first_text(mutation.get("action")) or _operation_label(tool_name)
    operation = _operation_label(tool_name)
    extra = _mapping_or_empty(mutation.get("extra"))

    if error:
        return _error_result(operation=operation, path=path, error=error)
    if not mutation and not path:
        return DeterministicCompressionResult.none(
            fallback_reason=f"no_{metadata_key}_metadata",
        )

    affected_paths = _affected_paths(path=path, parameters=parameters, extra=extra)
    bytes_changed = as_int(mutation.get("bytes_changed"))
    summary = f"{operation} {action}"
    if affected_paths:
        summary += f" {', '.join(affected_paths)}"
    if bytes_changed is not None:
        summary += f" ({bytes_changed} bytes changed)"

    findings = _mutation_findings(
        operation=operation,
        action=action,
        affected_paths=affected_paths,
        bytes_changed=bytes_changed,
        mutation=mutation,
    )
    signals = _compact_signal(
        kind="filesystem_mutation",
        operation=operation,
        action=action,
        path=path,
        affected_paths=affected_paths,
        bytes_changed=bytes_changed,
        start_line=as_int(mutation.get("start_line")),
        end_line=as_int(mutation.get("end_line")),
        lines_affected=as_int(mutation.get("lines_affected")),
    )
    evidence = tuple(_mutation_evidence(operation=operation, mutation=mutation))
    return DeterministicCompressionResult(
        summary=_summary(summary),
        key_findings=tuple(findings),
        structured_signals=(signals,),
        decision_evidence=evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    """Return a mapping value or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> List[Mapping[str, Any]]:
    """Return mapping list items only."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _first_text(*values: Any) -> Optional[str]:
    """Return the first non-empty stripped text value."""

    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _summary(value: Any) -> str:
    """Return a one-line bounded summary."""

    text = compact_evidence_line(value)
    if len(text) <= COMPACT_SUMMARY_MAX_CHARS:
        return text
    return text[: max(COMPACT_SUMMARY_MAX_CHARS - 3, 0)].rstrip() + "..."


def _operation_label(tool_name: str) -> str:
    """Return the filesystem operation label from a canonical tool id."""

    return tool_name.rsplit(".", 1)[-1]


def _read_mode_for_tool(
    *,
    tool_name: str,
    fs_read: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> Optional[str]:
    mode = _first_text(fs_read.get("read_mode_used"), parameters.get("read_mode"))
    if mode:
        return mode
    if tool_name == "filesystem.read_head":
        return "head"
    if tool_name == "filesystem.read_tail":
        return "tail"
    if tool_name == "filesystem.grep":
        return "grep"
    return None


def _read_key_findings(
    *,
    path: Optional[str],
    mode: Optional[str],
    fs_read: Mapping[str, Any],
) -> List[str]:
    candidates: List[str] = []
    if path:
        candidates.append(f"path: {path}")
    if mode:
        candidates.append(f"mode: {mode}")
    line_range = fs_read.get("line_range")
    if isinstance(line_range, (list, tuple)) and len(line_range) >= 2:
        start = as_int(line_range[0])
        end = as_int(line_range[1])
        if start is not None and end is not None:
            candidates.append(f"line_range: {start}-{end}")
    total_lines = as_int(fs_read.get("total_lines"))
    if total_lines is not None:
        candidates.append(f"total_lines: {total_lines}")
    if fs_read.get("truncated"):
        candidates.append("truncated: true")
    return dedupe_string_list(candidates, limit=_ENTRY_LIMIT)


def _prefix_path_to_line_evidence(
    *,
    path: Optional[str],
    values: Any,
) -> List[str]:
    if not isinstance(values, list):
        return []
    evidence: List[str] = []
    seen: set[str] = set()
    for item in values:
        line = compact_evidence_line(item)
        if not line:
            continue
        prefixed = f"{path}:{line}" if path and not line.startswith(f"{path}:") else line
        if prefixed in seen:
            continue
        seen.add(prefixed)
        evidence.append(prefixed)
        if len(evidence) >= _ENTRY_LIMIT:
            break
    return evidence


def _format_match_evidence(
    matches: Iterable[Mapping[str, Any]],
    *,
    include_column: bool,
    limit: int,
) -> List[str]:
    evidence: List[str] = []
    seen: set[str] = set()
    for item in matches:
        path = _first_text(item.get("path"))
        line = as_int(item.get("line"))
        snippet = _first_text(item.get("snippet"))
        if not path or line is None or not snippet:
            continue
        column = as_int(item.get("column")) if include_column else None
        locator = f"{path}:{line}"
        if column is not None:
            locator += f":{column}"
        compact = compact_evidence_line(f"{locator}:{snippet}")
        if compact in seen:
            continue
        seen.add(compact)
        evidence.append(compact)
        if len(evidence) >= limit:
            break
    return evidence


def _entry_findings(
    entries: Iterable[Mapping[str, Any]],
    *,
    label: str,
) -> List[str]:
    candidates: List[str] = []
    for entry in entries:
        path = _first_text(entry.get("path"))
        if not path:
            continue
        details = []
        entry_type = _first_text(entry.get("type"))
        if entry_type:
            details.append(entry_type)
        size_bytes = as_int(entry.get("size_bytes"))
        if size_bytes is not None:
            details.append(f"{size_bytes} bytes")
        suffix = f" ({', '.join(details)})" if details else ""
        candidates.append(f"{label}: {path}{suffix}")
    return dedupe_string_list(candidates, limit=_ENTRY_LIMIT)


def _affected_paths(
    *,
    path: Optional[str],
    parameters: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> List[str]:
    candidates = [
        path,
        parameters.get("src"),
        parameters.get("dest"),
        extra.get("source"),
        extra.get("destination"),
    ]
    return dedupe_string_list(
        (str(item).strip() for item in candidates if item is not None),
        limit=_ENTRY_LIMIT,
    )


def _mutation_findings(
    *,
    operation: str,
    action: str,
    affected_paths: List[str],
    bytes_changed: Optional[int],
    mutation: Mapping[str, Any],
) -> List[str]:
    candidates: List[str] = [
        f"operation: {operation}",
        f"action: {action}",
    ]
    if affected_paths:
        candidates.append(f"affected_paths: {', '.join(affected_paths)}")
    if bytes_changed is not None:
        candidates.append(f"bytes_changed: {bytes_changed}")
    for key in ("mode", "start_line", "end_line", "lines_affected", "backup_created"):
        value = mutation.get(key)
        if value is not None:
            candidates.append(f"{key}: {value}")
    return dedupe_string_list(candidates, limit=_ENTRY_LIMIT)


def _mutation_evidence(
    *,
    operation: str,
    mutation: Mapping[str, Any],
) -> List[str]:
    message = _first_text(mutation.get("message"))
    if not message:
        return []
    return [compact_evidence_line(f"{operation}: {message}")]


def _compact_signal(**values: Any) -> Dict[str, Any]:
    """Return a compact structured signal with empty values removed."""

    signal: Dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if value == []:
            continue
        signal[key] = value
    return signal


def _metadata_error(
    metadata: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> Optional[str]:
    return _first_text(operation.get("error"), metadata.get("error"))


def _compact_error(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    operation: Mapping[str, Any] | None = None,
) -> Optional[str]:
    operation = operation or {}
    success = raw_result.get("success")
    status = _first_text(raw_result.get("status"))
    explicit = _first_text(operation.get("error"), metadata.get("error"))
    if explicit:
        return explicit
    if success is False or status in {"error", "failed", "timeout", "cancelled"}:
        return status or "filesystem operation failed"
    return None


def _error_result(
    *,
    operation: str,
    path: Optional[str],
    error: str,
) -> DeterministicCompressionResult:
    target = f" for {path}" if path else ""
    compact_error = compact_evidence_line(error)
    return DeterministicCompressionResult(
        summary=_summary(f"{operation} failed{target}: {compact_error}"),
        errors=(compact_error,),
        structured_signals=(
            _compact_signal(
                kind="filesystem_error",
                operation=operation,
                path=path,
                error=compact_error,
            ),
        ),
        completeness="partial",
        lossiness_risk="low",
    )


register_filesystem_adapters()
