"""Data-plane artifact manifest construction and conditional send.

Builds the tool-command artifact-manifest context by scanning the task
workspace, and conditionally sends the data_plane manifest envelope when the cache
entry has a runtime job and the negotiated protocol version supports data_plane,
registering the pending upload context on the passed connection-session state.

Boundary: this collaborator mutates only the provided
``ConnectionSessionState.pending_upload_contexts`` and owns no client-lifetime
state. It performs websocket sends only through the call-time ``websocket``
object and never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.artifact_manifest import (
    ScannedArtifactFile,
    scan_runner_artifacts_for_manifest,
)
from drowai_runner.protocol_handler import build_data_plane_artifact_manifest_envelope
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactManifestPayload,
    RunnerEnvelope,
)

from drowai_runner.control_channel.artifacts.metadata import (
    _artifact_manifest_metadata_patch,
)
from drowai_runner.control_channel.artifacts.models import _PendingArtifactUploadContext
from drowai_runner.control_channel.constants import _DATA_PLANE_ARTIFACT_WARNING_LIMIT
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandCacheEntry,
)


class ArtifactManifestSender:
    """Builds data_plane manifest context and conditionally sends manifests."""

    def __init__(self, *, workspace_manager: RunnerWorkspaceManager) -> None:
        self._workspace_manager = workspace_manager

    def build_manifest_context(
        self,
        *,
        inbound: RunnerEnvelope,
        response: Mapping[str, object],
    ) -> tuple[
        RunnerArtifactManifestPayload | None,
        Mapping[str, ScannedArtifactFile],
        Mapping[str, object],
        tuple[str, ...],
    ]:
        payload = inbound.payload
        response_metadata = response.get("metadata")
        metadata_mapping = dict(response_metadata) if isinstance(response_metadata, Mapping) else {}
        artifacts_raw = metadata_mapping.get("artifacts")
        artifacts_candidates = (
            list(artifacts_raw)
            if isinstance(artifacts_raw, (list, tuple))
            else []
        )
        workspace_id = str(payload.workspace_id).strip()
        if not artifacts_candidates:
            return None, {}, {"artifact_manifest": {"status": "none", "declared_count": 0}}, ()

        try:
            workspace_path = self._workspace_manager.resolve_task_workspace(workspace_id)
        except ValueError:
            return None, {}, {
                "artifact_manifest": {
                    "status": "skipped_invalid_workspace",
                    "declared_count": len(artifacts_candidates),
                    "accepted_count": 0,
                    "skipped_count": len(artifacts_candidates),
                }
            }, ()

        scan_result = scan_runner_artifacts_for_manifest(
            workspace_path=workspace_path,
            artifacts=artifacts_candidates,
            max_warnings=_DATA_PLANE_ARTIFACT_WARNING_LIMIT,
        )
        manifest_payload: RunnerArtifactManifestPayload | None = None
        if scan_result.manifest_items:
            manifest_payload = RunnerArtifactManifestPayload(
                task_runtime_job_id=str(payload.task_runtime_job_id).strip(),
                command_id=str(payload.command_id).strip(),
                workspace_id=workspace_id,
                tool_call_id=(
                    str(payload.tool_call_id).strip()
                    if payload.tool_call_id is not None and str(payload.tool_call_id).strip()
                    else None
                ),
                tool_batch_id=(
                    str(payload.tool_batch_id).strip()
                    if payload.tool_batch_id is not None and str(payload.tool_batch_id).strip()
                    else None
                ),
                artifacts=scan_result.manifest_items,
            )
        return (
            manifest_payload,
            scan_result.files_by_client_id,
            _artifact_manifest_metadata_patch(
                scan_result=scan_result,
                declared_count=len(artifacts_candidates),
            ),
            tuple(item.relative_path for item in scan_result.manifest_items),
        )

    def send_if_available(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        command_key: tuple[str, str],
        cache_entry: _ToolCommandCacheEntry,
        session_state: ConnectionSessionState,
        correlation_id: str | None,
    ) -> _ToolCommandCacheEntry:
        manifest_payload = cache_entry.manifest_payload
        if manifest_payload is None:
            return cache_entry
        if not str(cache_entry.tool_command_runtime_job_id).strip():
            return cache_entry
        if identity.protocol_version != RUNNER_PROTOCOL_DATA_PLANE_VERSION:
            return cache_entry
        manifest_envelope = build_data_plane_artifact_manifest_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            payload=manifest_payload,
            correlation_id=correlation_id,
            runtime_job_id=cache_entry.tool_command_runtime_job_id,
            task_id=cache_entry.task_id,
            protocol_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        )
        websocket.send(manifest_envelope.to_json())
        session_state.pending_upload_contexts[command_key] = _PendingArtifactUploadContext(
            tool_command_runtime_job_id=cache_entry.tool_command_runtime_job_id,
            task_id=cache_entry.task_id,
            manifest_payload=manifest_payload,
            files_by_client_id=cache_entry.files_by_client_id,
            upload_completions_by_object_key=cache_entry.upload_completions_by_object_key,
        )
        return cache_entry
