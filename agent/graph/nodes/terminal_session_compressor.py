"""Compress one completed interactive-shell aggregate before reasoning.

This node owns the boundary between raw, process-local shell coordination and
the existing compact evidence consumed by PTR or a subagent observation. It is
registered by caller graphs after the execution-session subgraph and is a
no-op for ordinary terminal tool executions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, MutableMapping
from typing import Any

from backend.services.metrics.utils import safe_gauge, safe_inc
from core.llm import ROLE_TOOL_OUTPUT_COMPRESSOR
from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    read_compact_evidence,
    register_runtime_compact_evidence,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.shell_capabilities import SHELL_SESSION_START_TOOL_IDS

from ..compression import compact_output_size_bytes, compress_tool_output
from ..state import InteractiveState
from ..subgraphs.tool_execution_runtime.observability import (
    record_compression_observability_metrics,
)
from ..utils.llm_resolver import resolve_llm_client

logger = logging.getLogger(__name__)

_TERMINAL_SESSION_STATE_KEYS = (
    "process_status",
    "session_status",
    "interaction_boundary",
    "session_id",
    "exit_code",
    "stdin_available",
    "truncated",
    "error_code",
    "artifacts",
    "artifact_refs",
    "metadata",
)


def _is_completed_interactive_session(evidence: EvidenceView) -> bool:
    """Return true only for aggregates that crossed a live process boundary."""

    if evidence.raw.get("execution_session_aggregate") is not True:
        return False
    if not evidence.rows:
        return False
    originating_tool = str(evidence.rows[0].get("tool_id") or "").strip()
    if originating_tool not in SHELL_SESSION_START_TOOL_IDS:
        return False
    terminal_compact = evidence.rows[-1].get("compact_tool_result")
    if (
        isinstance(terminal_compact, Mapping)
        and isinstance(terminal_compact.get("compression"), Mapping)
    ):
        return False
    return any(
        str(compact.get("process_status") or "").strip().lower() == "running"
        for row in evidence.rows
        if isinstance((compact := row.get("compact_tool_result")), Mapping)
    )


def _terminal_rows(
    evidence: EvidenceView,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]] | None:
    """Return copied aggregate rows plus originating and terminal rows."""

    rows = [dict(row) for row in evidence.rows if isinstance(row, Mapping)]
    if not rows:
        return None
    return rows, rows[0], rows[-1]


def _replace_terminal_durable_compact(
    metadata: MutableMapping[str, Any],
    *,
    tool_batch_id: str,
    terminal_call_id: str,
    compact: Mapping[str, Any],
) -> None:
    """Update the durable assessment projection when the terminal row is durable."""

    batch = metadata.get("last_tool_result_compact_batch")
    if not isinstance(batch, Mapping):
        return
    if str(batch.get("tool_batch_id") or "").strip() != tool_batch_id:
        return

    rows = [
        dict(row)
        for row in batch.get("results", [])
        if isinstance(row, Mapping)
    ]
    if not rows:
        return
    terminal_index = next(
        (
            index
            for index in range(len(rows) - 1, -1, -1)
            if str(rows[index].get("tool_call_id") or "").strip()
            == terminal_call_id
        ),
        len(rows) - 1,
    )
    masked = mask_durable_secrets(
        dict(compact),
        source="last_tool_result_compact",
    )
    terminal = dict(rows[terminal_index])
    terminal["compact_tool_result"] = masked
    terminal["summary"] = str(masked.get("summary") or terminal.get("summary") or "")
    rows[terminal_index] = terminal

    durable_batch = dict(batch)
    durable_batch["results"] = rows
    metadata["last_tool_result_compact_batch"] = durable_batch
    metadata["last_tool_result_compact"] = masked


def _append_usage_record(interactive: InteractiveState, usage_record: Any) -> None:
    """Attach compressor usage to the existing graph trace."""

    if not isinstance(usage_record, Mapping):
        return
    interactive.trace.usage_records.append(dict(usage_record))


async def compress_terminal_execution_session_output(
    state: Mapping[str, Any] | InteractiveState,
    context: Any = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compress a completed live-session aggregate exactly once."""

    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    evidence = read_compact_evidence(metadata, prefer_runtime=True)
    if evidence is None or not _is_completed_interactive_session(evidence):
        return interactive.as_graph_update()

    selected = _terminal_rows(evidence)
    if selected is None:
        return interactive.as_graph_update()
    rows, originating_row, terminal_row = selected
    terminal_compact = terminal_row.get("compact_tool_result")
    if not isinstance(terminal_compact, Mapping):
        raise RuntimeError("Interactive session terminal row has no compact result")

    originating_tool = str(originating_row.get("tool_id") or "").strip()
    if not originating_tool:
        raise RuntimeError("Interactive session aggregate has no originating tool")

    raw_result = dict(terminal_compact)
    raw_result["tool"] = originating_tool
    raw_result["tool_id"] = originating_tool
    raw_result["status"] = str(
        terminal_row.get("status") or raw_result.get("status") or ""
    )
    raw_result["success"] = bool(
        terminal_row.get("success", raw_result.get("success", False))
    )
    intent = str(originating_row.get("intent") or "").strip()
    if intent:
        raw_result["tool_intent"] = intent

    llm_client = None
    try:
        llm_client = resolve_llm_client(
            metadata,
            context,
            config=config,
            role=ROLE_TOOL_OUTPUT_COMPRESSOR,
        )
    except Exception as exc:
        logger.warning(
            "Interactive session terminal compressor unavailable; using deterministic fallback: %s",
            exc,
        )

    logger.info(
        "Compressing terminal interactive session aggregate (tool=%s rows=%s)",
        originating_tool,
        len(rows),
    )
    started = time.perf_counter()
    compression_result = await compress_tool_output(
        tool_name=originating_tool,
        raw_result=raw_result,
        artifact_path=None,
        execution_id=None,
        llm_client=llm_client,
    )
    compact_output = compression_result.compact_output
    compact = compact_output.to_dict()
    for key in _TERMINAL_SESSION_STATE_KEYS:
        if key in terminal_compact:
            compact[key] = terminal_compact[key]

    record_compression_observability_metrics(
        source=compact_output.compression.source,
        fallback_reason=compact_output.compression.fallback_reason,
        duration_seconds=time.perf_counter() - started,
        compact_size_bytes=compact_output_size_bytes(compact_output),
        gauge_fn=safe_gauge,
        inc_fn=safe_inc,
    )
    _append_usage_record(interactive, compression_result.usage_record)

    terminal_call_id = str(terminal_row.get("tool_call_id") or "").strip()
    terminal_row["compact_tool_result"] = compact
    terminal_row["summary"] = str(
        compact.get("summary") or terminal_row.get("summary") or ""
    )
    rows[-1] = terminal_row
    aggregate = dict(evidence.raw)
    aggregate["results"] = rows
    register_runtime_compact_evidence(aggregate, single_compact=compact)
    _replace_terminal_durable_compact(
        metadata,
        tool_batch_id=str(aggregate.get("tool_batch_id") or "").strip(),
        terminal_call_id=terminal_call_id,
        compact=compact,
    )

    interactive.facts.metadata = metadata
    logger.info(
        "Compressed terminal interactive session aggregate (tool=%s source=%s)",
        originating_tool,
        compact_output.compression.source,
    )
    return interactive.as_graph_update()


__all__ = ["compress_terminal_execution_session_output"]
