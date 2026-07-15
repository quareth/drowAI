"""Runner-local logs, metrics, VPN, and artifact adapter surfaces.

This module exposes backend-free runner adapters for status, logs, metrics,
runtime inventory, workspace cleanup, environment metadata, VPN operations,
and artifact read/query flows expected by the managed control-plane contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.environment import (
    collect_and_save_runner_environment_info,
    load_runner_environment_info,
)
from drowai_runner.job_store import ACTIVE_JOB_STATUSES, RunnerJobStore
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.docker_contracts import (
    CONTAINER_VPN_CONFIG_PATH,
    IMAGE_INTERNAL_VPN_SCRIPT_PATH,
    RUNNER_VPN_CONFIG_FILE_NAME,
)
from runtime_shared.file_comm_contracts import STANDARD_RUNTIME_FILES, STANDARD_RUNTIME_SUBDIRECTORIES
from runtime_shared.workspace_write_mode import (
    WORKSPACE_WRITE_MODE_APPEND,
    normalize_workspace_write_mode,
    workspace_path_allows_append,
)
from runtime_shared.vpn_observability import normalize_vpn_log_lines
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspaceFilesystem,
    WorkspacePathError,
)

logger = logging.getLogger(__name__)

ENV_METADATA_FILE = ".runtime-env.json"
ERROR_UNSUPPORTED_OPERATION = "RUNNER_OPERATION_UNSUPPORTED"
ERROR_JOB_NOT_ACTIVE = "RUNNER_JOB_NOT_ACTIVE"
ERROR_RUNTIME_JOB_NOT_FOUND = "RUNNER_JOB_NOT_FOUND"
ERROR_CONTAINER_NOT_ASSIGNED = "RUNNER_CONTAINER_NOT_ASSIGNED"
ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE = "RUNNER_WORKSPACE_PATH_OUTSIDE_SCOPE"
ERROR_ARTIFACT_PATH_OUTSIDE_SCOPE = "RUNNER_ARTIFACT_PATH_OUTSIDE_SCOPE"
ERROR_ARTIFACT_NOT_FOUND = "RUNNER_ARTIFACT_NOT_FOUND"
ERROR_INVALID_ENV_METADATA_KEY = "RUNNER_INVALID_ENV_METADATA_KEY"
ERROR_UNSUPPORTED_ENV_METADATA_KEY = "RUNNER_UNSUPPORTED_ENV_METADATA_KEY"
ERROR_UNSUPPORTED_ENV_METADATA_FILTER = "RUNNER_UNSUPPORTED_ENV_METADATA_FILTER"
ERROR_UNSUPPORTED_CLEANUP_SCOPE = "RUNNER_UNSUPPORTED_CLEANUP_SCOPE"
ERROR_WORKSPACE_WRITE_MODE_UNSUPPORTED = "RUNNER_WORKSPACE_WRITE_MODE_UNSUPPORTED"
ERROR_VPN_CONFIG_EMPTY = "RUNNER_VPN_CONFIG_EMPTY"
ERROR_WORKSPACE_ENTRY_UNSAFE = "RUNNER_WORKSPACE_ENTRY_UNSAFE"
DEFAULT_VPN_CONFIG_FILE_NAME = RUNNER_VPN_CONFIG_FILE_NAME
_ARTIFACTS_DIR = "artifacts"
_VPN_DIR = "vpn"
_CLEANUP_SCOPES = frozenset({"workspace", "runtime", "all"})
ALLOWED_ENV_METADATA_KEYS = frozenset(
    {
        "agent.version",
    }
)


@dataclass(frozen=True, slots=True)
class RunnerOperationResponse:
    """Backend-free operation response envelope for runner adapter calls."""

    accepted: bool
    status: str
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] | None = None


def unsupported_operation_response(
    *,
    operation: str,
    owning_wave: str,
    route_behavior: str,
) -> RunnerOperationResponse:
    """Return a stable fail-closed unsupported-operation response."""
    return RunnerOperationResponse(
        accepted=False,
        status="rejected",
        error_code=ERROR_UNSUPPORTED_OPERATION,
        error_message=f"Operation `{operation}` is not available in execution_plane runner mode.",
        metadata={
            "operation": operation,
            "owning_wave": owning_wave,
            "route_behavior": route_behavior,
        },
    )


class RunnerLogsMetricsAdapter:
    """Provide runner-local logs/metrics and compatibility runtime operations."""

    def __init__(
        self,
        *,
        job_store: RunnerJobStore,
        docker_runtime: RunnerDockerRuntime,
        workspace_manager: RunnerWorkspaceManager,
    ) -> None:
        self._job_store = job_store
        self._docker_runtime = docker_runtime
        self._workspace_manager = workspace_manager

    def get_runtime_status(self, runtime_job_id: str) -> RunnerOperationResponse:
        """Return normalized runtime/job status for one runner job."""
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_RUNTIME_JOB_NOT_FOUND,
                error_message=f"Unknown runtime job: {runtime_job_id}",
            )
        container_status = None
        if job.container_id:
            container_status = self._docker_runtime.container_status(job.container_id)
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "runtime_job_id": job.runtime_job_id,
                "task_id": job.task_id,
                "workspace_id": job.workspace_id,
                "job_status": job.status,
                "container_status": container_status,
            },
        )

    def get_runtime_startup_progress(self, runtime_job_id: str) -> RunnerOperationResponse:
        """Return a coarse startup phase projection from job/container state."""
        status = self.get_runtime_status(runtime_job_id)
        if not status.accepted:
            return status
        metadata = dict(status.metadata or {})
        job_status = str(metadata.get("job_status") or "unknown")
        phase = "initializing"
        if job_status == "starting":
            phase = "container_starting"
        elif job_status == "running":
            phase = "ready"
        elif job_status == "paused":
            phase = "paused"
        elif job_status in {"stopping", "stopped", "failed", "cleaned_up"}:
            phase = "terminal"
        metadata["startup_phase"] = phase
        return RunnerOperationResponse(accepted=True, status="succeeded", metadata=metadata)

    def get_runtime_logs(self, runtime_job_id: str, *, lines: int = 200) -> RunnerOperationResponse:
        """Read bounded container logs for one active runtime job."""
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_RUNTIME_JOB_NOT_FOUND,
                error_message=f"Unknown runtime job: {runtime_job_id}",
            )
        if not job.container_id:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_CONTAINER_NOT_ASSIGNED,
                error_message=f"Runtime job `{runtime_job_id}` has no assigned container.",
            )
        bounded_lines = max(1, lines)
        log_output = self._docker_runtime.container_logs(job.container_id, tail=bounded_lines)
        normalized_logs: list[dict[str, str]] = []
        for raw_line in log_output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            timestamp, separator, message = line.partition(" ")
            normalized_logs.append(
                {
                    "timestamp": timestamp if separator and timestamp[:4].isdigit() else "",
                    "service": "kali-container",
                    "level": "info",
                    "message": message if separator and timestamp[:4].isdigit() else line,
                }
            )
        try:
            vpn_probe = self._docker_runtime.exec_probe(
                job.container_id,
                [
                    "/bin/bash",
                    "-lc",
                    f"tail -n {bounded_lines} /vpn/connection.log 2>/dev/null || true",
                ],
                timeout_seconds=5,
            )
            if vpn_probe.exit_code == 0:
                normalized_logs.extend(
                    normalize_vpn_log_lines(vpn_probe.stdout.splitlines())
                )
        except Exception as exc:
            logger.debug(
                "runner.runtime_logs.vpn_probe_unavailable "
                "runtime_job_id=%s container_id=%s error_type=%s",
                runtime_job_id,
                job.container_id,
                type(exc).__name__,
            )
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "runtime_job_id": runtime_job_id,
                "task_id": job.task_id,
                "logs": normalized_logs,
                "lines": bounded_lines,
            },
        )

    def get_runtime_metrics(self, runtime_job_id: str) -> RunnerOperationResponse:
        """Read one metrics snapshot for an assigned runtime container."""
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_RUNTIME_JOB_NOT_FOUND,
                error_message=f"Unknown runtime job: {runtime_job_id}",
            )
        if not job.container_id:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_CONTAINER_NOT_ASSIGNED,
                error_message=f"Runtime job `{runtime_job_id}` has no assigned container.",
            )
        metrics = self._docker_runtime.container_metrics(job.container_id)
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "runtime_job_id": runtime_job_id,
                "task_id": job.task_id,
                "metrics": dict(metrics),
            },
        )

    def list_runtime_inventory(
        self,
        *,
        tenant_id: str | None = None,
        task_id: str | None = None,
        runtime_job_id: str | None = None,
    ) -> RunnerOperationResponse:
        """List scoped runtime inventory without host absolute path exposure."""
        jobs = self._job_store.list_jobs()
        if tenant_id:
            jobs = [job for job in jobs if job.tenant_id == tenant_id]
        if task_id:
            jobs = [job for job in jobs if job.task_id == task_id]
        if runtime_job_id:
            jobs = [job for job in jobs if job.runtime_job_id == runtime_job_id]
        inventory = []
        for job in jobs:
            item = {
                "runtime_job_id": job.runtime_job_id,
                "task_id": job.task_id,
                "workspace_id": job.workspace_id,
                "status": job.status,
            }
            if isinstance(job.container_id, str) and job.container_id.strip():
                item["container_id"] = job.container_id
            inventory.append(item)
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={"items": inventory},
        )

    def cleanup_runtime_workspace(
        self,
        workspace_id: str,
        *,
        cleanup_scope: str = "workspace",
        retain_outputs: bool = True,
    ) -> RunnerOperationResponse:
        """Apply scoped runner-local workspace cleanup with output-retention support."""
        normalized_scope = cleanup_scope.strip().lower()
        if normalized_scope not in _CLEANUP_SCOPES:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_UNSUPPORTED_CLEANUP_SCOPE,
                error_message=(
                    "Unsupported cleanup scope. "
                    f"scope={cleanup_scope!r}; allowlisted={sorted(_CLEANUP_SCOPES)}"
                ),
        )
        try:
            self._workspace_manager.resolve_task_workspace(workspace_id)
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )

        removed_paths: list[str] = []
        retained_paths: list[str] = []
        workspace_removed = False
        try:
            filesystem = self._workspace_manager.filesystem(workspace_id)
            try:
                filesystem.list_entries(None, recursive=False)
            except FileNotFoundError:
                cleaned = False
            else:
                cleaned = True
            if not cleaned:
                pass
            elif normalized_scope == "runtime":
                removed_paths, retained_paths = self._cleanup_runtime_scope(
                    filesystem=filesystem,
                    retain_outputs=retain_outputs,
                )
                cleaned = bool(removed_paths)
            elif normalized_scope == "workspace":
                if retain_outputs:
                    removed_paths, retained_paths = (
                        self._cleanup_workspace_scope_retain_outputs(filesystem)
                    )
                    cleaned = bool(removed_paths)
                else:
                    self._workspace_manager.cleanup_task_workspace(workspace_id)
                    workspace_removed = True
                    removed_paths = ["."]
                    cleaned = True
            else:
                self._workspace_manager.cleanup_task_workspace(workspace_id)
                workspace_removed = True
                removed_paths = ["."]
                cleaned = True
        except WorkspaceEntryUnsafeError:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_ENTRY_UNSAFE,
                error_message="Workspace entry is unsafe.",
            )
        except OSError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code="RUNNER_WORKSPACE_CLEANUP_FAILED",
                error_message=f"Workspace cleanup failed: {exc}",
            )
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "workspace_id": workspace_id,
                "cleanup_scope": normalized_scope,
                "retain_outputs": retain_outputs,
                "cleaned": cleaned,
                "workspace_removed": workspace_removed,
                "removed_count": len(removed_paths),
                "removed_paths": removed_paths,
                "retained_paths": retained_paths,
            },
        )

    def _cleanup_runtime_scope(
        self,
        *,
        filesystem: WorkspaceFilesystem,
        retain_outputs: bool,
    ) -> tuple[list[str], list[str]]:
        removed_paths: list[str] = []
        retained_paths: list[str] = []
        runtime_directories = set(STANDARD_RUNTIME_SUBDIRECTORIES) | {_VPN_DIR}
        if retain_outputs:
            runtime_directories.discard(_ARTIFACTS_DIR)

        for file_name in STANDARD_RUNTIME_FILES:
            try:
                filesystem.remove(file_name, missing_ok=False)
            except FileNotFoundError:
                pass
            else:
                removed_paths.append(file_name)

        for directory_name in sorted(runtime_directories):
            try:
                filesystem.remove(
                    directory_name, recursive=True, missing_ok=False
                )
            except FileNotFoundError:
                pass
            else:
                removed_paths.append(directory_name)

        if retain_outputs:
            try:
                filesystem.list_entries(_ARTIFACTS_DIR, recursive=False)
            except FileNotFoundError:
                pass
            else:
                retained_paths.append(_ARTIFACTS_DIR)
        return removed_paths, retained_paths

    def _cleanup_workspace_scope_retain_outputs(
        self, filesystem: WorkspaceFilesystem
    ) -> tuple[list[str], list[str]]:
        removed_paths: list[str] = []
        retained_paths: list[str] = []
        entries = sorted(
            filesystem.list_entries(None, recursive=False),
            key=lambda item: item.relative_path,
        )
        for entry in entries:
            if entry.relative_path == _ARTIFACTS_DIR:
                retained_paths.append(_ARTIFACTS_DIR)
                continue
            filesystem.remove(
                entry.relative_path,
                recursive=entry.kind == "directory",
            )
            removed_paths.append(entry.relative_path)
        return removed_paths, retained_paths

    def materialize_vpn_config(
        self,
        workspace_id: str,
        *,
        config_payload: str,
        file_name: str | None = None,
    ) -> RunnerOperationResponse:
        """Write task-local VPN config using restrictive file permissions."""
        payload = config_payload.strip()
        if not payload:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_VPN_CONFIG_EMPTY,
                error_message="VPN config payload must not be empty.",
            )
        target_file = file_name or DEFAULT_VPN_CONFIG_FILE_NAME
        try:
            self._workspace_manager.write_vpn_file(
                workspace_id, DEFAULT_VPN_CONFIG_FILE_NAME, payload + "\n"
            )
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        relative_vpn_path = f"vpn/{target_file}"
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "workspace_id": workspace_id,
                "vpn_file": relative_vpn_path,
            },
        )

    def retry_vpn_connection(
        self,
        runtime_job_id: str,
        *,
        command: str = f"VPN_CONFIG={CONTAINER_VPN_CONFIG_PATH} {IMAGE_INTERNAL_VPN_SCRIPT_PATH} reconnect",
    ) -> RunnerOperationResponse:
        """Run a bounded VPN reconnect probe in the task container."""
        return self._run_vpn_probe(runtime_job_id, command=command, operation="retry_vpn_connection")

    def check_vpn_status(
        self,
        runtime_job_id: str,
        *,
        command: str = "/opt/drowai/runtime/vpn/vpn-manager.sh status",
    ) -> RunnerOperationResponse:
        """Run a bounded VPN status probe in the task container."""
        return self._run_vpn_probe(runtime_job_id, command=command, operation="check_vpn_status")

    def _run_vpn_probe(
        self,
        runtime_job_id: str,
        *,
        command: str,
        operation: str,
    ) -> RunnerOperationResponse:
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_RUNTIME_JOB_NOT_FOUND,
                error_message=f"Unknown runtime job: {runtime_job_id}",
            )
        if not job.container_id:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_CONTAINER_NOT_ASSIGNED,
                error_message=f"Runtime job `{runtime_job_id}` has no assigned container.",
            )
        probe = self._docker_runtime.exec_probe(
            job.container_id,
            ["/bin/bash", "-lc", command],
            timeout_seconds=15,
        )
        status = "succeeded" if probe.exit_code == 0 else "failed"
        return RunnerOperationResponse(
            accepted=probe.exit_code == 0,
            status=status,
            metadata={
                "runtime_job_id": runtime_job_id,
                "task_id": job.task_id,
                "operation": operation,
                "exit_code": probe.exit_code,
                "stdout": probe.stdout,
                "stderr": probe.stderr,
            },
            error_code=None if probe.exit_code == 0 else "RUNNER_VPN_COMMAND_FAILED",
            error_message=None if probe.exit_code == 0 else "VPN command exited with non-zero status.",
        )

    def write_runtime_environment_metadata(
        self,
        workspace_id: str,
        *,
        key: str,
        value: str,
    ) -> RunnerOperationResponse:
        """Write one runtime environment metadata key-value pair."""
        normalized_key = key.strip()
        if not normalized_key:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_INVALID_ENV_METADATA_KEY,
                error_message="Environment metadata key must not be empty.",
            )
        if normalized_key not in ALLOWED_ENV_METADATA_KEYS:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_UNSUPPORTED_ENV_METADATA_KEY,
                error_message=(
                    "Environment metadata key is not supported. "
                    f"key={normalized_key!r}; allowlisted_keys={sorted(ALLOWED_ENV_METADATA_KEYS)}"
                ),
            )
        try:
            metadata = self._read_env_metadata_map(workspace_id)
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        metadata[normalized_key] = value
        try:
            self._write_env_metadata_map(workspace_id, metadata)
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={"workspace_id": workspace_id, "key": normalized_key, "value": value},
        )

    def read_runtime_environment_metadata(
        self,
        workspace_id: str,
        *,
        key: str,
    ) -> RunnerOperationResponse:
        """Read one runtime environment metadata value by key."""
        normalized_key = key.strip()
        if normalized_key not in ALLOWED_ENV_METADATA_KEYS:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_UNSUPPORTED_ENV_METADATA_KEY,
                error_message=(
                    "Environment metadata key is not supported. "
                    f"key={normalized_key!r}; allowlisted_keys={sorted(ALLOWED_ENV_METADATA_KEYS)}"
                ),
            )
        try:
            metadata = self._read_env_metadata_map(workspace_id)
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        return RunnerOperationResponse(
            accepted=normalized_key in metadata,
            status="succeeded" if normalized_key in metadata else "failed",
            error_code=None if normalized_key in metadata else "RUNNER_ENV_METADATA_NOT_FOUND",
            error_message=None if normalized_key in metadata else f"Metadata key `{normalized_key}` not found.",
            metadata={
                "workspace_id": workspace_id,
                "key": normalized_key,
                "value": metadata.get(normalized_key),
            },
        )

    def query_runtime_environment_metadata(
        self,
        workspace_id: str,
        *,
        key_prefix: str | None = None,
        runtime_job_id: str | None = None,
    ) -> RunnerOperationResponse:
        """Return runtime environment info and key/value metadata."""
        prefix = (key_prefix or "").strip()
        if prefix and not any(key.startswith(prefix) for key in ALLOWED_ENV_METADATA_KEYS):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_UNSUPPORTED_ENV_METADATA_FILTER,
                error_message=(
                    "Environment metadata query filter is not supported. "
                    f"key_prefix={prefix!r}; allowlisted_keys={sorted(ALLOWED_ENV_METADATA_KEYS)}"
                ),
            )
        try:
            metadata = self._read_env_metadata_map(workspace_id)
        except ValueError as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        metadata = {
            key: value
            for key, value in metadata.items()
            if key in ALLOWED_ENV_METADATA_KEYS
        }
        if prefix:
            metadata = {key: value for key, value in metadata.items() if key.startswith(prefix)}
        environment = self._load_or_collect_environment_info(
            workspace_id=workspace_id,
            runtime_job_id=runtime_job_id,
        )
        result_metadata: dict[str, Any] = {"workspace_id": workspace_id, "items": metadata}
        if environment is not None:
            result_metadata["environment"] = environment
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata=result_metadata,
        )

    def read_runtime_artifact_file(
        self,
        workspace_id: str,
        *,
        artifact_path: str,
        binary: bool = False,
        max_bytes: int | None = None,
        max_chars: int | None = None,
        encoding: str = "utf-8",
    ) -> RunnerOperationResponse:
        """Read one workspace file through workspace-relative paths only."""
        if not self._is_workspace_subpath(artifact_path):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_ARTIFACT_PATH_OUTSIDE_SCOPE,
                error_message="Workspace path must be task-local and must not target protected files.",
            )
        try:
            data = self._workspace_manager.read_workspace_bytes(workspace_id, artifact_path)
        except WorkspaceEntryUnsafeError:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_ENTRY_UNSAFE,
                error_message="Workspace entry is unsafe.",
            )
        except (WorkspacePathError, ValueError) as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        except FileNotFoundError:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_ARTIFACT_NOT_FOUND,
                error_message=f"Artifact file not found: {artifact_path}",
            )
        if max_bytes is not None and max_bytes >= 0:
            data = data[:max_bytes]
        if binary:
            return RunnerOperationResponse(
                accepted=True,
                status="succeeded",
                metadata={
                    "workspace_id": workspace_id,
                    "path": artifact_path,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                    "size": len(data),
                },
            )
        decode_encoding = encoding or "utf-8"
        try:
            content = data.decode(decode_encoding)
            resolved_encoding = decode_encoding
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
            resolved_encoding = "utf-8-replace"
        omitted_by_policy = False
        if max_chars is not None and max_chars >= 0 and len(content) > max_chars:
            content = content[:max_chars]
            omitted_by_policy = True
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "workspace_id": workspace_id,
                "path": artifact_path,
                "content": content,
                "encoding": resolved_encoding,
                "size": len(data),
                "omitted_by_policy": omitted_by_policy,
            },
        )

    def write_runtime_artifact_file(
        self,
        workspace_id: str,
        *,
        artifact_path: str,
        content_base64: str | None = None,
        content: str | None = None,
        encoding: str = "utf-8",
        mode: str = "write",
    ) -> RunnerOperationResponse:
        """Write one workspace file through workspace-relative paths only."""
        if not self._is_workspace_subpath(artifact_path):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_ARTIFACT_PATH_OUTSIDE_SCOPE,
                error_message="Workspace path must be task-local and must not target protected files.",
            )
        resolved_mode = normalize_workspace_write_mode(mode)
        if resolved_mode is None:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_WRITE_MODE_UNSUPPORTED,
                error_message="Workspace write mode must be `write` or `append`.",
            )
        if resolved_mode == WORKSPACE_WRITE_MODE_APPEND and not workspace_path_allows_append(artifact_path):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_WRITE_MODE_UNSUPPORTED,
                error_message="Workspace append mode is only supported for index writes.",
            )
        try:
            if content_base64 is not None:
                data = base64.b64decode(str(content_base64))
                resolved_encoding = "base64"
            else:
                resolved_encoding = encoding or "utf-8"
                data = str(content or "").encode(resolved_encoding)
            if resolved_mode == WORKSPACE_WRITE_MODE_APPEND:
                self._workspace_manager.append_workspace_bytes(
                    workspace_id, artifact_path, data, mode=0o600
                )
            else:
                self._workspace_manager.write_workspace_bytes(
                    workspace_id, artifact_path, data, mode=0o600
                )
        except WorkspaceEntryUnsafeError:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_ENTRY_UNSAFE,
                error_message="Workspace entry is unsafe.",
            )
        except (WorkspacePathError, ValueError) as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        except OSError:
            return RunnerOperationResponse(
                accepted=False,
                status="failed",
                error_code="RUNNER_ARTIFACT_WRITE_FAILED",
                error_message="Artifact file could not be written.",
            )
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "workspace_id": workspace_id,
                "path": artifact_path,
                "encoding": resolved_encoding,
                "mode": resolved_mode,
                "size": len(data),
            },
        )

    def query_runtime_artifacts(
        self,
        workspace_id: str,
        *,
        prefix: str = "",
    ) -> RunnerOperationResponse:
        """List workspace files under a workspace-relative prefix."""
        if not self._is_workspace_subpath(prefix, allow_root=True):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_ARTIFACT_PATH_OUTSIDE_SCOPE,
                error_message="Workspace query prefix must be task-local.",
            )
        normalized_prefix = prefix.strip()
        try:
            filesystem = self._workspace_manager.filesystem(workspace_id)
            if normalized_prefix:
                try:
                    entries = (filesystem.metadata(normalized_prefix),)
                except WorkspaceEntryUnsafeError:
                    entries = filesystem.list_entries(normalized_prefix, recursive=True)
            else:
                entries = filesystem.list_entries(None, recursive=True)
        except WorkspaceEntryUnsafeError:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_ENTRY_UNSAFE,
                error_message="Workspace entry is unsafe.",
            )
        except (WorkspacePathError, ValueError) as exc:
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_PATH_OUTSIDE_SCOPE,
                error_message=str(exc),
            )
        except FileNotFoundError:
            return RunnerOperationResponse(
                accepted=True,
                status="succeeded",
                metadata={"workspace_id": workspace_id, "items": []},
            )
        items: list[dict[str, Any]] = []
        try:
            for entry in entries:
                if entry.kind != "file":
                    continue
                relative_path = entry.relative_path
                if not self._is_workspace_subpath(relative_path):
                    continue
                current_entry = filesystem.metadata(relative_path, digest=True)
                items.append(
                    {
                        "path": relative_path,
                        "size": current_entry.size,
                        "content_sha256": current_entry.digest,
                    }
                )
        except (FileNotFoundError, WorkspaceEntryUnsafeError):
            return RunnerOperationResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_WORKSPACE_ENTRY_UNSAFE,
                error_message="Workspace entry is unsafe.",
            )
        return RunnerOperationResponse(
            accepted=True,
            status="succeeded",
            metadata={"workspace_id": workspace_id, "items": items},
        )

    def _read_env_metadata_map(self, workspace_id: str) -> dict[str, str]:
        try:
            payload_bytes = self._workspace_manager.read_workspace_bytes(
                workspace_id, ENV_METADATA_FILE, max_bytes=1024 * 1024
            )
        except FileNotFoundError:
            return {}
        raw_payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(raw_payload, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, value in raw_payload.items():
            normalized[str(key)] = str(value)
        return normalized

    def _write_env_metadata_map(self, workspace_id: str, payload: Mapping[str, str]) -> None:
        encoded = (json.dumps(dict(payload), sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
        self._workspace_manager.write_workspace_bytes(
            workspace_id, ENV_METADATA_FILE, encoded, mode=0o600
        )

    def _load_or_collect_environment_info(
        self,
        *,
        workspace_id: str,
        runtime_job_id: str | None,
    ) -> dict[str, Any] | None:
        environment = load_runner_environment_info(
            workspace_manager=self._workspace_manager,
            workspace_id=workspace_id,
        )
        if environment is not None:
            return environment
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        if not normalized_runtime_job_id:
            return None
        job = self._job_store.find_job(normalized_runtime_job_id)
        if job is None or job.workspace_id != workspace_id or not job.container_id:
            return None
        if job.status not in ACTIVE_JOB_STATUSES:
            return None
        try:
            return collect_and_save_runner_environment_info(
                docker_runtime=self._docker_runtime,
                workspace_manager=self._workspace_manager,
                container_id=job.container_id,
                workspace_id=workspace_id,
            )
        except Exception:
            return None

    def _resolve_workspace_child(self, workspace_id: str, candidate_path: str) -> Path:
        workspace = self._workspace_manager.resolve_task_workspace(workspace_id).resolve()
        candidate = Path(candidate_path.strip())
        if candidate.is_absolute():
            raise ValueError("Artifact path must be workspace-relative.")
        resolved = (workspace / candidate).resolve()
        if resolved == workspace or workspace in resolved.parents:
            return resolved
        raise ValueError("Artifact path escapes workspace scope.")

    @staticmethod
    def _is_workspace_subpath(candidate_path: str, *, allow_root: bool = False) -> bool:
        normalized = candidate_path.strip().replace("\\", "/")
        if not normalized:
            return allow_root
        if normalized.startswith("/") or ".." in Path(normalized).parts:
            return False
        protected_roots = {"vpn", "locks"}
        first_segment = Path(normalized).parts[0] if Path(normalized).parts else ""
        if first_segment in protected_roots:
            return False
        if allow_root and normalized == "":
            return True
        return True
