"""Data-plane artifact upload request handling — ACK classification, binding validation,
upload execution, cache metadata patches, follow-up tool-result emission, and
upload-complete envelope.

Mutates only the passed ``ConnectionSessionState`` ACK/cache/pending-upload fields
(via context mutation). Must not import ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from dataclasses import replace

from drowai_runner.artifact_uploader import RunnerArtifactUploader
from drowai_runner.protocol_handler import (
    build_runner_ack_envelope,
    build_tooling_plane_tool_result_envelope,
    build_data_plane_artifact_upload_complete_envelope,
    validate_data_plane_artifact_upload_request_binding,
)
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactUploadCompletePayload,
    RunnerArtifactUploadRequestPayload,
    RunnerEnvelope,
    RunnerProtocolValidationError,
)

from drowai_runner.control_channel.artifacts.metadata import (
    _artifact_promotion_metadata_patch as _artifact_promotion_metadata_patch_fn,
    _artifact_upload_metadata_patch as _artifact_upload_metadata_patch_fn,
    _tool_result_with_metadata_patch as _tool_result_with_metadata_patch_fn,
)
from drowai_runner.control_channel.artifacts.models import _PendingArtifactUploadContext
from drowai_runner.control_channel.constants import _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT
from drowai_runner.control_channel.helpers import _merge_json_dicts
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState


class ArtifactUploadHandler:
    """Handles inbound data_plane artifact upload requests."""

    def __init__(self, *, artifact_uploader: RunnerArtifactUploader) -> None:
        self._artifact_uploader = artifact_uploader

    def handle_upload(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
        session_state: ConnectionSessionState,
    ) -> None:
        normalized_message_id = str(inbound.message_id).strip()
        cached_decision = session_state.ack_decisions_by_message_id.get(normalized_message_id)
        payload: RunnerArtifactUploadRequestPayload | None = None
        context: _PendingArtifactUploadContext | None = None
        if cached_decision is None:
            try:
                payload = validate_data_plane_artifact_upload_request_binding(
                    inbound,
                    expected_tenant_id=identity.tenant_id,
                    expected_runner_id=identity.runner_id,
                )
                command_key = (
                    str(payload.task_runtime_job_id).strip(),
                    str(payload.command_id).strip(),
                )
                context = session_state.pending_upload_contexts.get(command_key)
                if context is None:
                    cached_decision = ("rejected", _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT)
                elif context.tool_command_runtime_job_id != str(inbound.runtime_job_id or "").strip():
                    cached_decision = ("rejected", _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT)
                elif context.task_id is not None and inbound.task_id != context.task_id:
                    cached_decision = ("rejected", _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT)
                elif (
                    str(context.manifest_payload.workspace_id).strip()
                    != str(payload.workspace_id).strip()
                ):
                    cached_decision = ("rejected", _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT)
                else:
                    cached_decision = ("accepted", None)
            except RunnerProtocolValidationError:
                cached_decision = ("rejected", _DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT)
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
        if status != "accepted":
            return
        if payload is None:
            payload = validate_data_plane_artifact_upload_request_binding(
                inbound,
                expected_tenant_id=identity.tenant_id,
                expected_runner_id=identity.runner_id,
            )
        command_key = (
            str(payload.task_runtime_job_id).strip(),
            str(payload.command_id).strip(),
        )
        if context is None:
            context = session_state.pending_upload_contexts.get(command_key)
        if context is None:
            return
        upload_result = self._artifact_uploader.upload(
            uploads=payload.uploads,
            files_by_client_id=context.files_by_client_id,
            uploaded_by_object_key=context.upload_completions_by_object_key,
        )
        for completion in upload_result.completed:
            context.upload_completions_by_object_key[completion.object_key] = completion
        cache_entry = session_state.cached_tool_command_results.get(command_key)
        if cache_entry is not None:
            metadata_patch = _artifact_upload_metadata_patch_fn(
                requested_uploads=payload.uploads,
                upload_result=upload_result,
            )
            metadata_patch = _merge_json_dicts(
                metadata_patch,
                _artifact_promotion_metadata_patch_fn(
                    requested_uploads=payload.uploads,
                    manifest_payload=context.manifest_payload,
                    upload_result=upload_result,
                ),
            )
            cache_entry = replace(
                cache_entry,
                result_payload=_tool_result_with_metadata_patch_fn(
                    payload=cache_entry.result_payload,
                    metadata_patch=metadata_patch,
                ),
            )
            session_state.cached_tool_command_results[command_key] = cache_entry
            promoted_ids = metadata_patch.get("promoted_artifact_ids")
            has_promoted_ids = isinstance(promoted_ids, list) and any(str(item or "").strip() for item in promoted_ids)
            if int(metadata_patch.get("artifact_upload", {}).get("unpromoted_count", 0)) > 0 or has_promoted_ids:
                follow_up_result = build_tooling_plane_tool_result_envelope(
                    tenant_id=identity.tenant_id,
                    runner_id=identity.runner_id,
                    payload=cache_entry.result_payload,
                    correlation_id=inbound.correlation_id,
                    runtime_job_id=context.tool_command_runtime_job_id,
                    task_id=context.task_id,
                )
                websocket.send(follow_up_result.to_json())
        if not upload_result.completed:
            return
        complete_payload = RunnerArtifactUploadCompletePayload(
            task_runtime_job_id=payload.task_runtime_job_id,
            command_id=payload.command_id,
            workspace_id=payload.workspace_id,
            tool_call_id=payload.tool_call_id,
            tool_batch_id=payload.tool_batch_id,
            uploads=upload_result.completed,
        )
        complete_envelope = build_data_plane_artifact_upload_complete_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            payload=complete_payload,
            correlation_id=inbound.correlation_id,
            runtime_job_id=context.tool_command_runtime_job_id,
            task_id=context.task_id,
            protocol_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        )
        websocket.send(complete_envelope.to_json())
