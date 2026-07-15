"""Result-state projection helpers for tool execution runtime extraction.

This module centralizes post-execution compact-result projection and metadata
updates, including section-snapshot current-turn phase memory records for
post-tool reasoning continuity.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from core.prompts.builders.post_tool.last_tool import (
    extract_last_tool_sections,
    iter_renderable_last_tool_sections,
)

from ...memory.findings import extract_observed_findings
from ...memory.target_resolution import (
    RUNTIME_TOOL_TARGET_FIELD_SPECS,
    coerce_target_value,
    resolve_target_from_working_memory,
)
from ...utils import iteration_memory as _iteration_memory
from ...utils.llm_resolver import ROLE_TOOL_OUTPUT_COMPRESSOR
from ...utils.tool_optimization import ToolExecution, get_scan_phase, record_tool_execution
from runtime_shared.durable_secret_masking import mask_durable_secrets

_CURRENT_TURN_RUNTIME_CONTROLS_KEY = "current_turn_runtime_controls"


def _append_compression_usage_record(
    interactive: Any,
    usage_record: Optional[Mapping[str, Any]],
    *,
    logger: Any,
) -> None:
    """Append successful compressor LLM usage into the graph trace."""
    if not usage_record:
        return

    trace = getattr(interactive, "trace", None)
    if trace is None:
        logger.warning("[TOOL_EXECUTION] Compressor usage dropped: trace missing")
        return

    if not hasattr(trace, "usage_records") or getattr(trace, "usage_records") is None:
        setattr(trace, "usage_records", [])

    usage_records = getattr(trace, "usage_records")
    if not isinstance(usage_records, list):
        logger.warning("[TOOL_EXECUTION] Compressor usage dropped: usage_records not list")
        return

    usage_records.append(dict(usage_record))


def sanitize_tool_result_for_metadata(
    raw_result: Mapping[str, Any],
    *,
    compact_sanitized_result_keys: Sequence[str],
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Keep only compact-safe lifecycle fields for state metadata."""
    sanitized: Dict[str, Any] = {}
    for key in compact_sanitized_result_keys:
        if key in raw_result:
            value = raw_result[key]
            sanitized[key] = (
                dict(value) if key == "parameters" and isinstance(value, Mapping) else value
            )
    if tool_name and "tool" not in sanitized:
        sanitized["tool"] = tool_name
    return sanitized


def compact_observation_text(
    compact_result: Mapping[str, Any],
    fallback: Optional[str] = None,
) -> str:
    """Return a compact, prompt-safe observation string."""
    summary = str(compact_result.get("summary") or "").strip()
    if summary:
        return summary
    fallback_text = str(fallback or "").strip()
    return fallback_text or "Tool executed."


def _append_tool_execution_record(
    *,
    facts: Any,
    outcome: Any,
    resolved_tool_id: str,
    tool_call_id: str,
    turn_sequence: Optional[int],
    workspace_id: Optional[str],
    artifact_refs_for_memory: Sequence[Mapping[str, Any]],
    artifact_projection_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Persist a compact per-call execution record for provenance/telemetry joins."""
    result_map = dict(outcome.result or {})
    metadata_map = (
        dict(result_map.get("metadata"))
        if isinstance(result_map.get("metadata"), Mapping)
        else {}
    )
    if isinstance(artifact_projection_metadata, Mapping):
        for key in ("artifact_scope", "artifact_promotion_status", "artifact_visibility"):
            value = artifact_projection_metadata.get(key)
            if isinstance(value, str) and value.strip():
                metadata_map[key] = value.strip()
    route_policy = (
        dict(metadata_map.get("route_policy"))
        if isinstance(metadata_map.get("route_policy"), Mapping)
        else {}
    )
    lane = str(route_policy.get("selected_lane") or "unknown").strip() or "unknown"
    authority = (
        str(route_policy.get("selected_authority") or "unknown").strip() or "unknown"
    )

    status = str(result_map.get("status") or "").strip().lower()
    if not status:
        status = "success" if bool(result_map.get("success")) else "error"

    duration_seconds = outcome.duration
    if not isinstance(duration_seconds, (int, float)):
        duration_seconds = result_map.get("duration")
    if not isinstance(duration_seconds, (int, float)):
        duration_seconds = 0.0
    duration_seconds = max(0.0, float(duration_seconds))

    artifacts = result_map.get("artifacts")
    has_artifacts = (isinstance(artifacts, list) and bool(artifacts)) or bool(artifact_refs_for_memory)
    artifact_scope = metadata_map.get("artifact_scope")
    artifact_promotion_status = metadata_map.get("artifact_promotion_status")
    artifact_visibility = metadata_map.get("artifact_visibility")
    if has_artifacts:
        if not isinstance(artifact_scope, str) or not artifact_scope.strip():
            artifact_scope = "runner_local"
        if not isinstance(artifact_promotion_status, str) or not artifact_promotion_status.strip():
            artifact_promotion_status = "unpromoted"
        if not isinstance(artifact_visibility, str) or not artifact_visibility.strip():
            artifact_visibility = "runner_workspace_only"

    stdout_excerpt = mask_durable_secrets(
        str(result_map.get("stdout_excerpt") or ""),
        source="tool_execution_record_stdout_excerpt",
    )
    stderr_excerpt = mask_durable_secrets(
        str(result_map.get("stderr_excerpt") or ""),
        source="tool_execution_record_stderr_excerpt",
    )

    record: Dict[str, Any] = {
        "tool": resolved_tool_id,
        "tool_call_id": tool_call_id,
        "turn_sequence": turn_sequence,
        "status": status,
        "success": bool(result_map.get("success")),
        "duration_ms": int(duration_seconds * 1000),
        "exit_code": result_map.get("exit_code"),
        "stdout_excerpt": stdout_excerpt if isinstance(stdout_excerpt, str) else "",
        "stderr_excerpt": stderr_excerpt if isinstance(stderr_excerpt, str) else "",
        "lane": lane,
        "authority": authority,
        "runtime_job_id": metadata_map.get("runtime_job_id"),
        "tool_command_runtime_job_id": metadata_map.get("tool_command_runtime_job_id"),
        "task_runtime_job_id": metadata_map.get("task_runtime_job_id"),
        "command_id": metadata_map.get("command_id"),
        "runner_id": metadata_map.get("runner_id"),
        "workspace_id": workspace_id,
        "artifact_scope": artifact_scope,
        "artifact_promotion_status": artifact_promotion_status,
        "artifact_visibility": artifact_visibility,
        "artifact_refs": [
            dict(item) for item in artifact_refs_for_memory if isinstance(item, Mapping)
        ],
    }

    history = facts.metadata.setdefault("tool_execution_records", [])
    if not isinstance(history, list):
        history = []
        facts.metadata["tool_execution_records"] = history
    history.append(record)
    if len(history) > 100:
        del history[:-100]


def _looks_signed_url(value: Any) -> bool:
    """Return True when value resembles a pre-signed URL that should not reach prompts."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    return any(
        token in lowered
        for token in (
            "x-amz-signature=",
            "x-amz-credential=",
            "x-amz-security-token=",
            "sig=",
            "signature=",
            "signed=",
        )
    )


def _sanitize_artifact_refs_for_memory(
    refs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Drop object keys/signed URLs and keep stable artifact-id-oriented handles."""
    allowed_keys = {
        "path",
        "artifact_id",
        "execution_id",
        "tool_call_id",
        "tool_name",
        "artifact_kind",
        "label",
        "relative_path",
        "count",
        "artifact_promotion_status",
        "upload_status",
    }
    sanitized: list[dict[str, Any]] = []
    for raw_ref in refs:
        if not isinstance(raw_ref, Mapping):
            continue
        ref = {str(key): value for key, value in raw_ref.items() if str(key) in allowed_keys}
        artifact_id = ref.get("artifact_id")
        artifact_id_text = str(artifact_id).strip() if isinstance(artifact_id, str) else None

        relative_path = ref.get("relative_path")
        relative_path_text = (
            str(relative_path).strip() if isinstance(relative_path, str) and relative_path.strip() else None
        )

        raw_path = ref.get("path")
        path_text = str(raw_path).strip() if isinstance(raw_path, str) and raw_path.strip() else None
        if path_text and _looks_signed_url(path_text):
            path_text = None

        if not path_text:
            path_text = relative_path_text
        if not path_text and artifact_id_text:
            path_text = f"artifact://{artifact_id_text}"
        if not path_text:
            continue

        ref["path"] = path_text
        sanitized.append(ref)

    return sanitized


def _resolve_artifact_projection_metadata(
    *,
    existing_metadata: Mapping[str, Any],
    artifact_refs: Sequence[Mapping[str, Any]],
    has_artifacts: bool,
) -> dict[str, Any]:
    """Resolve artifact projection metadata for promoted vs unpromoted result refs."""
    if not has_artifacts:
        return {}

    existing_scope = str(existing_metadata.get("artifact_scope") or "").strip()
    existing_status = str(existing_metadata.get("artifact_promotion_status") or "").strip()
    existing_visibility = str(existing_metadata.get("artifact_visibility") or "").strip()

    promoted_refs = [
        ref
        for ref in artifact_refs
        if isinstance(ref.get("artifact_id"), str) and str(ref.get("artifact_id")).strip()
    ]
    if not promoted_refs:
        return {
            "artifact_scope": existing_scope or "runner_local",
            "artifact_promotion_status": existing_status or "unpromoted",
            "artifact_visibility": existing_visibility or "runner_workspace_only",
        }

    statuses: list[str] = []
    for ref in promoted_refs:
        status = str(ref.get("artifact_promotion_status") or "").strip().lower()
        if not status:
            upload_status = str(ref.get("upload_status") or "").strip().lower()
            if upload_status == "upload_pending":
                status = "upload_pending"
            elif upload_status in {"upload_failed", "failed"}:
                status = "upload_failed"
            elif upload_status:
                status = "ready"
        if status in {"ready", "upload_pending", "upload_failed"}:
            statuses.append(status)

    if existing_status in {"ready", "upload_pending", "upload_failed"}:
        promotion_status = existing_status
    elif "upload_failed" in statuses:
        promotion_status = "upload_failed"
    elif "upload_pending" in statuses:
        promotion_status = "upload_pending"
    else:
        promotion_status = "ready"

    return {
        "artifact_scope": "cloud_data_plane",
        "artifact_promotion_status": promotion_status,
        "artifact_visibility": "artifact_catalog",
    }


def _normalize_tool_phase_status(tool_result: Mapping[str, Any]) -> str:
    """Normalize runtime tool-result status for deterministic control signals."""
    explicit_status = str(tool_result.get("status") or "").strip().lower()
    if explicit_status:
        return explicit_status
    success = tool_result.get("success")
    if success is True:
        return "completed"
    if success is False:
        return "failed"
    exit_code = tool_result.get("exit_code")
    if isinstance(exit_code, int):
        return "completed" if exit_code == 0 else "failed"
    return "unknown"


def _errors_to_text(value: Any) -> str:
    """Render compact error payloads into classifier-friendly free text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        message = str(value.get("message") or value.get("error") or "").strip()
        code = str(value.get("code") or "").strip()
        if message and code:
            return f"{code}: {message}"
        if message:
            return message
        return code
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered = [_errors_to_text(item) for item in value]
        return "\n".join(item for item in rendered if item)
    return str(value).strip()


def _tool_failed(
    *,
    status: str,
    tool_result: Mapping[str, Any],
    compact_result: Mapping[str, Any],
) -> bool:
    """Classify deterministic tool outcome success/failure for runtime controls."""
    error_statuses = {"failed", "error", "validation_error", "timeout"}
    if status in error_statuses:
        return True
    if tool_result.get("success") is False:
        return True
    exit_code = tool_result.get("exit_code")
    if not isinstance(exit_code, int):
        compact_exit = compact_result.get("exit_code")
        exit_code = compact_exit if isinstance(compact_exit, int) else None
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    return False


def _classify_failure_category(error_text: str, exit_code: Optional[int]) -> str:
    """Classify failure category from deterministic runtime signals."""
    lowered = str(error_text or "").lower()

    if "connection refused" in lowered or "network unreachable" in lowered:
        return "network_error"
    if "permission denied" in lowered or "operation not permitted" in lowered:
        return "permission_denied"
    if exit_code == 124 or "timeout" in lowered:
        return "timeout"
    if "not found" in lowered or "command not found" in lowered:
        return "tool_unavailable"
    if "invalid" in lowered or "error" in lowered or "failed" in lowered:
        return "invalid_params"
    if not lowered.strip():
        return "empty_output"
    return "unknown"


def _evaluate_tool_phase_outcome(
    *,
    tool_result: Mapping[str, Any],
    compact_result: Mapping[str, Any],
    summary: str,
) -> Dict[str, Any]:
    """Return deterministic status/result flags for runtime control consumers."""
    status = _normalize_tool_phase_status(tool_result)
    failed = _tool_failed(status=status, tool_result=tool_result, compact_result=compact_result)

    exit_code = tool_result.get("exit_code")
    if not isinstance(exit_code, int):
        compact_exit = compact_result.get("exit_code")
        exit_code = compact_exit if isinstance(compact_exit, int) else None

    failure_text = "\n".join(
        part
        for part in (
            _errors_to_text(tool_result.get("stderr")),
            _errors_to_text(tool_result.get("error")),
            _errors_to_text(compact_result.get("errors")),
            str(compact_result.get("summary") or "").strip(),
            str(summary).strip(),
        )
        if part
    )
    failure_category = _classify_failure_category(failure_text, exit_code) if failed else None
    result = "negative"
    if failed:
        result = "timeout" if failure_category == "timeout" else "error"
    return {
        "status": status,
        "failed": failed,
        "failure_category": failure_category,
        "result": result,
    }


def _record_current_turn_unavailable_tool(
    metadata: Dict[str, Any],
    *,
    turn_sequence: Optional[int],
    tool_id: str,
    failure_category: Optional[str],
) -> None:
    """Persist current-turn unavailable-tool control state outside phase memory."""
    if failure_category != "tool_unavailable" or not isinstance(turn_sequence, int):
        return

    controls = metadata.get(_CURRENT_TURN_RUNTIME_CONTROLS_KEY)
    if (
        not isinstance(controls, Mapping)
        or controls.get("turn_sequence") != turn_sequence
    ):
        controls_map: Dict[str, Any] = {
            "turn_sequence": turn_sequence,
            "unavailable_tools": [],
        }
    else:
        raw_tools = controls.get("unavailable_tools")
        tools = []
        if isinstance(raw_tools, list):
            tools = [str(item).strip() for item in raw_tools if str(item).strip()]
        controls_map = {
            "turn_sequence": turn_sequence,
            "unavailable_tools": tools,
        }

    unavailable_tools = list(controls_map["unavailable_tools"])
    normalized_tool_id = str(tool_id or "").strip()
    if normalized_tool_id and normalized_tool_id not in unavailable_tools:
        unavailable_tools.append(normalized_tool_id)
    controls_map["unavailable_tools"] = unavailable_tools
    metadata[_CURRENT_TURN_RUNTIME_CONTROLS_KEY] = controls_map


def append_tool_phase_snapshot_from_metadata(
    *,
    facts: Any,
    turn_sequence: Optional[int],
    logger: Any,
) -> Optional[Mapping[str, Any]]:
    """Append one tool phase snapshot from finalized PTR-readable metadata."""
    if not isinstance(turn_sequence, int):
        logger.debug(
            "[TOOL_EXECUTION] Skipping tool iteration-memory append: "
            "metadata['turn_sequence'] missing or not an int (value=%r)",
            turn_sequence,
        )
        return None

    metadata = facts.metadata if isinstance(getattr(facts, "metadata", None), Mapping) else {}
    projected_sections = extract_last_tool_sections(metadata, facts, synthesized=None)
    sections = [
        {"heading": heading, "body": body}
        for heading, body in iter_renderable_last_tool_sections(projected_sections)
    ]

    try:
        tool_phase_record = _iteration_memory.append(
            metadata,
            turn_sequence=turn_sequence,
            source="tool",
            payload={"sections": sections},
        )
    except ValueError:
        logger.debug(
            "[TOOL_EXECUTION] Skipping tool iteration-memory append: "
            "final PTR last-tool projection carried no renderable sections"
        )
        return None

    logger.debug(
        "[TOOL_EXECUTION] Appended tool iteration-memory record: "
        "turn=%s phase=%s sections=%s",
        tool_phase_record.get("turn_sequence"),
        tool_phase_record.get("phase_sequence"),
        len(tool_phase_record.get("sections", [])),
    )
    return tool_phase_record


async def project_result_state(
    *,
    interactive: Any,
    facts: Any,
    outcome: Any,
    tool_name: str,
    metadata: Mapping[str, Any],
    runtime_context: Optional[Any],
    artifact_path: Optional[str],
    execution_id: Optional[Any],
    tool_call_id: str,
    turn_sequence: Optional[int],
    persisted_artifact_refs: Sequence[Mapping[str, Any]],
    compact_sanitized_result_keys: Sequence[str],
    compact_observation_text_fn: Callable[[Mapping[str, Any], Optional[str]], str],
    enrich_artifact_refs_with_provenance_fn: Callable[..., list[dict[str, Any]]],
    refresh_trace_scratchpad_fn: Callable[[Any], None],
    resolve_llm_client_fn: Callable[..., Any],
    compress_tool_output_fn: Callable[..., Any],
    compact_output_size_bytes_fn: Callable[[Any], int],
    record_compression_observability_metrics_fn: Callable[..., None],
    memory_reduce_tool_result_fn: Callable[..., Any],
    logger: Any,
    safe_inc_fn: Callable[[str], None],
    safe_gauge_fn: Callable[[str, float], None],
    config: Optional[Mapping[str, Any]] = None,
    tool_batch_id: Optional[str] = None,
    tool_intent: str = "",
    apply_to_state: bool = True,
) -> Dict[str, Any]:
    """Project compact result and metadata updates into interactive state."""
    resolved_tool_id = str(outcome.tool_id or tool_name)
    graph_metadata: Dict[str, Any] = outcome.to_graph_metadata()
    llm_client = None
    try:
        llm_client = resolve_llm_client_fn(
            metadata,
            runtime_context,
            config=config,
            role=ROLE_TOOL_OUTPUT_COMPRESSOR,
        )
    except Exception as exc:
        logger.warning(
            "[TOOL_EXECUTION] Compact compression LLM unavailable; using deterministic fallback: %s",
            exc,
        )

    compression_started = time.perf_counter()
    compression_raw_result = dict(outcome.result)
    compression_raw_result["parameters"] = dict(outcome.parameters or {})
    compression_raw_result["tool_call_id"] = tool_call_id
    if tool_batch_id:
        compression_raw_result["tool_batch_id"] = tool_batch_id
    if tool_intent:
        compression_raw_result["tool_intent"] = tool_intent
    compression_result = await compress_tool_output_fn(
        tool_name=resolved_tool_id,
        raw_result=compression_raw_result,
        artifact_path=artifact_path,
        execution_id=str(execution_id) if execution_id is not None else None,
        llm_client=llm_client,
    )
    compact_result = compression_result.compact_output
    deterministic_compact_result = getattr(
        compression_result,
        "deterministic_compact_output",
        None,
    )
    compression_duration_seconds = time.perf_counter() - compression_started
    compact_size = compact_output_size_bytes_fn(compact_result)
    record_compression_observability_metrics_fn(
        source=compact_result.compression.source,
        fallback_reason=compact_result.compression.fallback_reason,
        duration_seconds=compression_duration_seconds,
        compact_size_bytes=compact_size,
        gauge_fn=safe_gauge_fn,
        inc_fn=safe_inc_fn,
    )
    if compact_result.compression.source == "deterministic":
        logger.warning(
            "[TOOL_EXECUTION] Deterministic compact compression fallback triggered "
            "(tool=%s reason=%s)",
            resolved_tool_id,
            compact_result.compression.fallback_reason or "unknown",
        )

    compact_result_dict = compact_result.to_dict()
    deterministic_compact_result_dict = (
        deterministic_compact_result.to_dict()
        if deterministic_compact_result is not None
        else None
    )
    # Phase 9 Task 9.2: ``last_tool_result_compact`` is now authored as a
    # derived view of the per-call entry inside the batch-shaped metadata
    # (see ``batch_runner.write_compact_batch_metadata``). The projection
    # still returns ``compact_result_dict`` in its return payload so the
    # orchestrator can hand it to the batch writer.
    result_source_for_metadata = dict(outcome.result)
    result_source_for_metadata["parameters"] = dict(outcome.parameters or {})
    result_for_metadata = sanitize_tool_result_for_metadata(
        result_source_for_metadata,
        compact_sanitized_result_keys=compact_sanitized_result_keys,
        tool_name=resolved_tool_id,
    )
    if isinstance(graph_metadata, dict) and isinstance(graph_metadata.get("result"), Mapping):
        graph_metadata = dict(graph_metadata)
        graph_metadata["result"] = sanitize_tool_result_for_metadata(
            graph_metadata["result"],
            compact_sanitized_result_keys=compact_sanitized_result_keys,
            tool_name=resolved_tool_id,
        )

    artifact_refs_for_memory: list[dict[str, Any]] = []
    compact_artifact_refs = compact_result_dict.get("artifact_refs")
    if isinstance(compact_artifact_refs, list):
        artifact_refs_for_memory = [
            dict(item) for item in compact_artifact_refs if isinstance(item, Mapping)
        ]
    if not artifact_refs_for_memory and artifact_path:
        artifact_refs_for_memory = [{"path": artifact_path, "count": 1}]
    elif not artifact_refs_for_memory:
        tool_artifacts = outcome.result.get("artifacts")
        if isinstance(tool_artifacts, list):
            artifact_refs_for_memory = [
                {"path": str(path), "count": 1}
                for path in tool_artifacts
                if isinstance(path, str) and path
            ]
    if not artifact_refs_for_memory and persisted_artifact_refs:
        artifact_refs_for_memory = [
            {
                "path": str(item["path"]),
                "artifact_id": item.get("artifact_id"),
                "count": 1,
            }
            for item in persisted_artifact_refs
            if isinstance(item.get("path"), str) and str(item.get("path")).strip()
        ]
    if persisted_artifact_refs:
        artifact_refs_for_memory = enrich_artifact_refs_with_provenance_fn(
            refs=artifact_refs_for_memory,
            provenance_refs=persisted_artifact_refs,
            tool_name=resolved_tool_id,
            tool_call_id=tool_call_id,
            execution_id=str(execution_id) if execution_id is not None else None,
            turn_sequence=turn_sequence,
        )
    artifact_refs_for_memory = _sanitize_artifact_refs_for_memory(artifact_refs_for_memory)
    if artifact_refs_for_memory:
        compact_result_dict["artifact_refs"] = artifact_refs_for_memory
        if deterministic_compact_result_dict is not None:
            deterministic_compact_result_dict["artifact_refs"] = artifact_refs_for_memory

    result_metadata = (
        dict(outcome.result.get("metadata"))
        if isinstance(outcome.result.get("metadata"), Mapping)
        else {}
    )
    has_artifacts = (
        isinstance(outcome.result.get("artifacts"), list) and bool(outcome.result.get("artifacts"))
    ) or bool(artifact_refs_for_memory)
    artifact_projection_metadata = _resolve_artifact_projection_metadata(
        existing_metadata=result_metadata,
        artifact_refs=artifact_refs_for_memory,
        has_artifacts=has_artifacts,
    )
    if artifact_projection_metadata:
        result_metadata.update(artifact_projection_metadata)
        result_metadata["artifact_refs"] = artifact_refs_for_memory
        result_for_metadata["metadata"] = result_metadata
        if isinstance(graph_metadata, dict):
            graph_result = graph_metadata.get("result")
            if isinstance(graph_result, Mapping):
                graph_result_copy = dict(graph_result)
                graph_result_copy["metadata"] = result_metadata
                graph_metadata["result"] = graph_result_copy

    action_record = {
        "tool_id": resolved_tool_id,
        "params": dict(outcome.parameters),
    }
    if isinstance(turn_sequence, int):
        action_record["turn_sequence"] = turn_sequence

    projection = {
        "compact_result_dict": compact_result_dict,
        "deterministic_compact_result_dict": deterministic_compact_result_dict,
        "result_for_metadata": result_for_metadata,
        "graph_metadata": graph_metadata,
        "action_record": action_record,
        "artifact_refs_for_memory": artifact_refs_for_memory,
        "artifact_projection_metadata": artifact_projection_metadata,
        "resolved_tool_id": resolved_tool_id,
        "compression_usage_record": compression_result.usage_record,
    }
    if apply_to_state:
        apply_result_state_projection(
            interactive=interactive,
            facts=facts,
            outcome=outcome,
            projection=projection,
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            turn_sequence=turn_sequence,
            compact_observation_text_fn=compact_observation_text_fn,
            refresh_trace_scratchpad_fn=refresh_trace_scratchpad_fn,
            memory_reduce_tool_result_fn=memory_reduce_tool_result_fn,
            logger=logger,
            safe_inc_fn=safe_inc_fn,
        )
    return projection


def apply_result_state_projection(
    *,
    interactive: Any,
    facts: Any,
    outcome: Any,
    projection: Mapping[str, Any],
    execution_id: Optional[Any],
    tool_call_id: str,
    turn_sequence: Optional[int],
    compact_observation_text_fn: Callable[[Mapping[str, Any], Optional[str]], str],
    refresh_trace_scratchpad_fn: Callable[[Any], None],
    memory_reduce_tool_result_fn: Callable[..., Any],
    logger: Any,
    safe_inc_fn: Callable[[str], None],
) -> None:
    """Apply one call-local projection into shared graph state."""
    resolved_tool_id = str(projection.get("resolved_tool_id") or outcome.tool_id or "unknown_tool")
    compact_result_dict = dict(projection.get("compact_result_dict") or {})
    result_for_metadata = dict(projection.get("result_for_metadata") or {})
    graph_metadata = dict(projection.get("graph_metadata") or {})
    action_record = dict(projection.get("action_record") or {})
    artifact_refs_for_memory = [
        dict(item)
        for item in (projection.get("artifact_refs_for_memory") or [])
        if isinstance(item, Mapping)
    ]
    compression_usage_record = projection.get("compression_usage_record")
    artifact_projection_metadata = projection.get("artifact_projection_metadata")
    workspace_id_value = None
    workspace_id_raw = facts.metadata.get("workspace_id")
    if isinstance(workspace_id_raw, str) and workspace_id_raw.strip():
        workspace_id_value = workspace_id_raw.strip()

    durable_graph_metadata = _mask_durable_mapping(
        graph_metadata,
        source="tool_history_entry",
    )
    durable_result_for_metadata = _mask_durable_mapping(
        result_for_metadata,
        source="last_tool_result",
    )
    durable_compact_result = _mask_durable_mapping(
        compact_result_dict,
        source="working_memory_compact_result",
    )
    durable_action_record = _mask_durable_mapping(
        action_record,
        source="action_history_record",
    )
    durable_artifact_refs = mask_durable_secrets(
        artifact_refs_for_memory,
        source="artifact_refs_for_memory",
    )
    if not isinstance(durable_artifact_refs, list):
        durable_artifact_refs = []

    facts.metadata.setdefault("tool_history", []).append(durable_graph_metadata)
    facts.metadata["last_tool_result"] = durable_result_for_metadata
    _append_tool_execution_record(
        facts=facts,
        outcome=outcome,
        resolved_tool_id=resolved_tool_id,
        tool_call_id=tool_call_id,
        turn_sequence=turn_sequence,
        workspace_id=workspace_id_value,
        artifact_refs_for_memory=[
            dict(item) for item in durable_artifact_refs if isinstance(item, Mapping)
        ],
        artifact_projection_metadata=(
            artifact_projection_metadata if isinstance(artifact_projection_metadata, Mapping) else None
        ),
    )
    _append_compression_usage_record(
        interactive,
        compression_usage_record if isinstance(compression_usage_record, Mapping) else None,
        logger=logger,
    )

    existing_working_memory = facts.metadata.get("working_memory")
    raw_result_metadata = outcome.result.get("metadata") if isinstance(outcome.result, Mapping) else {}
    target_hint = coerce_target_value(
        outcome.parameters if isinstance(outcome.parameters, Mapping) else {},
        allow_single_label=True,
        field_specs=RUNTIME_TOOL_TARGET_FIELD_SPECS,
    ) or ""
    if not target_hint and isinstance(existing_working_memory, Mapping):
        resolved_target = resolve_target_from_working_memory(
            dict(existing_working_memory),
            intent_referent_key="intent:target",
            recent_turn_limit=4,
        )
        if isinstance(resolved_target, str):
            target_hint = resolved_target
    observed_findings = extract_observed_findings(
        raw_result_metadata if isinstance(raw_result_metadata, Mapping) else None,
        target_hint=target_hint,
        seen_at=int(time.time()),
    )
    facts.metadata["working_memory"] = memory_reduce_tool_result_fn(
        previous=existing_working_memory if isinstance(existing_working_memory, Mapping) else None,
        tool_id=resolved_tool_id,
        tool_params=_mask_durable_mapping(dict(outcome.parameters), source="working_memory_tool_params"),
        compact_envelope=durable_compact_result,
        artifact_refs=[
            dict(item) for item in durable_artifact_refs if isinstance(item, Mapping)
        ],
        execution_id=str(execution_id or tool_call_id),
        observed_findings=observed_findings,
    )
    refresh_trace_scratchpad_fn(interactive)

    action_history = facts.metadata.setdefault("action_history", [])
    action_history.append(durable_action_record)
    if len(action_history) > 10:
        action_history.pop(0)

    execution_history_data = facts.metadata.get("tool_execution_history", [])
    execution_history = [
        ToolExecution.from_dict(exec_data) if isinstance(exec_data, dict) else exec_data
        for exec_data in execution_history_data
    ]

    result_summary = compact_observation_text_fn(compact_result_dict, fallback=outcome.summary)
    if len(result_summary) > 200:
        result_summary = result_summary[:197] + "..."

    tool_phase_outcome = _evaluate_tool_phase_outcome(
        tool_result=dict(outcome.result),
        compact_result=compact_result_dict,
        summary=result_summary,
    )
    _record_current_turn_unavailable_tool(
        facts.metadata,
        turn_sequence=turn_sequence,
        tool_id=resolved_tool_id,
        failure_category=tool_phase_outcome.get("failure_category"),
    )

    durable_history_parameters = _mask_durable_mapping(
        dict(outcome.parameters),
        source="tool_execution_history_parameters",
    )
    durable_result_summary = mask_durable_secrets(
        result_summary,
        source="tool_execution_history_result_summary",
    )
    if not isinstance(durable_result_summary, str):
        durable_result_summary = ""

    updated_history = record_tool_execution(
        tool_id=resolved_tool_id,
        parameters=durable_history_parameters,
        result_summary=durable_result_summary,
        iteration=facts.iterations,
        execution_history=execution_history,
    )

    facts.metadata["tool_execution_history"] = [
        exec_record.to_dict() for exec_record in updated_history
    ]

    current_phase = get_scan_phase(facts.metadata)
    facts.metadata["current_scan_phase"] = current_phase
    logger.debug(f"[OPTIMIZATION] Current scan phase: {current_phase}")

    validation_errors = outcome.result.get("validation_errors")
    if validation_errors:
        durable_validation_errors = mask_durable_secrets(
            validation_errors,
            source="tool_validation_errors",
        )
        facts.metadata["validation_errors"] = durable_validation_errors
        safe_inc_fn("langgraph_tool_validation_errors")
    elif "validation_errors" in facts.metadata:
        del facts.metadata["validation_errors"]


def _mask_durable_mapping(value: Mapping[str, Any], *, source: str) -> Dict[str, Any]:
    masked = mask_durable_secrets(dict(value), source=source)
    return masked if isinstance(masked, dict) else {}


def project_trace_history_and_outbound_events(
    *,
    interactive: Any,
    facts: Any,
    outcome: Any,
    compact_result_dict: Mapping[str, Any],
    result_for_metadata: Mapping[str, Any],
    graph_metadata: Mapping[str, Any],
    action_record: Mapping[str, Any],
    approval_response: Optional[Mapping[str, Any]],
    tool_name: str,
    tool_call_id: str,
    tool_batch_id: Optional[str],
    conversation_id: Optional[str],
    turn_id: Optional[str],
    turn_sequence: Optional[int],
    sub_turn_index: Optional[int],
    interrupt_id: Optional[str],
    has_writer: bool,
    writer: Optional[Callable[[Mapping[str, Any]], None]],
    compact_observation_text_fn: Callable[[Mapping[str, Any], Optional[str]], str],
    tool_execution_record_cls: Any,
    store_dispatch_cache_result_fn: Callable[..., None],
    tool_dispatch_cache_key: str,
    diag_info_fn: Callable[..., None],
    logger: Any,
    deterministic_compact_result_dict: Optional[Mapping[str, Any]] = None,
) -> str:
    """Project trace/history side effects, tool_end event, and dispatch cache payload."""
    interactive.trace.reasoning.extend(outcome.reasoning)
    observation_text = compact_observation_text_fn(compact_result_dict, fallback=outcome.summary)
    if observation_text:
        interactive.trace.observations.append(observation_text)

    execution_reasoning = str(
        compact_result_dict.get("summary") or observation_text or "Tool executed."
    ).strip()

    approval_result = dict(approval_response or {})
    if approval_result:
        outcome.result["approval_granted"] = approval_result.get("action") != "skip"
        outcome.result["approval_reason"] = approval_result.get("action")
        outcome.result["approval_metadata"] = dict(approval_result)

    interactive.trace.executed_tools.append(
        tool_execution_record_cls(
            tool_id=str(outcome.tool_id),
            args=dict(outcome.parameters),
            status="success" if outcome.result.get("success") else "error",
            observation=observation_text,
            reasoning=execution_reasoning,
            approval_granted=outcome.result.get("approval_granted"),
            approval_reason=outcome.result.get("approval_reason"),
            approval_metadata=dict(outcome.result.get("approval_metadata") or {}),
        )
    )

    if has_writer and writer is not None:
        execution_status = "success" if outcome.result.get("success") else "error"
        execution_duration = outcome.result.get("duration", 0)
        exit_code = outcome.result.get("exit_code")

        writer(
            {
                "type": "tool_end",
                "tool": outcome.tool_id or tool_name,
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "status": execution_status,
                "duration": execution_duration,
                "exit_code": exit_code,
                "summary": {
                    "summary": compact_result_dict.get("summary", ""),
                    "key_findings": compact_result_dict.get("key_findings", []),
                    "errors": compact_result_dict.get("errors", []),
                    "report_recommendations": compact_result_dict.get("report_recommendations", []),
                },
                "compact_tool_result": compact_result_dict,
                "error": None,
                "ind": 1,
                "step_type": "tool_end",
                "turn_sequence": turn_sequence,
                "sub_turn_index": sub_turn_index,
            }
        )
        logger.info(
            "[TOOL_EXECUTION] Emitted tool_end for %s (task_id=%s interrupt_id=%s "
            "status=%s tool_call_id=%s turn_sequence=%s turn_id=%s sub_turn_index=%s)",
            outcome.tool_id or tool_name,
            facts.task_id,
            interrupt_id or "unknown",
            execution_status,
            tool_call_id,
            turn_sequence,
            turn_id,
            sub_turn_index,
        )
        diag_info_fn(
            "TOOL_EXECUTION | tool_end | task_id=%s interrupt_id=%s tool=%s "
            "status=%s tool_call_id=%s turn_sequence=%s turn_id=%s sub_turn_index=%s",
            facts.task_id,
            interrupt_id or "unknown",
            outcome.tool_id or tool_name,
            execution_status,
            tool_call_id,
            turn_sequence,
            turn_id,
            sub_turn_index,
        )

    store_dispatch_cache_result_fn(
        facts=facts,
        tool_dispatch_cache_key=tool_dispatch_cache_key,
        tool_call_id=tool_call_id,
        compact_result_dict=dict(compact_result_dict),
        deterministic_compact_result_dict=(
            dict(deterministic_compact_result_dict)
            if isinstance(deterministic_compact_result_dict, Mapping)
            else None
        ),
        result_for_metadata=dict(result_for_metadata),
        graph_metadata=dict(graph_metadata),
        action_record=dict(action_record),
        observation_text=observation_text,
        reasoning_additions=list(outcome.reasoning or []),
        outcome_parameters=outcome.parameters,
        outcome_success=bool(outcome.result.get("success")),
        outcome_summary=execution_reasoning,
        approval_granted=outcome.result.get("approval_granted"),
        approval_reason=outcome.result.get("approval_reason"),
        approval_metadata=dict(outcome.result.get("approval_metadata") or {}),
    )
    return observation_text
