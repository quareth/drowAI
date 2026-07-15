"""Shared runner-local operation dispatcher for managed control-plane mode.

This module centralizes runtime lifecycle, observability, VPN, environment metadata,
workspace cleanup, terminal, and command-dispatch operations behind the managed
runner control channel.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.artifact_manifest import scan_runner_artifacts_for_manifest
from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.config import RunnerConfig
from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.job_store import RunnerJobStore
from drowai_runner.lifecycle_operations import RunnerLifecycleOperations
from drowai_runner.logs_metrics import RunnerLogsMetricsAdapter, unsupported_operation_response
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.tool_command_operations import RunnerToolCommandOperations
from drowai_runner.workspace import RunnerWorkspaceManager


class RunnerOperationService:
    """Dispatch runner-local operations with deterministic fail-closed responses."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        workspace: RunnerWorkspaceManager,
        job_store: RunnerJobStore,
        docker_runtime: RunnerDockerRuntime,
        logs_metrics: RunnerLogsMetricsAdapter,
        terminal_proxy: RunnerTerminalProxy,
        cleanup: RunnerCleanupService,
    ) -> None:
        self._workspace = workspace
        self._logs_metrics = logs_metrics
        self._terminal_proxy = terminal_proxy
        self._lifecycle = RunnerLifecycleOperations(
            config=config,
            workspace=workspace,
            job_store=job_store,
            docker_runtime=docker_runtime,
            cleanup=cleanup,
        )
        self._tool_commands = RunnerToolCommandOperations(
            config=config,
            workspace=workspace,
            job_store=job_store,
            terminal_proxy=terminal_proxy,
        )

    def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
        """Execute one operation and return transport-safe response payload."""
        if operation == "materialize_runtime":
            return self._lifecycle.materialize_runtime(params)
        if operation == "pause_runtime":
            return self._lifecycle.pause_or_resume_runtime(params, pause=True)
        if operation == "resume_runtime":
            return self._lifecycle.pause_or_resume_runtime(params, pause=False)
        if operation == "stop_runtime":
            return self._lifecycle.stop_runtime(params)
        if operation == "retire_runtime":
            return self._lifecycle.retire_runtime(params)
        if operation in {"dispatch_command", "dispatch_tool_command"}:
            return self._tool_commands.dispatch_tool_command(params)
        if operation == "submit_tool_command":
            return self._tool_commands.submit_tool_command(params)
        if operation == "get_tool_command_result":
            return self._tool_commands.get_tool_command_result(params)
        if operation == "append_runtime_input":
            return self._lifecycle.append_runtime_input(params)
        if operation == "runtime_status":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            return self._response_to_payload(self._logs_metrics.get_runtime_status(runtime_job_id))
        if operation == "runtime_startup_progress":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            return self._response_to_payload(
                self._logs_metrics.get_runtime_startup_progress(runtime_job_id)
            )
        if operation == "runtime_logs":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            lines = int(params.get("lines") or 200)
            return self._response_to_payload(self._logs_metrics.get_runtime_logs(runtime_job_id, lines=lines))
        if operation == "runtime_metrics":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            return self._response_to_payload(self._logs_metrics.get_runtime_metrics(runtime_job_id))
        if operation == "runtime_inventory":
            return self._response_to_payload(
                self._logs_metrics.list_runtime_inventory(
                    tenant_id=str(params.get("scope_tenant_id") or "").strip() or None,
                    task_id=str(params.get("scope_task_id") or "").strip() or None,
                    runtime_job_id=str(params.get("scope_runtime_job_id") or "").strip() or None,
                )
            )
        if operation == "runtime_workspace_cleanup":
            workspace_id = str(params.get("workspace_id") or "").strip()
            cleanup_scope = str(params.get("cleanup_scope") or "workspace").strip().lower()
            retain_outputs_raw = params.get("retain_outputs", True)
            retain_outputs = retain_outputs_raw if isinstance(retain_outputs_raw, bool) else True
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.cleanup_runtime_workspace(
                    workspace_id,
                    cleanup_scope=cleanup_scope,
                    retain_outputs=retain_outputs,
                )
            )
        if operation == "materialize_vpn_config":
            workspace_id = str(params.get("workspace_id") or "").strip()
            config_payload = str(params.get("config_payload") or "")
            file_name = str(params.get("file_name") or "").strip() or None
            vpn_config = params.get("vpn_config")
            if isinstance(vpn_config, Mapping):
                config_payload = str(vpn_config.get("config_data") or config_payload)
                file_name = str(vpn_config.get("file_name") or file_name or "").strip() or None
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.materialize_vpn_config(
                    workspace_id,
                    config_payload=config_payload,
                    file_name=file_name,
                )
            )
        if operation == "retry_vpn_connection":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            return self._response_to_payload(self._logs_metrics.retry_vpn_connection(runtime_job_id))
        if operation == "check_vpn_status":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            if not runtime_job_id:
                return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
            return self._response_to_payload(self._logs_metrics.check_vpn_status(runtime_job_id))
        if operation == "write_runtime_environment_metadata":
            workspace_id = str(params.get("workspace_id") or "").strip()
            key = str(params.get("key") or "").strip()
            value = str(params.get("value") or "")
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.write_runtime_environment_metadata(
                    workspace_id,
                    key=key,
                    value=value,
                )
            )
        if operation == "read_runtime_environment_metadata":
            workspace_id = str(params.get("workspace_id") or "").strip()
            key = str(params.get("key") or "").strip()
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.read_runtime_environment_metadata(workspace_id, key=key)
            )
        if operation == "query_runtime_environment_metadata":
            workspace_id = str(params.get("workspace_id") or "").strip()
            runtime_job_id = str(params.get("runtime_job_id") or "").strip() or None
            key_prefix = str(params.get("key_prefix") or "").strip() or None
            filters = params.get("filters")
            if key_prefix is None and isinstance(filters, Mapping):
                key_prefix = str(filters.get("key_prefix") or "").strip() or None
            if runtime_job_id is None and isinstance(filters, Mapping):
                runtime_job_id = str(filters.get("runtime_job_id") or "").strip() or None
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.query_runtime_environment_metadata(
                    workspace_id,
                    key_prefix=key_prefix,
                    runtime_job_id=runtime_job_id,
                )
            )
        if operation == "read_runtime_artifact_file":
            workspace_id = str(params.get("workspace_id") or "").strip()
            artifact_path = str(params.get("artifact_path") or "").strip()
            binary = bool(params.get("binary") or False)
            max_bytes = int(params.get("max_bytes")) if params.get("max_bytes") is not None else None
            max_chars = int(params.get("max_chars")) if params.get("max_chars") is not None else None
            encoding = str(params.get("encoding") or "utf-8")
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.read_runtime_artifact_file(
                    workspace_id,
                    artifact_path=artifact_path,
                    binary=binary,
                    max_bytes=max_bytes,
                    max_chars=max_chars,
                    encoding=encoding,
                )
            )
        if operation == "write_runtime_artifact_file":
            workspace_id = str(params.get("workspace_id") or "").strip()
            artifact_path = str(params.get("artifact_path") or "").strip()
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.write_runtime_artifact_file(
                    workspace_id,
                    artifact_path=artifact_path,
                    content_base64=(
                        str(params.get("content_base64"))
                        if params.get("content_base64") is not None
                        else None
                    ),
                    content=(
                        str(params.get("content"))
                        if params.get("content") is not None
                        else None
                    ),
                    encoding=str(params.get("encoding") or "utf-8"),
                    mode=str(params.get("mode") or "write"),
                )
            )
        if operation == "query_runtime_artifacts":
            workspace_id = str(params.get("workspace_id") or "").strip()
            prefix = str(params.get("prefix") or "")
            if not workspace_id:
                return {"status": "failed", "error_code": "MISSING_WORKSPACE_ID"}
            return self._response_to_payload(
                self._logs_metrics.query_runtime_artifacts(workspace_id, prefix=prefix)
            )
        if operation == "promote_artifact_refs":
            return self._promote_artifact_refs(params)
        if operation == "terminal_open":
            runtime_job_id = str(params.get("runtime_job_id") or "").strip()
            session_name = str(params.get("session_name") or "terminal")
            cols = int(params.get("cols") or 120)
            rows = int(params.get("rows") or 30)
            return self._response_to_payload(
                self._terminal_proxy.open_terminal_session(
                    runtime_job_id=runtime_job_id,
                    session_name=session_name,
                    cols=cols,
                    rows=rows,
                )
            )
        if operation == "terminal_input":
            session_id = str(params.get("session_id") or "").strip()
            data = str(params.get("data") or "")
            return self._response_to_payload(
                self._terminal_proxy.send_terminal_input(session_id=session_id, data=data)
            )
        if operation == "terminal_read":
            session_id = str(params.get("session_id") or "").strip()
            max_bytes = int(params.get("max_bytes") or 32768)
            return self._response_to_payload(
                self._terminal_proxy.read_terminal_output(
                    session_id=session_id,
                    max_bytes=max_bytes,
                )
            )
        if operation == "terminal_resize":
            session_id = str(params.get("session_id") or "").strip()
            cols = int(params.get("cols") or 120)
            rows = int(params.get("rows") or 30)
            return self._response_to_payload(
                self._terminal_proxy.resize_terminal_session(
                    session_id=session_id,
                    cols=cols,
                    rows=rows,
                )
            )
        if operation == "terminal_close":
            session_id = str(params.get("session_id") or "").strip()
            return self._response_to_payload(
                self._terminal_proxy.close_terminal_session(session_id=session_id)
            )
        unsupported = unsupported_operation_response(
            operation=operation,
            owning_wave="5",
            route_behavior="fail_closed",
        )
        return self._response_to_payload(unsupported)

    def _promote_artifact_refs(self, params: dict[str, object]) -> dict[str, object]:
        """Scan explicit artifact refs for manifest promotion without tool knowledge."""
        workspace_id = str(params.get("workspace_id") or "").strip()
        artifacts_raw = params.get("artifacts")
        artifacts_candidates: list[str] = []
        if isinstance(artifacts_raw, (list, tuple)):
            artifacts_candidates = [str(item).strip() for item in artifacts_raw if str(item).strip()]
        if not workspace_id:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "MISSING_WORKSPACE_ID",
                "error_message": "workspace_id is required for promote_artifact_refs.",
            }
        try:
            workspace_path = self._workspace.resolve_task_workspace(workspace_id)
        except ValueError as exc:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "INVALID_WORKSPACE",
                "error_message": str(exc),
                "metadata": {
                    "artifact_manifest": {
                        "status": "skipped_invalid_workspace",
                        "declared_count": len(artifacts_candidates),
                        "accepted_count": 0,
                    }
                },
            }
        scan_result = scan_runner_artifacts_for_manifest(
            workspace_path=workspace_path,
            artifacts=artifacts_candidates,
        )
        normalized_artifacts = tuple(item.relative_path for item in scan_result.manifest_items)
        manifest_status = (
            "ready_for_upload_request" if scan_result.manifest_items else "no_uploadable_artifacts"
        )
        return {
            "accepted": True,
            "status": "succeeded",
            "metadata": {
                "artifacts": list(normalized_artifacts),
                "artifact_manifest": {
                    "status": manifest_status,
                    "declared_count": len(artifacts_candidates),
                    "accepted_count": len(scan_result.manifest_items),
                    "skipped_count": scan_result.skipped_count,
                    "warnings": scan_result.warnings_json(),
                    "warnings_truncated_count": scan_result.warnings_truncated_count,
                },
                "manifest_items": [
                    {
                        "artifact_client_id": item.artifact_client_id,
                        "relative_path": item.relative_path,
                        "artifact_kind": item.artifact_kind,
                        "content_type": item.content_type,
                        "size_bytes": item.size_bytes,
                        "content_sha256": item.content_sha256,
                        "is_text": item.is_text,
                    }
                    for item in scan_result.manifest_items
                ],
                "files_by_client_id": {
                    client_id: {
                        "artifact_client_id": scanned.artifact_client_id,
                        "relative_path": scanned.relative_path,
                        "size_bytes": scanned.size_bytes,
                        "content_sha256": scanned.content_sha256,
                        "content_type": scanned.content_type,
                        "is_text": scanned.is_text,
                    }
                    for client_id, scanned in scan_result.files_by_client_id.items()
                },
            },
        }

    @staticmethod
    def _response_to_payload(response: object) -> dict[str, object]:
        base = {
            "accepted": bool(getattr(response, "accepted", False)),
            "status": str(getattr(response, "status", "failed")),
        }
        error_code = getattr(response, "error_code", None)
        error_message = getattr(response, "error_message", None)
        metadata = getattr(response, "metadata", None)
        if error_code is not None:
            base["error_code"] = str(error_code)
        if error_message is not None:
            base["error_message"] = str(error_message)
        base["metadata"] = dict(metadata or {})
        return base


__all__ = ["RunnerOperationService"]
