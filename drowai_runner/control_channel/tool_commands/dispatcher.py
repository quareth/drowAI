"""Tooling-plane tool-command dispatch lifecycle and result draining.

Runs each accepted tooling_plane tool command on a daemon thread (submit/poll through
the operation service), builds the tool-result and artifact-manifest context,
and drains completed/failed dispatch events back onto the websocket while
preserving cache, inflight-replay, and manifest send ordering.

Boundary: this collaborator mutates only the passed ``ConnectionSessionState``
dispatch/cache/inflight fields (and ``pending_upload_contexts`` via the injected
``ArtifactManifestSender``). It performs websocket sends only through the
call-time ``websocket`` object and never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Mapping

from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.protocol_handler import build_tooling_plane_tool_result_envelope
from runtime_shared.runner_protocol import RunnerEnvelope

from drowai_runner.control_channel.artifacts.manifest import ArtifactManifestSender
from drowai_runner.control_channel.constants import (
    TOOL_RESULT_GRACE_SECONDS,
    TOOL_RESULT_POLL_INTERVAL_SECONDS,
)
from drowai_runner.control_channel.helpers import (
    _coerce_positive_float,
    _is_terminal_tool_command_response,
)
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandCacheEntry,
    _ToolCommandDispatchCompleted,
    _ToolCommandDispatchFailed,
)
from drowai_runner.control_channel.tool_commands.result_payload import (
    _build_tooling_plane_tool_result_payload,
)


class ToolCommandDispatcher:
    """Owns tooling_plane tool-command dispatch threads and result draining."""

    def __init__(
        self,
        *,
        operation_service_provider: Callable[[], RunnerOperationService],
        manifest_sender: ArtifactManifestSender,
    ) -> None:
        self._operation_service_provider = operation_service_provider
        self._manifest_sender = manifest_sender

    def start(
        self,
        *,
        inbound: RunnerEnvelope,
        command_key: tuple[str, str],
        mapped_operation: dict[str, object],
        session_state: ConnectionSessionState,
    ) -> None:
        def _dispatch() -> None:
            try:
                response = self.execute(mapped_operation)
                (
                    manifest_payload,
                    files_by_client_id,
                    artifact_metadata_patch,
                    normalized_artifacts,
                ) = self._manifest_sender.build_manifest_context(
                    inbound=inbound,
                    response=response,
                )
                result_payload = _build_tooling_plane_tool_result_payload(
                    inbound=inbound,
                    response=response,
                    artifact_metadata_patch=artifact_metadata_patch,
                    normalized_artifacts=normalized_artifacts,
                )
                cache_entry = _ToolCommandCacheEntry(
                    task_runtime_job_id=command_key[0],
                    command_id=command_key[1],
                    tool_command_runtime_job_id=str(inbound.runtime_job_id or "").strip(),
                    task_id=inbound.task_id,
                    result_payload=result_payload,
                    workspace_id=str(inbound.payload.workspace_id).strip(),
                    tool_call_id=(
                        str(inbound.payload.tool_call_id).strip()
                        if inbound.payload.tool_call_id is not None
                        else None
                    ),
                    tool_batch_id=(
                        str(inbound.payload.tool_batch_id).strip()
                        if inbound.payload.tool_batch_id is not None
                        else None
                    ),
                    manifest_payload=manifest_payload,
                    files_by_client_id=files_by_client_id,
                    upload_completions_by_object_key={},
                )
                session_state.tool_command_dispatch_events.put(
                    _ToolCommandDispatchCompleted(
                        command_key=command_key,
                        cache_entry=cache_entry,
                        correlation_id=inbound.correlation_id,
                    )
                )
            except BaseException as exc:
                session_state.tool_command_dispatch_events.put(
                    _ToolCommandDispatchFailed(error=exc)
                )

        dispatch_thread = threading.Thread(
            target=_dispatch,
            name=f"runner-tool-command-{command_key[1]}",
            daemon=True,
        )
        dispatch_thread.start()

    def execute(self, params: Mapping[str, object]) -> Mapping[str, object]:
        """Run a tool command through the canonical runner submit/poll contract."""

        operation_service = self._operation_service_provider()
        command_id = str(params.get("command_id") or "").strip()
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        timeout_seconds = _coerce_positive_float(params.get("timeout_seconds"), default=30.0)
        poll_params = {
            "runtime_job_id": runtime_job_id,
            "command_id": command_id,
        }
        transport = str(params.get("transport") or "").strip()
        if transport:
            poll_params["transport"] = transport

        submit_response = operation_service.dispatch_operation(
            operation="submit_tool_command",
            params=dict(params),
        )
        if _is_terminal_tool_command_response(submit_response):
            return submit_response

        deadline = time.monotonic() + timeout_seconds + TOOL_RESULT_GRACE_SECONDS
        last_response: Mapping[str, object] = submit_response
        while time.monotonic() <= deadline:
            poll_response = operation_service.dispatch_operation(
                operation="get_tool_command_result",
                params=poll_params,
            )
            last_response = poll_response
            if _is_terminal_tool_command_response(poll_response):
                return poll_response
            time.sleep(TOOL_RESULT_POLL_INTERVAL_SECONDS)

        timeout_message = "Timed out waiting for runner-local tool command result."
        return {
            "accepted": False,
            "status": "timed_out",
            "error_code": "RUNNER_TOOL_RESULT_WAIT_TIMEOUT",
            "error_message": timeout_message,
            "metadata": {
                "runtime_job_id": runtime_job_id,
                "command_id": command_id,
                "exit_code": 124,
                "stdout": "",
                "stderr": timeout_message,
                "artifacts": [],
                "last_status": str(last_response.get("status") or ""),
            },
        }

    def drain(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        session_state: ConnectionSessionState,
        wait_for_inflight: bool = False,
        suppress_dispatch_errors: bool = False,
    ) -> None:
        wait_deadline = time.monotonic() + 1.0
        while True:
            drained_event = False
            while True:
                try:
                    event = session_state.tool_command_dispatch_events.get_nowait()
                except queue.Empty:
                    break
                drained_event = True
                if isinstance(event, _ToolCommandDispatchFailed):
                    if suppress_dispatch_errors:
                        continue
                    raise event.error
                cache_entry = event.cache_entry
                session_state.cached_tool_command_results[event.command_key] = cache_entry
                inflight = session_state.inflight_tool_commands.pop(event.command_key, None)
                cache_entry = self._manifest_sender.send_if_available(
                    websocket=websocket,
                    identity=identity,
                    command_key=event.command_key,
                    cache_entry=cache_entry,
                    session_state=session_state,
                    correlation_id=event.correlation_id,
                )
                session_state.cached_tool_command_results[event.command_key] = cache_entry
                envelope = build_tooling_plane_tool_result_envelope(
                    tenant_id=identity.tenant_id,
                    runner_id=identity.runner_id,
                    payload=cache_entry.result_payload,
                    correlation_id=event.correlation_id,
                    runtime_job_id=cache_entry.tool_command_runtime_job_id,
                    task_id=cache_entry.task_id,
                )
                websocket.send(envelope.to_json())
                if inflight is None:
                    continue
                for replay_correlation_id, replay_task_id in inflight.replay_requests:
                    replay = build_tooling_plane_tool_result_envelope(
                        tenant_id=identity.tenant_id,
                        runner_id=identity.runner_id,
                        payload=cache_entry.result_payload,
                        correlation_id=replay_correlation_id,
                        runtime_job_id=cache_entry.tool_command_runtime_job_id,
                        task_id=replay_task_id,
                    )
                    websocket.send(replay.to_json())
            if not wait_for_inflight:
                return
            if not session_state.inflight_tool_commands:
                return
            if time.monotonic() >= wait_deadline:
                return
            if not drained_event:
                time.sleep(0.01)
