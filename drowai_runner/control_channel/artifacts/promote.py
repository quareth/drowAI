"""Data-plane artifact promote request handling — ACK, runtime validation delegation,
operation dispatch, cache synthesis, inline manifest scan, manifest send hand-off,
and promote completion event.

Mutates only the passed ``ConnectionSessionState`` ACK/processed/cache fields.
Performs websocket sends only through the call-time ``websocket`` object. Must not
import ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from drowai_runner.artifact_manifest import (
    ScannedArtifactFile,
    scan_runner_artifacts_for_manifest,
)
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.protocol_handler import (
    build_runner_ack_envelope,
    build_remote_runtime_envelope,
)
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.runner_protocol import (
    RunnerArtifactManifestPayload,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerToolResultPayload,
)

from drowai_runner.control_channel.artifacts.manifest import ArtifactManifestSender
from drowai_runner.control_channel.constants import _DATA_PLANE_ARTIFACT_WARNING_LIMIT
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.runtime.operation_map import map_remote_runtime_operation
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandCacheEntry,
)


class ArtifactPromoteHandler:
    """Handles inbound data_plane artifact promote requests."""

    def __init__(
        self,
        *,
        validate_runtime_request: Callable[
            ...,
            tuple[str, str | None, _RemoteRuntimeRequestContext | None],
        ],
        operation_service_provider: Callable[[], RunnerOperationService],
        workspace_manager: RunnerWorkspaceManager,
        manifest_sender: ArtifactManifestSender,
    ) -> None:
        self._validate_runtime_request = validate_runtime_request
        self._operation_service_provider = operation_service_provider
        self._workspace_manager = workspace_manager
        self._manifest_sender = manifest_sender

    def build_promote_cache_entry_from_params(
        self,
        *,
        promote_params: Mapping[str, object],
        inbound: RunnerEnvelope,
        command_key: tuple[str, str],
    ) -> _ToolCommandCacheEntry | None:
        """Synthesize a tool-command cache entry for upload-only promote requests."""
        tool_command_runtime_job_id = str(
            promote_params.get("tool_command_runtime_job_id") or ""
        ).strip()
        if not tool_command_runtime_job_id:
            return None

        workspace_id = str(promote_params.get("workspace_id") or "").strip()
        command_id = str(promote_params.get("command_id") or command_key[1]).strip()
        tool = str(promote_params.get("tool") or "").strip()
        canonical_status = str(promote_params.get("canonical_status") or "succeeded").strip()
        canonical_success = bool(
            promote_params.get("canonical_success", canonical_status == "succeeded")
        )
        try:
            canonical_exit_code = int(
                promote_params.get("canonical_exit_code")
                if promote_params.get("canonical_exit_code") is not None
                else (0 if canonical_success else 1)
            )
        except (TypeError, ValueError):
            canonical_exit_code = 0 if canonical_success else 1

        artifacts_raw = promote_params.get("artifacts")
        artifacts: tuple[str, ...] = ()
        if isinstance(artifacts_raw, (list, tuple)):
            artifacts = tuple(
                str(item).strip() for item in artifacts_raw if str(item).strip()
            )

        tool_call_id_raw = promote_params.get("tool_call_id")
        tool_call_id = (
            str(tool_call_id_raw).strip()
            if tool_call_id_raw is not None and str(tool_call_id_raw).strip()
            else None
        )
        tool_batch_id_raw = promote_params.get("tool_batch_id")
        tool_batch_id = (
            str(tool_batch_id_raw).strip()
            if tool_batch_id_raw is not None and str(tool_batch_id_raw).strip()
            else None
        )

        result_payload = RunnerToolResultPayload(
            operation_id=command_id,
            command_id=command_id,
            tool=tool,
            status=canonical_status,
            success=canonical_success,
            exit_code=canonical_exit_code,
            stdout=str(promote_params.get("stdout") or ""),
            stderr=str(promote_params.get("stderr") or ""),
            artifacts=artifacts,
            error_code=None,
            error_message=None,
            result={},
            metadata={},
        )
        return _ToolCommandCacheEntry(
            task_runtime_job_id=command_key[0],
            command_id=command_key[1],
            tool_command_runtime_job_id=tool_command_runtime_job_id,
            task_id=inbound.task_id,
            result_payload=result_payload,
            workspace_id=workspace_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            manifest_payload=None,
            files_by_client_id={},
            upload_completions_by_object_key={},
        )

    def handle_promote(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
        session_state: ConnectionSessionState,
    ) -> None:
        """Scan declared refs and emit artifact manifest/upload only."""
        normalized_message_id = str(inbound.message_id).strip()
        cached_decision = session_state.ack_decisions_by_message_id.get(normalized_message_id)
        validated_context: _RemoteRuntimeRequestContext | None = None
        if cached_decision is None:
            status, error_code, validated_context = self._validate_runtime_request(
                identity=identity,
                inbound=inbound,
            )
            status = str(status or "accepted").strip() or "accepted"
            error_code = (
                str(error_code).strip()
                if error_code is not None and str(error_code).strip()
                else None
            )
            cached_decision = (status, error_code)
            session_state.ack_decisions_by_message_id[normalized_message_id] = cached_decision

        status, error_code = cached_decision
        ack = build_runner_ack_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            acked_message_id=inbound.message_id,
            status=status,
            error_code=error_code,
            correlation_id=inbound.correlation_id,
            protocol_version=identity.protocol_version,
        )
        websocket.send(ack.to_json())
        if (
            status != "accepted"
            or normalized_message_id in session_state.processed_runtime_messages
        ):
            return
        session_state.processed_runtime_messages.add(normalized_message_id)

        if validated_context is None:
            validation = self._validate_runtime_request(identity=identity, inbound=inbound)
            if validation[0] != "accepted" or validation[2] is None:
                return
            validated_context = validation[2]

        operation_name, operation_params = map_remote_runtime_operation(
            inbound=inbound,
            context=validated_context,
        )
        response = self._operation_service_provider().dispatch_operation(
            operation=operation_name,
            params=operation_params,
        )
        promote_params = operation_params
        command_key = (
            str(promote_params.get("task_runtime_job_id") or "").strip(),
            str(promote_params.get("command_id") or "").strip(),
        )
        existing_cache = session_state.cached_tool_command_results.get(command_key)
        if existing_cache is None:
            existing_cache = self.build_promote_cache_entry_from_params(
                promote_params=promote_params,
                inbound=inbound,
                command_key=command_key,
            )
        if existing_cache is None:
            return

        artifacts_raw = promote_params.get("artifacts")
        artifacts_candidates: list[str] = []
        if isinstance(artifacts_raw, (list, tuple)):
            artifacts_candidates = [str(item).strip() for item in artifacts_raw if str(item).strip()]

        manifest_payload: RunnerArtifactManifestPayload | None = None
        files_by_client_id: dict[str, ScannedArtifactFile] = {}
        workspace_id = str(promote_params.get("workspace_id") or validated_context.workspace_id).strip()
        if artifacts_candidates:
            try:
                workspace_path = self._workspace_manager.resolve_task_workspace(workspace_id)
            except ValueError:
                pass
            else:
                scan_result = scan_runner_artifacts_for_manifest(
                    workspace_path=workspace_path,
                    artifacts=artifacts_candidates,
                    max_warnings=_DATA_PLANE_ARTIFACT_WARNING_LIMIT,
                )
                files_by_client_id = dict(scan_result.files_by_client_id)
                if scan_result.manifest_items:
                    manifest_payload = RunnerArtifactManifestPayload(
                        task_runtime_job_id=str(promote_params.get("task_runtime_job_id") or "").strip(),
                        command_id=str(promote_params.get("command_id") or "").strip(),
                        workspace_id=workspace_id,
                        tool_call_id=(
                            str(promote_params.get("tool_call_id")).strip()
                            if promote_params.get("tool_call_id") is not None
                            and str(promote_params.get("tool_call_id")).strip()
                            else None
                        ),
                        tool_batch_id=(
                            str(promote_params.get("tool_batch_id")).strip()
                            if promote_params.get("tool_batch_id") is not None
                            and str(promote_params.get("tool_batch_id")).strip()
                            else None
                        ),
                        artifacts=scan_result.manifest_items,
                    )

        cache_entry = replace(
            existing_cache,
            manifest_payload=manifest_payload,
            files_by_client_id=files_by_client_id,
        )
        cache_entry = self._manifest_sender.send_if_available(
            websocket=websocket,
            identity=identity,
            command_key=command_key,
            cache_entry=cache_entry,
            session_state=session_state,
            correlation_id=inbound.correlation_id,
        )
        session_state.cached_tool_command_results[command_key] = cache_entry

        payload = inbound.payload
        operation_id = str(getattr(payload, "operation_id", "") or promote_params.get("operation_id") or "").strip()
        accepted = bool(response.get("accepted"))
        event_status = str(response.get("status") or "failed")
        response_metadata = response.get("metadata")
        result_payload: dict[str, object] = {
            "runtime_job_id": validated_context.runtime_job_id,
            "task_id": validated_context.task_id,
            "workspace_id": validated_context.workspace_id,
        }
        if isinstance(response_metadata, Mapping):
            result_payload.update(dict(response_metadata))
        event_payload = {
            "operation_id": operation_id,
            "status": event_status,
            "error_code": (
                str(response.get("error_code"))
                if response.get("error_code") is not None
                else None
            ),
            "error_message": (
                str(response.get("error_message"))
                if response.get("error_message") is not None
                else None
            ),
            "result": result_payload,
        }
        event_type = (
            RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE
            if accepted and event_status == "succeeded"
            else RunnerMessageType.RUNTIME_FAILED
        )
        event = build_remote_runtime_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            message_type=event_type,
            payload=event_payload,
            correlation_id=inbound.correlation_id,
            runtime_job_id=inbound.runtime_job_id,
            task_id=inbound.task_id,
        )
        websocket.send(event.to_json())
