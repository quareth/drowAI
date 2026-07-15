"""Normalize runner-provider tool results to graph tool-result shape.

This module adapts cloud runner provider `delegate_result` payloads into the
same field contract used by local tool execution so batch aggregation, prompt
builders, and stream projection remain stable across execution authorities.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from agent.utils.output_processing import classify_output_type, smart_truncate
from agent.utils.truncation_config import STDERR_SNIPPET, get_threshold_for_type

_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "ok"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
_TIMEOUT_STATUSES = frozenset({"timed_out", "timeout"})
_VALIDATION_STATUSES = frozenset({"validation_error"})


def adapt_remote_tool_result(
    *,
    tool_id: str,
    provider_ok: bool,
    provider_error_code: str | None,
    provider_error_message: str | None,
    provider_metadata: Mapping[str, Any] | None,
    delegate_result: Mapping[str, Any] | None,
    duration_seconds: float,
    route_policy: Mapping[str, Any],
    timeout_policy: Mapping[str, Any],
    missing_result: bool,
) -> Dict[str, Any]:
    """Return GraphToolExecutor-compatible tool-result payload for remote calls."""
    delegate = dict(delegate_result or {})
    raw_status = str(delegate.get("status") or "").strip().lower()
    status_hint = "tool_result_missing" if missing_result else ""

    success = _resolve_success(delegate=delegate, provider_ok=provider_ok, raw_status=raw_status)
    if missing_result:
        success = False
    stdout = str(delegate.get("stdout") or "")
    stderr = str(delegate.get("stderr") or "")
    if not success and not stderr:
        stderr = str(provider_error_message or "")
    exit_code = _resolve_exit_code(delegate=delegate, success=success)

    metadata = _build_metadata(
        delegate=delegate,
        provider_metadata=provider_metadata,
        route_policy=route_policy,
        timeout_policy=timeout_policy,
        provider_error_code=provider_error_code,
        provider_error_message=provider_error_message,
        success=success,
    )
    status = _normalize_graph_status(
        raw_status=raw_status,
        success=success,
        error_code=str(metadata.get("error_code") or ""),
        status_hint=status_hint,
    )

    if missing_result and not stderr:
        stderr = "Runner tool command did not return a terminal result."
    if not success and not stderr:
        stderr = str(provider_error_message or "Runner tool execution failed.")
    if not success and exit_code == 0:
        exit_code = 2

    output_type = classify_output_type(tool_name=tool_id, command="", output=stdout)
    stdout_excerpt, stdout_truncated = smart_truncate(
        stdout,
        total_limit=get_threshold_for_type(output_type),
        output_type=output_type,
        return_was_truncated=True,
    )
    stderr_excerpt, stderr_truncated = smart_truncate(
        stderr,
        total_limit=STDERR_SNIPPET,
        return_was_truncated=True,
    )
    observation = stdout_excerpt or stderr_excerpt or "Tool completed without output."
    chars_truncated = max(0, len(stdout) - len(stdout_excerpt)) + max(
        0,
        len(stderr) - len(stderr_excerpt),
    )

    artifacts = _coerce_artifacts(delegate.get("artifacts"))
    has_promoted_artifacts = _metadata_has_promoted_artifacts(metadata)
    if artifacts or has_promoted_artifacts:
        if has_promoted_artifacts:
            metadata.setdefault("artifact_scope", "cloud_data_plane")
            metadata.setdefault(
                "artifact_promotion_status",
                _resolve_artifact_promotion_status_from_metadata(metadata),
            )
            metadata.setdefault("artifact_visibility", "artifact_catalog")
        else:
            metadata.setdefault("artifact_scope", "runner_local")
            metadata.setdefault("artifact_promotion_status", "unpromoted")
            metadata.setdefault("artifact_visibility", "runner_workspace_only")

    command_text = _resolve_command_text(delegate, metadata)
    return {
        "tool": tool_id,
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "exit_code": exit_code,
        "observation": observation,
        "approval_granted": True,
        "approval_reason": None,
        "approval_metadata": {},
        "duration": duration_seconds,
        "metadata": metadata,
        "status": status,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "was_truncated": stdout_truncated or stderr_truncated,
        "chars_truncated": chars_truncated,
        "output_type": "structured",
        "suggest_file_reading": False,
        "artifacts": artifacts,
        "command_text": command_text,
    }


def _resolve_success(*, delegate: Mapping[str, Any], provider_ok: bool, raw_status: str) -> bool:
    explicit = delegate.get("success")
    if isinstance(explicit, bool):
        return explicit
    if raw_status in _SUCCESS_STATUSES:
        return True
    if raw_status in _CANCELLED_STATUSES or raw_status in _TIMEOUT_STATUSES:
        return False
    return bool(provider_ok)


def _resolve_exit_code(*, delegate: Mapping[str, Any], success: bool) -> int:
    try:
        return int(delegate.get("exit_code", 0 if success else 2))
    except (TypeError, ValueError):
        return 0 if success else 2


def _normalize_graph_status(
    *,
    raw_status: str,
    success: bool,
    error_code: str,
    status_hint: str,
) -> str:
    if status_hint:
        return status_hint
    if raw_status in _SUCCESS_STATUSES:
        return "success"
    if raw_status in _CANCELLED_STATUSES:
        return "cancelled"
    if raw_status in _TIMEOUT_STATUSES:
        return "timeout"
    if raw_status in _VALIDATION_STATUSES:
        return "validation_error"
    if raw_status in {"failed", "error", "rejected"}:
        return "error"
    if success:
        return "success"

    normalized_error_code = str(error_code or "").strip().upper()
    if "CANCEL" in normalized_error_code:
        return "cancelled"
    if "TIMEOUT" in normalized_error_code:
        return "timeout"
    return "error"


def _build_metadata(
    *,
    delegate: Mapping[str, Any],
    provider_metadata: Mapping[str, Any] | None,
    route_policy: Mapping[str, Any],
    timeout_policy: Mapping[str, Any],
    provider_error_code: str | None,
    provider_error_message: str | None,
    success: bool,
) -> Dict[str, Any]:
    delegate_metadata = delegate.get("metadata")
    metadata = dict(delegate_metadata) if isinstance(delegate_metadata, Mapping) else {}
    if isinstance(provider_metadata, Mapping):
        for key in (
            "runtime_job_id",
            "runner_runtime_job_id",
            "task_runtime_job_id",
            "tool_command_runtime_job_id",
            "command_id",
            "workspace_id",
            "runtime_job_status",
            "runner_id",
            "execution_site_id",
        ):
            if key in provider_metadata and key not in metadata:
                metadata[key] = provider_metadata[key]
        if not success and "error_code" in provider_metadata and "error_code" not in metadata:
            metadata["error_code"] = provider_metadata["error_code"]
    metadata.setdefault("route_policy", dict(route_policy))
    metadata.setdefault("timeout_policy", dict(timeout_policy))

    delegate_error_code = delegate.get("error_code")
    if not success and isinstance(delegate_error_code, str) and delegate_error_code:
        metadata.setdefault("error_code", delegate_error_code)
    if not success and provider_error_code:
        metadata.setdefault("error_code", provider_error_code)
    if success:
        diagnostics: Dict[str, Any] = {}
        metadata_error_code = metadata.pop("error_code", None)
        if metadata_error_code:
            diagnostics["metadata_error_code"] = metadata_error_code
        if isinstance(provider_metadata, Mapping):
            provider_metadata_error = provider_metadata.get("error_code")
            if provider_metadata_error:
                diagnostics["provider_error_code"] = provider_metadata_error
            runtime_job_status = provider_metadata.get("runtime_job_status")
            if runtime_job_status:
                diagnostics["runtime_job_status"] = runtime_job_status
        if delegate_error_code:
            diagnostics["delegate_error_code"] = delegate_error_code
        delegate_error_message = delegate.get("error_message")
        if delegate_error_message:
            diagnostics["delegate_error_message"] = delegate_error_message
        if provider_error_code:
            diagnostics["provider_error_code"] = provider_error_code
        if provider_error_message:
            diagnostics["provider_error_message"] = provider_error_message
        if diagnostics:
            existing_diagnostics = metadata.get("runner_provider_diagnostics")
            merged_diagnostics = (
                dict(existing_diagnostics) if isinstance(existing_diagnostics, Mapping) else {}
            )
            merged_diagnostics.update(diagnostics)
            metadata["runner_provider_diagnostics"] = merged_diagnostics
    return metadata


def _coerce_artifacts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    artifacts: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            artifacts.append(text)
    return artifacts


def _metadata_has_promoted_artifacts(metadata: Mapping[str, Any]) -> bool:
    promoted_ids = metadata.get("promoted_artifact_ids")
    if isinstance(promoted_ids, list):
        for item in promoted_ids:
            if str(item or "").strip():
                return True
    artifact_refs = metadata.get("artifact_refs")
    if isinstance(artifact_refs, list):
        for item in artifact_refs:
            if not isinstance(item, Mapping):
                continue
            artifact_id = item.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id.strip():
                return True
    return False


def _resolve_artifact_promotion_status_from_metadata(metadata: Mapping[str, Any]) -> str:
    status = str(metadata.get("artifact_promotion_status") or "").strip().lower()
    if status in {"ready", "upload_pending", "upload_failed"}:
        return status
    artifact_promotion = metadata.get("artifact_promotion")
    if isinstance(artifact_promotion, Mapping):
        promotion_status = str(artifact_promotion.get("status") or "").strip().lower()
        if promotion_status in {"ready", "upload_pending", "upload_failed"}:
            return promotion_status
        if promotion_status in {"failed", "error"}:
            return "upload_failed"
    return "ready"


def _resolve_command_text(delegate: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    for candidate in (
        delegate.get("command_text"),
        (delegate.get("result") or {}).get("command_text")
        if isinstance(delegate.get("result"), Mapping)
        else None,
        metadata.get("command_text"),
    ):
        if isinstance(candidate, str):
            text = candidate.strip()
            if text:
                return text
    return None
