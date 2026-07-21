"""Local Docker runtime provider implementation.

Responsibilities:
- Delegate task runtime operations to existing local Docker/workspace services.
- Normalize delegated operation results into runtime-provider contracts.
- Keep management-plane callers decoupled from direct Docker service imports.
"""

from __future__ import annotations

import inspect
import json
import signal
import base64
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config.workspace_config import WorkspaceConfig
from backend.core.time_utils import format_iso, utc_now
from backend.services.docker.runtime_config import contains_llm_secret_fields
from backend.services.unified_docker_service import unified_docker_service
from backend.services.runtime_provider.local_file_comm_cancel import append_file_comm_cancellations
from backend.services.workspace.manager import WorkspaceManager
from runtime_shared.workspace_write_mode import (
    WORKSPACE_WRITE_MODE_APPEND,
    WORKSPACE_WRITE_MODE_WRITE,
    normalize_workspace_write_mode,
    workspace_path_allows_append,
)
from runtime_shared.workspace_filesystem import (
    WorkspaceFilesystem,
    normalize_workspace_relative_path,
)

from .contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)
from .provider import TaskExecutionRuntimeProvider

_Adapter = Callable[[RuntimeOperationRequest], Any]


class LocalDockerRuntimeProvider(TaskExecutionRuntimeProvider):
    """Task runtime provider backed by the existing local Docker stack."""

    def __init__(
        self,
        *,
        docker_service: Any = unified_docker_service,
        workspace_manager: WorkspaceManager | None = None,
        operation_adapters: Mapping[str, _Adapter] | None = None,
    ) -> None:
        self._docker_service = docker_service
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._operation_adapters = dict(operation_adapters or {})

    @property
    def provider_name(self) -> str:
        return "local_docker"

    async def provision_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        if contains_llm_secret_fields(request.payload) or contains_llm_secret_fields(
            request.metadata
        ):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self.provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code="llm_secret_payload_forbidden",
                error_message=(
                    "Task runtime provisioning payloads must not contain LLM connection secrets."
                ),
            )
        return await self._call_delegate(
            request,
            self._docker_service.create_and_start_container,
            target=request.payload.get("target", "127.0.0.1"),
            user_id=request.user_id,
            tenant_id=request.tenant_id,
        )

    async def materialize_runtime_workspace(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._materialize_workspace,
            inject_task_id=False,
        )

    async def pause_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._docker_service.pause_container)

    async def resume_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._docker_service.unpause_container)

    async def stop_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._docker_service.stop_container)

    async def retire_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        force = bool(request.payload.get("force", True))
        return await self._call_delegate(
            request,
            self._retire_runtime,
            inject_task_id=False,
            force=force,
        )

    async def append_runtime_input(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._append_runtime_input, inject_task_id=False)

    async def materialize_vpn_config(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._materialize_vpn_config,
            inject_task_id=False,
        )

    async def retry_vpn_connection(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._retry_vpn_connection_with_exec)

    async def check_vpn_status(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        command = request.payload.get(
            "command",
            "ps aux | grep -i openvpn | grep -v grep || true",
        )
        return await self._call_delegate(
            request,
            self._docker_service.execute_container_command,
            command=command,
        )

    async def get_runtime_status(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._docker_service.get_container_status)

    async def get_runtime_startup_progress(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._docker_service.get_container_startup_progress,
        )

    async def get_runtime_logs(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._docker_service.get_container_logs,
            lines=request.payload.get("lines", 50),
        )

    async def get_runtime_metrics(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._docker_service.get_container_metrics)

    async def list_runtime_inventory(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._list_runtime_inventory,
            inject_task_id=False,
        )

    async def cleanup_runtime_workspace(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._cleanup_workspace,
            inject_task_id=False,
        )

    async def read_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._read_runtime_environment_metadata,
            inject_task_id=False,
        )

    async def write_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._write_runtime_environment_metadata,
            inject_task_id=False,
        )

    async def query_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(
            request,
            self._read_runtime_environment_metadata,
            inject_task_id=False,
        )

    async def open_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._open_terminal_session, inject_task_id=False)

    async def send_terminal_input(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._send_terminal_input, inject_task_id=False)

    async def read_terminal_output(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._read_terminal_output, inject_task_id=False)

    async def resize_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._resize_terminal_session, inject_task_id=False)

    async def close_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_delegate(request, self._close_terminal_session, inject_task_id=False)

    async def execute_runtime_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        command = request.payload.get("command") or request.payload.get("input")
        if not command:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self.provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code="missing_command",
                error_message="`command` (or `input`) is required for runtime command execution.",
            )
        return await self._call_delegate(
            request,
            self._docker_service.execute_container_command,
            command=command,
        )

    async def dispatch_tool_execution(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        dispatch_callable = request.payload.get("dispatch_callable")
        if callable(dispatch_callable):
            return await self._call_delegate(request, dispatch_callable, inject_task_id=False)
        return await self._call_adapter("dispatch_tool_execution", request)

    async def read_runtime_artifact_file(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        adapter = self._operation_adapters.get("read_runtime_artifact_file")
        if adapter is not None:
            return await self._call_delegate(request, adapter, inject_task_id=False)
        return await self._call_delegate(
            request,
            self._read_runtime_workspace_file,
            inject_task_id=False,
        )

    async def promote_artifact_refs(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=True,
            provider=self.provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={"promotion": "local_noop"},
        )

    async def finalize_tool_command_result(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=True,
            provider=self.provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={"finalize": "local_noop"},
        )

    async def write_runtime_artifact_file(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        adapter = self._operation_adapters.get("write_runtime_artifact_file")
        if adapter is not None:
            return await self._call_delegate(request, adapter, inject_task_id=False)
        return await self._call_delegate(
            request,
            self._write_runtime_workspace_file,
            inject_task_id=False,
        )

    async def query_runtime_artifacts(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_adapter("query_runtime_artifacts", request)

    async def send_tool_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._call_adapter("send_tool_command", request)

    async def cancel_tool_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        command_targets = request.payload.get("commands")
        if isinstance(command_targets, list):
            file_comm_command_ids = tuple(
                dict.fromkeys(
                    str(target.get("command_id") or "").strip()
                    for target in command_targets
                    if isinstance(target, Mapping)
                    and str(target.get("command_id") or "").strip()
                    and str(target.get("execution_transport") or "file_comm").strip().lower()
                    in {"", "file_comm", "local_file_comm"}
                )
            )
            all_command_ids = tuple(
                dict.fromkeys(
                    str(target.get("command_id") or "").strip()
                    for target in command_targets
                    if isinstance(target, Mapping) and str(target.get("command_id") or "").strip()
                )
            )
        else:
            file_comm_command_ids = tuple(
                dict.fromkeys(
                    str(command_id or "").strip()
                    for command_id in (request.payload.get("command_ids") or ())
                    if str(command_id or "").strip()
                )
            )
            all_command_ids = file_comm_command_ids
        supported_command_id_set = set(file_comm_command_ids)
        unsupported_command_ids = tuple(
            command_id
            for command_id in all_command_ids
            if command_id not in supported_command_id_set
        )
        command_ids = tuple(
            dict.fromkeys(
                file_comm_command_ids
            )
        )
        if not command_ids:
            return build_runtime_result(
                request,
                accepted=True,
                provider=self.provider_name,
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "runtime_kill_attempted": False,
                    "runtime_kill_supported": False,
                    "process_state": "orphaned_until_terminal" if all_command_ids else "cancel_requested",
                    "reason": "unsupported_transport" if all_command_ids else "no_command_ids",
                    "command_ids": list(all_command_ids),
                    "unsupported_command_ids": list(unsupported_command_ids),
                },
            )

        try:
            workspace_path = WorkspaceConfig.ensure_workspace_structure(request.task_id)
            cancel_result = append_file_comm_cancellations(
                workspace_path=workspace_path,
                command_ids=command_ids,
                reason=str(request.payload.get("reason") or "user_stop"),
                source=str(request.payload.get("source") or "chat_stop"),
            )
        except Exception as exc:
            return build_runtime_result(
                request,
                accepted=True,
                provider=self.provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code="tool_cancel_dispatch_failed",
                error_message=str(exc),
                metadata={
                    "runtime_kill_attempted": True,
                    "runtime_kill_supported": True,
                    "process_state": "cancel_requested",
                    "command_ids": list(command_ids),
                    "supported_command_ids": list(command_ids),
                    "unsupported_command_ids": list(unsupported_command_ids),
                },
            )

        return build_runtime_result(
            request,
            accepted=True,
            provider=self.provider_name,
            status=RuntimeOperationStatus.ACCEPTED,
            metadata={
                "runtime_kill_attempted": bool(cancel_result.command_ids),
                "runtime_kill_supported": True,
                "process_state": "cancel_requested",
                "command_ids": list(cancel_result.command_ids),
                "supported_command_ids": list(cancel_result.command_ids),
                "unsupported_command_ids": list(unsupported_command_ids),
                "cancellation_ids": list(cancel_result.cancellation_ids),
                "cancellation_transport": "file_comm",
            },
        )

    async def _call_adapter(
        self,
        operation_name: str,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        adapter = self._operation_adapters.get(operation_name)
        if adapter is None:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self.provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code="operation_not_configured",
                error_message=f"Local adapter for `{operation_name}` is not configured.",
            )
        return await self._call_delegate(request, adapter, inject_task_id=False)

    async def _call_delegate(
        self,
        request: RuntimeOperationRequest,
        delegate: Callable[..., Any],
        *,
        inject_task_id: bool = True,
        **kwargs: Any,
    ) -> RuntimeOperationResult:
        try:
            if inject_task_id:
                delegated_result = delegate(request.task_id, **kwargs)
            else:
                delegated_result = delegate(request)

            if inspect.isawaitable(delegated_result):
                delegated_result = await delegated_result

            return self._normalize_delegate_result(request, delegated_result)
        except Exception as exc:  # pragma: no cover - defensive normalization guard
            return build_runtime_result(
                request,
                accepted=False,
                provider=self.provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code="runtime_operation_failed",
                error_message=str(exc),
                metadata={"exception_type": type(exc).__name__},
            )

    def _normalize_delegate_result(
        self,
        request: RuntimeOperationRequest,
        delegated_result: Any,
    ) -> RuntimeOperationResult:
        if isinstance(delegated_result, RuntimeOperationResult):
            return delegated_result

        accepted = True
        status = RuntimeOperationStatus.SUCCEEDED
        error_code = None
        error_message = None
        metadata: dict[str, Any] = {"delegate_result": delegated_result}

        if isinstance(delegated_result, Mapping):
            raw_accepted = delegated_result.get("accepted")
            raw_success = delegated_result.get("success")
            raw_status = delegated_result.get("status")
            raw_error = delegated_result.get("error")

            if raw_accepted is not None:
                accepted = bool(raw_accepted)
            elif raw_success is not None:
                accepted = bool(raw_success)

            if isinstance(raw_status, str):
                status_value = raw_status.lower().strip()
                try:
                    status = RuntimeOperationStatus(status_value)
                except ValueError:
                    if not accepted:
                        status = RuntimeOperationStatus.FAILED
                    elif status_value in {"running", "in_progress"}:
                        status = RuntimeOperationStatus.RUNNING

            if raw_error:
                accepted = False
                if status == RuntimeOperationStatus.SUCCEEDED:
                    status = RuntimeOperationStatus.FAILED
                if isinstance(raw_error, Mapping):
                    error_code = str(raw_error.get("code") or "runtime_operation_failed")
                    error_message = str(raw_error.get("message") or raw_error)
                else:
                    error_code = "runtime_operation_failed"
                    error_message = str(raw_error)

            if not accepted and status == RuntimeOperationStatus.SUCCEEDED:
                status = RuntimeOperationStatus.FAILED
        elif isinstance(delegated_result, tuple) and delegated_result:
            metadata["delegate_result"] = delegated_result
            if isinstance(delegated_result[0], bool):
                accepted = bool(delegated_result[0])
                if not accepted:
                    status = RuntimeOperationStatus.FAILED
                    error_code = "runtime_operation_failed"
                    if len(delegated_result) > 1:
                        error_message = str(delegated_result[1])

        return build_runtime_result(
            request,
            accepted=accepted,
            provider=self.provider_name,
            status=status,
            error_code=error_code,
            error_message=error_message,
            metadata=metadata,
        )

    def _materialize_workspace(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        task_id = request.task_id
        workspace_path = self._workspace_manager.create_workspace(task_id)

        config_data = request.payload.get("config_data")
        if isinstance(config_data, dict) and config_data:
            self._workspace_manager.save_config_file(task_id, config_data)

        scope_content = request.payload.get("scope_content")
        scope_file_path = None
        if isinstance(scope_content, str) and scope_content.strip():
            scope_file_path = self._workspace_manager.save_scope_file(task_id, scope_content)

        return {
            "workspace_path": workspace_path,
            "workspace_id": request.workspace_id or f"task-{task_id}",
            "container_workspace_path": WorkspaceConfig.get_container_workspace_path(),
            "scope_file_path": str(scope_file_path) if scope_file_path is not None else None,
        }

    def _materialize_vpn_config(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        vpn_config = request.payload.get("vpn_config")
        ovpn_path = None
        if vpn_config is not None:
            config_data = getattr(vpn_config, "config_data", None)
            if not isinstance(config_data, str) or not config_data.strip():
                raise ValueError("vpn_config.config_data is required for VPN materialization")
            ovpn_path = self._write_vpn_config_file(request.task_id, config_data)

        mount_policy = None
        get_fields = getattr(self._docker_service, "get_runtime_path_diagnostic_fields", None)
        get_vpn_script = getattr(
            self._docker_service,
            "get_vpn_script_path_for_current_mode",
            None,
        )
        if callable(get_fields):
            fields = get_fields(mount_policy)
        else:
            fields = {}
        if callable(get_vpn_script):
            fields = {**fields, "vpn_script_path": get_vpn_script()}
        if ovpn_path is not None:
            fields = {**fields, "ovpn_path": str(ovpn_path), "configured": True}
        return fields

    @staticmethod
    def _write_vpn_config_file(task_id: int, config_data: str) -> Path:
        control_relative_path = "vpn/task.ovpn"
        WorkspaceConfig.control_filesystem(task_id).write_bytes_atomic(
            control_relative_path,
            config_data.encode("utf-8"),
            mode=0o600,
        )
        return WorkspaceConfig.get_task_workspace_path(task_id) / f"vpn/task-{task_id}.ovpn"

    def _runtime_environment_file(self, task_id: int) -> Path:
        from backend.services.workspace.environment_collector import ENV_INFO_FILENAME

        return WorkspaceConfig.get_task_workspace_path(task_id) / ENV_INFO_FILENAME

    def _read_runtime_environment_metadata(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        env_file = self._runtime_environment_file(request.task_id)
        try:
            raw_data = WorkspaceFilesystem(env_file.parent).read_bytes(env_file.name)
        except FileNotFoundError:
            return {"success": True, "found": False, "environment": None}
        data = json.loads(raw_data.decode("utf-8"))
        return {"success": True, "found": True, "environment": data}

    def _write_runtime_environment_metadata(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        env_info = request.payload.get("environment") or request.payload.get("env_info")
        if not isinstance(env_info, Mapping):
            raise ValueError("environment metadata payload is required")
        env_file = self._runtime_environment_file(request.task_id)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        WorkspaceFilesystem(env_file.parent).write_bytes_atomic(
            env_file.name,
            json.dumps(dict(env_info), indent=2).encode("utf-8"),
            mode=0o600,
        )
        return {"success": True, "path": str(env_file), "environment": dict(env_info)}

    async def _retry_vpn_connection_with_exec(self, task_id: int) -> Any:
        command = self._docker_service.build_vpn_connect_exec_shell(task_id, reconnect=True)
        result = self._docker_service.execute_container_command(task_id, command)
        if isinstance(result, Awaitable):
            return await result
        return result

    async def _retire_runtime(
        self,
        request: RuntimeOperationRequest,
        *,
        force: bool = True,
    ) -> dict[str, Any]:
        task_id = request.task_id
        container_status = await self._docker_service.get_container_status(task_id)
        stopped = None
        if container_status in {"running", "paused", "restarting"}:
            stopped = await self._docker_service.stop_container(task_id)
            if isinstance(stopped, tuple) and stopped and stopped[0] is False:
                return {"success": False, "error": stopped[1] if len(stopped) > 1 else "stop_failed"}
        removed = (True, "not_found")
        if container_status != "not_found":
            removed = await self._docker_service.remove_container(task_id, force=force)
        if isinstance(removed, tuple) and removed and removed[0] is False:
            message = str(removed[1] if len(removed) > 1 else "")
            if "not found" not in message.lower() and "no such container" not in message.lower():
                return {"success": False, "error": message or "remove_failed"}
        cleanup_ok = self._workspace_manager.cleanup_workspace(
            task_id,
            True,
            engagement_id=self._coerce_int_or_none(request.payload.get("engagement_id")),
        )
        return {
            "success": bool(cleanup_ok is not False),
            "container_status": container_status,
            "stopped": stopped,
            "removed": removed,
            "workspace_cleaned": cleanup_ok is not False,
        }

    async def _list_runtime_inventory(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        get_all = getattr(self._docker_service, "get_all_containers", None)
        if not callable(get_all):
            return {"containers": [], "total": 0}
        containers = get_all()
        if inspect.isawaitable(containers):
            containers = await containers
        return {"containers": containers, "total": len(containers or [])}

    def _cleanup_workspace(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        ok = self._workspace_manager.cleanup_workspace(
            request.task_id,
            bool(request.payload.get("archive", True)),
            engagement_id=request.payload.get("engagement_id"),
        )
        return {"success": ok is not False}

    def _resolve_workspace_file(self, request: RuntimeOperationRequest) -> Path:
        workspace = WorkspaceConfig.get_task_workspace_path(request.task_id)
        raw_path = str(request.payload.get("path") or request.payload.get("file_path") or "").strip()
        if not raw_path:
            raise ValueError("path is required")
        if raw_path == "/workspace":
            raise ValueError("path points to runtime workspace root")
        if raw_path.startswith("/workspace/"):
            raw_path = raw_path[len("/workspace/") :]
        absolute_candidate = Path(raw_path)
        if absolute_candidate.is_absolute():
            try:
                relative = absolute_candidate.relative_to(workspace)
            except ValueError as exc:
                raise ValueError("path resolves outside runtime workspace") from exc
            raw_path = relative.as_posix()
        relative_path = normalize_workspace_relative_path(raw_path.lstrip("/\\"))
        return workspace / relative_path

    def _read_runtime_workspace_file(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        resolved = self._resolve_workspace_file(request)
        workspace = WorkspaceConfig.get_task_workspace_path(request.task_id)
        relative_path = resolved.relative_to(workspace).as_posix()
        filesystem = WorkspaceFilesystem(workspace)
        max_bytes = self._coerce_int_or_none(request.payload.get("max_bytes"))
        data = filesystem.read_bytes(relative_path)
        if max_bytes is not None and max_bytes >= 0:
            data = data[:max_bytes]
        file_metadata = filesystem.metadata(relative_path)
        if request.payload.get("binary"):
            import base64

            return {
                "success": True,
                "path": str(request.payload.get("path") or request.payload.get("file_path")),
                "content_base64": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "size": len(data),
                "modified": format_iso(datetime.fromtimestamp(file_metadata.modified_at, tz=UTC)),
            }
        try:
            content = data.decode(str(request.payload.get("encoding") or "utf-8"))
            encoding = str(request.payload.get("encoding") or "utf-8")
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
            encoding = "utf-8-replace"
        max_chars = self._coerce_int_or_none(request.payload.get("max_chars"))
        omitted_by_policy = False
        if max_chars is not None and max_chars >= 0 and len(content) > max_chars:
            content = content[:max_chars]
            omitted_by_policy = True
        return {
            "success": True,
            "path": str(request.payload.get("path") or request.payload.get("file_path")),
            "content": content,
            "encoding": encoding,
            "size": len(data),
            "omitted_by_policy": omitted_by_policy,
            "modified": format_iso(datetime.fromtimestamp(file_metadata.modified_at, tz=UTC)),
        }

    def _write_runtime_workspace_file(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        resolved = self._resolve_workspace_file(request)
        workspace = WorkspaceConfig.get_task_workspace_path(request.task_id)
        relative_path = resolved.relative_to(workspace).as_posix()
        filesystem = WorkspaceFilesystem(workspace)
        content_base64 = request.payload.get("content_base64")
        if isinstance(content_base64, str):
            data = base64.b64decode(content_base64)
            encoding = "base64"
        else:
            content = str(request.payload.get("content") or "")
            text_encoding = str(request.payload.get("encoding") or "utf-8")
            data = content.encode(text_encoding)
            encoding = text_encoding
        mode = normalize_workspace_write_mode(request.payload.get("mode"))
        requested_path = request.payload.get("artifact_path") or request.payload.get("path") or request.payload.get("file_path")
        if mode is None:
            return {
                "accepted": False,
                "status": "rejected",
                "error": {
                    "code": "runtime_workspace_write_mode_invalid",
                    "message": "Workspace write mode must be `write` or `append`.",
                },
            }
        if mode == WORKSPACE_WRITE_MODE_APPEND and not workspace_path_allows_append(requested_path):
            return {
                "accepted": False,
                "status": "rejected",
                "error": {
                    "code": "runtime_workspace_append_scope_invalid",
                    "message": "Workspace append mode is only supported for index writes.",
                },
            }
        if mode == WORKSPACE_WRITE_MODE_APPEND:
            filesystem.append_bytes(relative_path, data, mode=0o600)
        else:
            filesystem.write_bytes_atomic(relative_path, data, mode=0o600)
        file_metadata = filesystem.metadata(relative_path)
        return {
            "success": True,
            "path": relative_path,
            "encoding": encoding,
            "mode": mode or WORKSPACE_WRITE_MODE_WRITE,
            "size": len(data),
            "modified": format_iso(datetime.fromtimestamp(file_metadata.modified_at, tz=UTC)),
        }

    async def _append_runtime_input(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        WorkspaceConfig.ensure_control_structure(request.task_id)
        entry = {
            "timestamp": format_iso(utc_now()),
            "message": request.payload.get("message", ""),
        }
        metadata = request.payload.get("metadata")
        if isinstance(metadata, Mapping):
            entry["metadata"] = dict(metadata)
        try:
            WorkspaceConfig.control_filesystem(request.task_id).append_bytes(
                "runtime-input/user_input.jsonl",
                (json.dumps(entry) + "\n").encode("utf-8"),
                mode=0o600,
            )
            persisted = True
            append_error = None
        except Exception as exc:
            persisted = False
            append_error = str(exc)
            if request.payload.get("strict_persistence"):
                return {"success": False, "persisted": False, "error": append_error}
        signal_result = await self._docker_service.send_signal(
            request.task_id,
            signal.SIGUSR1.name,
        )
        signal_sent = bool(signal_result[0]) if isinstance(signal_result, tuple) else bool(signal_result)
        return {
            "success": persisted or not request.payload.get("strict_persistence"),
            "persisted": persisted,
            "signal_attempted": True,
            "signal_sent": signal_sent,
            "detail": append_error if append_error else (
                None if signal_sent else str(signal_result[1] if isinstance(signal_result, tuple) and len(signal_result) > 1 else signal_result)
            ),
        }

    async def _open_terminal_session(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        exec_id, sock = await self._docker_service.start_persistent_pty(
            request.task_id,
            shell=request.payload.get("shell", "/bin/bash"),
            cols=int(request.payload.get("cols", 80)),
            rows=int(request.payload.get("rows", 24)),
        )
        return {
            "success": True,
            "exec_id": exec_id,
            "socket": sock,
            "container_name": self._docker_service.get_container_name_by_id(request.task_id),
        }

    async def _send_terminal_input(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        socket_obj = request.payload.get("socket")
        data = request.payload.get("data", b"")
        if isinstance(data, str):
            data = data.encode()
        if not socket_obj:
            return {"success": False, "error": "missing_socket"}
        import asyncio

        loop = asyncio.get_running_loop()
        raw_sock = getattr(socket_obj, "_sock", socket_obj)
        try:
            raw_sock.setblocking(False)
        except Exception:
            pass
        await loop.sock_sendall(raw_sock, data)
        return {"success": True}

    async def _read_terminal_output(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        socket_obj = request.payload.get("socket")
        if not socket_obj:
            return {"success": False, "error": "missing_socket"}
        import asyncio

        loop = asyncio.get_running_loop()
        raw_sock = getattr(socket_obj, "_sock", socket_obj)
        try:
            raw_sock.setblocking(False)
        except Exception:
            pass
        size = int(request.payload.get("size", 4096))
        timeout = request.payload.get("timeout")
        read_coro = loop.sock_recv(raw_sock, size)
        if timeout is not None:
            data = await asyncio.wait_for(read_coro, timeout=float(timeout))
        else:
            data = await read_coro
        return {"success": True, "data": data}

    def _resize_terminal_session(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        exec_id = request.payload.get("exec_id")
        if not exec_id:
            return {"success": False, "error": "missing_exec_id"}
        if not getattr(self._docker_service, "client", None):
            return {"success": False, "error": "docker_client_unavailable"}
        self._docker_service.client.api.exec_resize(
            exec_id,
            height=int(request.payload.get("rows", 24)),
            width=int(request.payload.get("cols", 80)),
        )
        return {"success": True}

    def _close_terminal_session(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        socket_obj = request.payload.get("socket")
        if socket_obj is not None:
            raw_sock = getattr(socket_obj, "_sock", socket_obj)
            try:
                raw_sock.close()
            except Exception:
                pass
        return {"success": True}

    @staticmethod
    def _coerce_int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None


__all__ = ["LocalDockerRuntimeProvider"]
