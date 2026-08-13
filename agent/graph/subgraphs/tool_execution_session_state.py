"""Process-local state and transcript storage for live tool execution sessions.

This module owns active-session lifecycle, evidence aggregation, pending shell
input, and bounded transcript compaction. Graph wiring and runtime I/O remain
in the tool-execution session subgraph.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    aggregate_compact_evidence_rows,
)
from runtime_shared.shell_capabilities import SHELL_WRITE_STDIN_TOOL_ID
from runtime_shared.shell_session_contracts import ShellProcessStatus

_TRANSCRIPT_MAX_ENTRIES = 24
_TRANSCRIPT_MAX_CHARS = 12_000
_TRANSCRIPT_TRUNCATION_MARKER = "...[truncated]...\n"
_RUNTIME_EXECUTION_SESSIONS: dict[str, dict[str, Any]] = {}
SHELL_STDIN_REDACTED_MARKER = "<SHELL_STDIN_REDACTED>"


def begin_execution_session_state(
    *,
    sequence_id: str,
    originating_tool_id: str,
    originating_parameters: Mapping[str, Any],
) -> None:
    """Start one bounded process-local execution session atomically."""

    normalized = str(sequence_id or "").strip()
    if not normalized:
        raise ValueError("Shell interaction transcript requires a sequence id")
    command = str(originating_parameters.get("command") or "").strip()
    _RUNTIME_EXECUTION_SESSIONS[normalized] = {
        "sequence_id": normalized,
        "originating_tool_id": str(originating_tool_id or "").strip(),
        "originating_command": command,
        "evidence_rows": [],
        "entries": [],
        "pending_inputs": {},
        "interaction_decisions": 0,
        "compacted": False,
        "omitted_entries": 0,
    }


def append_execution_session_evidence(
    sequence_id: str,
    evidence: EvidenceView,
) -> None:
    """Append deduplicated evidence rows to an active execution session."""

    normalized = str(sequence_id or "").strip()
    session = _RUNTIME_EXECUTION_SESSIONS.get(normalized)
    if session is None:
        raise KeyError(f"Unknown runtime execution session: {normalized}")
    rows = session["evidence_rows"]
    known_call_ids = {
        str(row.get("tool_call_id") or "").strip()
        for row in rows
        if str(row.get("tool_call_id") or "").strip()
    }
    for row in evidence.rows:
        call_id = str(row.get("tool_call_id") or "").strip()
        if call_id and call_id in known_call_ids:
            continue
        rows.append(dict(row))
        if call_id:
            known_call_ids.add(call_id)


def finish_execution_session_state(
    sequence_id: str,
    *,
    tool_batch_id: str,
) -> Mapping[str, Any]:
    """Remove one complete session and return its aggregate evidence."""

    normalized = str(sequence_id or "").strip()
    final_batch_id = str(tool_batch_id or "").strip()
    if not final_batch_id:
        raise ValueError("Final tool batch id is required")
    session = _RUNTIME_EXECUTION_SESSIONS.pop(normalized, None)
    if session is None:
        raise KeyError(f"Unknown runtime execution session: {normalized}")
    return aggregate_compact_evidence_rows(
        tool_batch_id=final_batch_id,
        rows=session["evidence_rows"],
    )


def abort_execution_session_state(sequence_id: str) -> None:
    """Discard all process-local state for an interrupted execution session."""

    _RUNTIME_EXECUTION_SESSIONS.pop(str(sequence_id or "").strip(), None)


def append_shell_interaction_transcript(
    *,
    sequence_id: str,
    evidence: EvidenceView,
    metadata: Mapping[str, Any],
) -> None:
    """Append compact evidence rows to the current process-local transcript."""

    normalized = str(sequence_id or "").strip()
    transcript = _RUNTIME_EXECUTION_SESSIONS.get(normalized)
    if transcript is None:
        return
    pending_inputs = transcript.get("pending_inputs")
    if not isinstance(pending_inputs, MutableMapping):
        pending_inputs = {}
        transcript["pending_inputs"] = pending_inputs
    params_by_call_id = _tool_parameters_by_call_id(metadata)
    for row in evidence.rows:
        compact = row.get("compact_tool_result")
        compact_map = compact if isinstance(compact, Mapping) else {}
        call_id = str(row.get("tool_call_id") or "").strip()
        parameters = params_by_call_id.get(call_id, {})
        chars = ""
        if str(row.get("tool_id") or "") == SHELL_WRITE_STDIN_TOOL_ID:
            chars = str(
                pending_inputs.pop(call_id, "") or parameters.get("chars") or ""
            )
        transcript["entries"].append(
            {
                "tool_id": str(row.get("tool_id") or ""),
                "tool_call_id": call_id,
                "input": chars or None,
                "stdout": str(compact_map.get("stdout") or ""),
                "stdout_ends_with_newline": bool(
                    compact_map.get("stdout_ends_with_newline")
                ),
                "stderr": str(compact_map.get("stderr") or ""),
                "boundary": infer_shell_interaction_boundary(compact_map),
                "process_status": compact_map.get("process_status"),
                "session_id": compact_map.get("session_id"),
                "exit_code": compact_map.get("exit_code"),
                "truncated": bool(compact_map.get("truncated")),
            }
        )
    _compact_shell_interaction_transcript(transcript)


def read_shell_interaction_transcript(sequence_id: str) -> Mapping[str, Any] | None:
    """Return a copy of the process-local transcript for decisions and tests."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None:
        return None
    copied = dict(transcript)
    copied["entries"] = [dict(entry) for entry in transcript.get("entries", [])]
    copied.pop("evidence_rows", None)
    copied.pop("pending_inputs", None)
    copied.pop("interaction_decisions", None)
    return copied


def consume_shell_interaction_decision(
    sequence_id: str,
    *,
    limit: int,
) -> bool:
    """Reserve one reasoning decision within a live session's fixed budget."""

    session = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if session is None:
        return False
    used = int(session.get("interaction_decisions") or 0)
    if used >= max(0, int(limit)):
        return False
    session["interaction_decisions"] = used + 1
    return True


def remember_shell_input(*, sequence_id: str, call_id: str, chars: str) -> None:
    """Keep exact stdin only in the process-local transcript cache."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None or not call_id or not chars:
        return
    pending_inputs = transcript.get("pending_inputs")
    if not isinstance(pending_inputs, MutableMapping):
        pending_inputs = {}
        transcript["pending_inputs"] = pending_inputs
    pending_inputs[call_id] = chars


def read_shell_input(*, sequence_id: str, call_id: str) -> str | None:
    """Return exact pending stdin without moving it into serializable state."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None or not call_id:
        return None
    pending_inputs = transcript.get("pending_inputs")
    if not isinstance(pending_inputs, Mapping):
        return None
    chars = pending_inputs.get(call_id)
    return chars if isinstance(chars, str) and chars else None


def materialize_terminal_session_output(
    aggregate: Mapping[str, Any],
    *,
    transcript: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Copy bounded session streams into the aggregate's terminal compact row."""

    rows = [
        dict(row)
        for row in aggregate.get("results", [])
        if isinstance(row, Mapping)
    ]
    if not rows:
        return dict(aggregate)

    entries = [
        entry for entry in transcript.get("entries", []) if isinstance(entry, Mapping)
    ]
    terminal_row = dict(rows[-1])
    terminal_compact = terminal_row.get("compact_tool_result")
    compact = dict(terminal_compact) if isinstance(terminal_compact, Mapping) else {}
    compact["stdout"] = _materialize_stdout(entries)
    compact["stderr"] = "".join(str(entry.get("stderr") or "") for entry in entries)
    compact["truncated"] = bool(
        transcript.get("compacted")
        or any(bool(entry.get("truncated")) for entry in entries)
    )
    terminal_row["compact_tool_result"] = compact
    rows[-1] = terminal_row

    materialized = dict(aggregate)
    materialized["results"] = rows
    return materialized


def _materialize_stdout(entries: Sequence[Mapping[str, Any]]) -> str:
    """Join stdout deltas while restoring deferred inter-window separators."""
    stdout_parts: list[str] = []
    for index, entry in enumerate(entries):
        stdout = str(entry.get("stdout") or "")
        if not stdout:
            continue
        stdout_parts.append(stdout)
        if not bool(entry.get("stdout_ends_with_newline")):
            continue
        later_stdout = next(
            (
                str(later.get("stdout") or "")
                for later in entries[index + 1 :]
                if str(later.get("stdout") or "")
            ),
            "",
        )
        if later_stdout and not stdout.endswith("\n") and not later_stdout.startswith("\n"):
            stdout_parts.append("\n")
    return "".join(stdout_parts)


def infer_shell_interaction_boundary(compact: Mapping[str, Any]) -> str:
    """Infer a lifecycle boundary when the runtime did not supply one."""

    boundary = str(compact.get("interaction_boundary") or "").strip().lower()
    if boundary:
        return boundary
    process_status = str(compact.get("process_status") or "").strip().lower()
    if process_status and process_status != ShellProcessStatus.RUNNING.value:
        return "terminal"
    if compact.get("stdout") or compact.get("stderr"):
        return "output_available"
    return "quiet_boundary"


def _tool_parameters_by_call_id(
    metadata: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    plan = metadata.get("planner_plan")
    batch = plan.get("tool_batch") if isinstance(plan, Mapping) else None
    calls = batch.get("tool_calls") if isinstance(batch, Mapping) else None
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
        return result
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        call_id = str(call.get("tool_call_id") or "").strip()
        params = call.get("parameters")
        if call_id and isinstance(params, Mapping):
            result[call_id] = params
    return result


def _compact_shell_interaction_transcript(
    transcript: MutableMapping[str, Any],
) -> None:
    entries = list(transcript.get("entries") or [])
    while len(entries) > _TRANSCRIPT_MAX_ENTRIES:
        entries.pop(0)
        transcript["omitted_entries"] = int(transcript.get("omitted_entries") or 0) + 1
        transcript["compacted"] = True
    while (
        _transcript_char_count(transcript, entries) > _TRANSCRIPT_MAX_CHARS
        and len(entries) > 2
    ):
        entries.pop(0)
        transcript["omitted_entries"] = int(transcript.get("omitted_entries") or 0) + 1
        transcript["compacted"] = True
    _truncate_transcript_text_to_limit(transcript, entries)
    transcript["entries"] = entries


def _truncate_transcript_text_to_limit(
    transcript: MutableMapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> None:
    """Enforce the character cap while retaining the newest output tails."""

    overflow = _transcript_char_count(transcript, entries) - _TRANSCRIPT_MAX_CHARS
    if overflow <= 0:
        return

    targets: list[tuple[MutableMapping[str, Any], str]] = [
        (transcript, "originating_command")
    ]
    for entry in entries:
        if not isinstance(entry, MutableMapping):
            continue
        targets.extend((entry, field) for field in ("input", "stdout", "stderr"))
    targets.sort(
        key=lambda item: len(str(item[0].get(item[1]) or "")),
        reverse=True,
    )

    for target, field in targets:
        text = str(target.get(field) or "")
        if not text:
            continue
        removed = min(overflow, len(text))
        target[field] = _truncate_text_head(text, len(text) - removed)
        if target is not transcript:
            target["truncated"] = True
        transcript["compacted"] = True
        overflow -= removed
        if overflow <= 0:
            return


def _truncate_text_head(text: str, keep_chars: int) -> str:
    """Keep the most recent characters, adding a marker when it fits."""

    if keep_chars <= 0:
        return ""
    if len(text) <= keep_chars:
        return text
    if keep_chars <= len(_TRANSCRIPT_TRUNCATION_MARKER):
        return text[-keep_chars:]
    tail_chars = keep_chars - len(_TRANSCRIPT_TRUNCATION_MARKER)
    return _TRANSCRIPT_TRUNCATION_MARKER + text[-tail_chars:]


def _transcript_char_count(
    transcript: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> int:
    total = len(str(transcript.get("originating_command") or ""))
    for entry in entries:
        total += len(str(entry.get("input") or ""))
        total += len(str(entry.get("stdout") or ""))
        total += len(str(entry.get("stderr") or ""))
    return total
