"""Pure artifact metadata patch helpers for the runner control channel.

Builds artifact upload, promotion, manifest, and tool-result metadata patches as
pure functions over protocol DTOs. No websocket, job-store, workspace, or upload
I/O; no connection-session state. Never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.artifact_manifest import ArtifactManifestScanResult
from drowai_runner.control_channel.helpers import _merge_json_dicts
from runtime_shared.runner_protocol import (
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadRequestItem,
    RunnerToolResultPayload,
    sanitize_tool_result_payload_for_transport,
)


def _artifact_upload_metadata_patch(
    *,
    requested_uploads: tuple[RunnerArtifactUploadRequestItem, ...],
    upload_result,
) -> Mapping[str, object]:
    completed_by_object_key = {str(item.object_key): item for item in upload_result.completed}
    unpromoted = []
    for requested in requested_uploads:
        if str(requested.object_key) in completed_by_object_key:
            continue
        unpromoted.append(
            {
                "artifact_id": str(requested.artifact_id),
                "artifact_client_id": str(requested.artifact_client_id),
                "object_key": str(requested.object_key),
            }
        )

    if upload_result.failures:
        status = "failed"
    elif unpromoted:
        status = "partial"
    else:
        status = "promoted"

    return {
        "artifact_upload": {
            "status": status,
            "requested_count": len(requested_uploads),
            "completed_count": len(upload_result.completed),
            "failed_count": len(upload_result.failures),
            "unpromoted_count": len(unpromoted),
            "failed_artifacts": upload_result.failures_json(),
            "unpromoted_artifacts": unpromoted,
        }
    }


def _artifact_promotion_metadata_patch(
    *,
    requested_uploads: tuple[RunnerArtifactUploadRequestItem, ...],
    manifest_payload: RunnerArtifactManifestPayload,
    upload_result,
) -> Mapping[str, object]:
    requested_by_artifact_id = {
        str(item.artifact_id): item for item in requested_uploads if str(item.artifact_id).strip()
    }
    relative_path_by_client_id = {
        str(item.artifact_client_id): str(item.relative_path)
        for item in manifest_payload.artifacts
        if str(item.artifact_client_id).strip() and str(item.relative_path).strip()
    }
    promoted_artifact_ids: list[str] = []
    artifact_refs: list[dict[str, str]] = []
    for completion in upload_result.completed:
        artifact_id = str(completion.artifact_id).strip()
        if not artifact_id:
            continue
        promoted_artifact_ids.append(artifact_id)
        ref: dict[str, str] = {"artifact_id": artifact_id}
        requested_item = requested_by_artifact_id.get(artifact_id)
        if requested_item is not None:
            artifact_client_id = str(requested_item.artifact_client_id).strip()
            if artifact_client_id:
                ref["artifact_client_id"] = artifact_client_id
                relative_path = relative_path_by_client_id.get(artifact_client_id)
                if relative_path:
                    ref["relative_path"] = relative_path
        artifact_refs.append(ref)

    upload_status = str(
        _artifact_upload_metadata_patch(
            requested_uploads=requested_uploads,
            upload_result=upload_result,
        )
        .get("artifact_upload", {})
        .get("status", "")
    ).strip()
    if upload_status == "promoted":
        artifact_promotion_status = "ready"
    elif upload_status == "partial":
        artifact_promotion_status = "upload_pending"
    else:
        artifact_promotion_status = "upload_failed"

    return {
        "artifact_scope": "cloud_data_plane",
        "artifact_visibility": "artifact_catalog",
        "artifact_promotion_status": artifact_promotion_status,
        "artifact_promotion": {"status": artifact_promotion_status},
        "promoted_artifact_ids": promoted_artifact_ids,
        "artifact_refs": artifact_refs,
    }


def _tool_result_with_metadata_patch(
    *,
    payload: RunnerToolResultPayload,
    metadata_patch: Mapping[str, object],
) -> RunnerToolResultPayload:
    merged_metadata = _merge_json_dicts(payload.metadata, metadata_patch)
    transport_payload = sanitize_tool_result_payload_for_transport(
        {
            "operation_id": payload.operation_id,
            "command_id": payload.command_id,
            "tool": payload.tool,
            "status": payload.status,
            "success": payload.success,
            "exit_code": payload.exit_code,
            "stdout": payload.stdout,
            "stderr": payload.stderr,
            "artifacts": tuple(payload.artifacts),
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "result": dict(payload.result),
            "metadata": merged_metadata,
        }
    )
    normalized_metadata = transport_payload.get("metadata")
    return RunnerToolResultPayload(
        operation_id=str(transport_payload.get("operation_id") or payload.operation_id),
        command_id=str(transport_payload.get("command_id") or payload.command_id),
        tool=str(transport_payload.get("tool") or payload.tool),
        status=str(transport_payload.get("status") or payload.status),
        success=bool(transport_payload.get("success") if "success" in transport_payload else payload.success),
        exit_code=int(transport_payload.get("exit_code") or payload.exit_code),
        stdout=str(transport_payload.get("stdout") or payload.stdout),
        stderr=str(transport_payload.get("stderr") or payload.stderr),
        artifacts=tuple(str(item) for item in payload.artifacts),
        error_code=(
            str(transport_payload.get("error_code")).strip() or None
            if transport_payload.get("error_code") is not None
            else None
        ),
        error_message=(
            str(transport_payload.get("error_message")).strip() or None
            if transport_payload.get("error_message") is not None
            else None
        ),
        result=dict(payload.result),
        metadata=dict(normalized_metadata) if isinstance(normalized_metadata, Mapping) else {},
    )


def _artifact_manifest_metadata_patch(
    *,
    scan_result: ArtifactManifestScanResult,
    declared_count: int,
) -> Mapping[str, object]:
    status = "ready_for_upload_request" if scan_result.manifest_items else "no_uploadable_artifacts"
    return {
        "artifact_manifest": {
            "status": status,
            "declared_count": declared_count,
            "accepted_count": len(scan_result.manifest_items),
            "skipped_count": scan_result.skipped_count,
            "warnings": scan_result.warnings_json(),
            "warnings_truncated_count": scan_result.warnings_truncated_count,
        }
    }
