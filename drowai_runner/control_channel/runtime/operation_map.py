"""Shared remote_runtime operation mapper.

Maps an inbound remote_runtime ``RunnerEnvelope`` plus its validated request context to
the ``(operation_name, operation_params)`` pair dispatched by the runner
operation service. This is the single source of remote_runtime operation/target mapping
used by the artifact promote, remote_runtime, and terminal stream handlers.

Boundary: stateless and pure. It owns no client-lifetime state, performs no I/O
or websocket sends, and never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from runtime_shared.runner_protocol import (
    RunnerEnvelope,
    RunnerMessageType,
    RunnerProtocolValidationError,
)

from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext


def map_remote_runtime_operation(
    *,
    inbound: RunnerEnvelope,
    context: _RemoteRuntimeRequestContext,
) -> tuple[str, dict[str, object]]:
    payload = inbound.payload
    payload_params = getattr(payload, "params", {})
    params = dict(payload_params) if isinstance(payload_params, Mapping) else {}
    runtime_job_id = context.runtime_job_id
    task_id = context.task_id
    workspace_id = context.workspace_id
    runtime_image = str(getattr(payload, "runtime_image", "") or "").strip()

    if inbound.message_type is RunnerMessageType.TASK_START:
        params.update(
            {
                "runtime_job_id": runtime_job_id,
                "task_id": task_id,
                "tenant_id": inbound.tenant_id,
                "workspace_id": workspace_id,
                "image": runtime_image,
            }
        )
        return ("materialize_runtime", params)
    if inbound.message_type is RunnerMessageType.TASK_PAUSE:
        return ("pause_runtime", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.TASK_RESUME:
        return ("resume_runtime", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.TASK_STOP:
        params.update({"runtime_job_id": runtime_job_id})
        return ("stop_runtime", params)
    if inbound.message_type is RunnerMessageType.TASK_RETIRE:
        return (
            "retire_runtime",
            {
                "runtime_job_id": runtime_job_id,
                "tenant_id": inbound.tenant_id,
                "task_id": task_id,
                "workspace_id": workspace_id,
            },
        )
    if inbound.message_type is RunnerMessageType.RUNTIME_INPUT:
        params.update({"runtime_job_id": runtime_job_id})
        return ("append_runtime_input", params)
    if inbound.message_type is RunnerMessageType.RUNTIME_STARTUP_PROGRESS:
        return ("runtime_startup_progress", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.RUNTIME_STATUS:
        return ("runtime_status", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.RUNTIME_LOGS:
        lines_value = params.get("lines")
        if lines_value is None:
            lines_value = params.get("tail")
        return ("runtime_logs", {"runtime_job_id": runtime_job_id, "lines": lines_value or 200})
    if inbound.message_type is RunnerMessageType.RUNTIME_METRICS:
        return ("runtime_metrics", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.RUNTIME_INVENTORY:
        params.update(
            {
                "scope_tenant_id": inbound.tenant_id,
                "scope_task_id": str(task_id),
                "scope_runtime_job_id": runtime_job_id,
            }
        )
        return ("runtime_inventory", params)
    if inbound.message_type is RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP:
        retain_outputs_raw = params.get("retain_outputs", True)
        return (
            "runtime_workspace_cleanup",
            {
                "workspace_id": workspace_id,
                "cleanup_scope": str(params.get("cleanup_scope") or "workspace").strip().lower(),
                "retain_outputs": (
                    retain_outputs_raw if isinstance(retain_outputs_raw, bool) else True
                ),
            },
        )
    if inbound.message_type is RunnerMessageType.RUNTIME_WORKSPACE_QUERY:
        return (
            "query_runtime_artifacts",
            {
                "workspace_id": workspace_id,
                "prefix": str(params.get("prefix") or ""),
            },
        )
    if inbound.message_type is RunnerMessageType.RUNTIME_WORKSPACE_READ:
        artifact_path = str(params.get("artifact_path") or params.get("path") or "").strip()
        read_params: dict[str, object] = {
            "workspace_id": workspace_id,
            "artifact_path": artifact_path,
            "binary": bool(params.get("binary") or False),
            "encoding": str(params.get("encoding") or "utf-8"),
        }
        if params.get("max_bytes") is not None:
            read_params["max_bytes"] = params.get("max_bytes")
        if params.get("max_chars") is not None:
            read_params["max_chars"] = params.get("max_chars")
        return ("read_runtime_artifact_file", read_params)
    if inbound.message_type is RunnerMessageType.RUNTIME_WORKSPACE_WRITE:
        artifact_path = str(params.get("artifact_path") or params.get("path") or "").strip()
        write_params: dict[str, object] = {
            "workspace_id": workspace_id,
            "artifact_path": artifact_path,
            "encoding": str(params.get("encoding") or "utf-8"),
        }
        if params.get("mode") is not None:
            write_params["mode"] = str(params.get("mode") or "")
        if params.get("content_base64") is not None:
            write_params["content_base64"] = params.get("content_base64")
        if params.get("content") is not None:
            write_params["content"] = params.get("content")
        return ("write_runtime_artifact_file", write_params)
    if inbound.message_type is RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE:
        promote_params: dict[str, object] = {
            "workspace_id": workspace_id,
            "runtime_job_id": runtime_job_id,
        }
        for key, value in params.items():
            promote_params[key] = value
        return ("promote_artifact_refs", promote_params)
    if inbound.message_type is RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA:
        action = str(params.get("action") or "query").strip().lower()
        env_params: dict[str, object] = {
            "workspace_id": workspace_id,
            "runtime_job_id": runtime_job_id,
        }
        if action == "read":
            env_params["key"] = params.get("key")
            return ("read_runtime_environment_metadata", env_params)
        if action == "write":
            env_params["key"] = params.get("key")
            env_params["value"] = params.get("value")
            return ("write_runtime_environment_metadata", env_params)
        env_params["filters"] = params.get("filters", {})
        env_params["key_prefix"] = params.get("key_prefix")
        return ("query_runtime_environment_metadata", env_params)
    if inbound.message_type is RunnerMessageType.RUNTIME_VPN_STATUS:
        return ("check_vpn_status", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.RUNTIME_VPN_RETRY:
        return ("retry_vpn_connection", {"runtime_job_id": runtime_job_id})
    if inbound.message_type is RunnerMessageType.RUNTIME_VPN_CONFIG:
        return (
            "materialize_vpn_config",
            {
                "workspace_id": workspace_id,
                "vpn_config": params.get("vpn_config", {}),
            },
        )
    if inbound.message_type is RunnerMessageType.TERMINAL_OPEN:
        session_name = str(getattr(payload, "session_name", "") or params.get("session_name") or "runtime")
        cols = int(getattr(payload, "cols", 120) or 120)
        rows = int(getattr(payload, "rows", 30) or 30)
        return (
            "terminal_open",
            {
                "runtime_job_id": runtime_job_id,
                "session_name": session_name,
                "cols": cols,
                "rows": rows,
            },
        )
    if inbound.message_type is RunnerMessageType.TERMINAL_INPUT:
        session_id = str(getattr(payload, "session_id", "") or params.get("session_id") or "").strip()
        return (
            "terminal_input",
            {
                "session_id": session_id,
                "data": str(getattr(payload, "data", "") or params.get("data") or ""),
            },
        )
    if inbound.message_type is RunnerMessageType.TERMINAL_RESIZE:
        session_id = str(getattr(payload, "session_id", "") or params.get("session_id") or "").strip()
        cols = int(getattr(payload, "cols", 120) or 120)
        rows = int(getattr(payload, "rows", 30) or 30)
        return (
            "terminal_resize",
            {
                "session_id": session_id,
                "cols": cols,
                "rows": rows,
            },
        )
    if inbound.message_type is RunnerMessageType.TERMINAL_CLOSE:
        session_id = str(getattr(payload, "session_id", "") or params.get("session_id") or "").strip()
        return (
            "terminal_close",
            {
                "session_id": session_id,
            },
        )
    raise RunnerProtocolValidationError(f"Unsupported remote_runtime runtime operation: {inbound.type}")
