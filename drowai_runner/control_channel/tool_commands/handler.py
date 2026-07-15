"""Tooling-plane tool-command request handling, ACK, and replay decisions.

Classifies inbound tooling_plane tool-command envelopes, sends the accepted/rejected
ACK, replays cached results, registers inflight entries, and starts background
dispatch through the injected tooling-plane command dispatcher.

Boundary: this collaborator mutates only the passed ``ConnectionSessionState``
ACK-decision/cache/inflight fields. It performs websocket sends only through the
call-time ``websocket`` object, resolves runtime bindings through the injected
lookup callback, and never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Callable

from drowai_runner.protocol_handler import (
    RunnerTaskRuntimeBinding,
    RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
    build_runner_ack_envelope,
    build_tooling_plane_tool_result_envelope,
    classify_runner_control_inbound_ack,
    validate_tooling_plane_tool_command_binding,
)
from runtime_shared.runner_protocol import RunnerEnvelope

from drowai_runner.control_channel.constants import (
    _TOOLING_PLANE_TOOL_COMMAND_BINDING_CONFLICT,
)
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.tool_commands.dispatcher import (
    ToolCommandDispatcher,
)
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandInflightEntry,
)


class ToolCommandHandler:
    """Handles inbound tooling_plane tool-command requests and dispatch hand-off."""

    def __init__(
        self,
        *,
        operation_mapper: Callable[..., dict[str, object]],
        task_runtime_binding_lookup: Callable[[str], RunnerTaskRuntimeBinding | None],
        dispatcher: ToolCommandDispatcher,
    ) -> None:
        self._operation_mapper = operation_mapper
        self._task_runtime_binding_lookup = task_runtime_binding_lookup
        self._dispatcher = dispatcher

    def handle(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
        session_state: ConnectionSessionState,
    ) -> None:
        normalized_message_id = str(inbound.message_id).strip()
        cached_decision = session_state.ack_decisions_by_message_id.get(normalized_message_id)
        payload = inbound.payload
        command_key: tuple[str, str] | None = None
        runtime_job_id = str(inbound.runtime_job_id or "").strip()
        if cached_decision is None:
            if not hasattr(payload, "task_runtime_job_id") or not hasattr(payload, "command_id"):
                cached_decision = ("rejected", RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE)
            else:
                command_key = (
                    str(payload.task_runtime_job_id).strip(),
                    str(payload.command_id).strip(),
                )
                cached_result = session_state.cached_tool_command_results.get(command_key)
                inflight = session_state.inflight_tool_commands.get(command_key)
                if (
                    cached_result is not None
                    and cached_result.tool_command_runtime_job_id != runtime_job_id
                ):
                    cached_decision = ("rejected", _TOOLING_PLANE_TOOL_COMMAND_BINDING_CONFLICT)
                elif (
                    inflight is not None
                    and inflight.tool_command_runtime_job_id != runtime_job_id
                ):
                    cached_decision = ("rejected", _TOOLING_PLANE_TOOL_COMMAND_BINDING_CONFLICT)
                else:
                    decision = classify_runner_control_inbound_ack(
                        inbound,
                        expected_tenant_id=identity.tenant_id,
                        expected_runner_id=identity.runner_id,
                        task_runtime_binding_lookup=self._task_runtime_binding_lookup,
                    )
                    if not decision.should_ack:
                        return
                    status = str(decision.status or "accepted").strip() or "accepted"
                    error_code = (
                        str(decision.error_code).strip()
                        if decision.error_code is not None and str(decision.error_code).strip()
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
        if status != "accepted":
            return

        if not hasattr(payload, "task_runtime_job_id") or not hasattr(payload, "command_id"):
            return
        command_key = command_key or (
            str(payload.task_runtime_job_id).strip(),
            str(payload.command_id).strip(),
        )
        cached_result = session_state.cached_tool_command_results.get(command_key)
        if cached_result is not None:
            replay = build_tooling_plane_tool_result_envelope(
                tenant_id=identity.tenant_id,
                runner_id=identity.runner_id,
                payload=cached_result.result_payload,
                correlation_id=inbound.correlation_id,
                runtime_job_id=cached_result.tool_command_runtime_job_id,
                task_id=cached_result.task_id,
            )
            websocket.send(replay.to_json())
            return
        inflight = session_state.inflight_tool_commands.get(command_key)
        if inflight is not None:
            inflight.replay_requests.append((inbound.correlation_id, inbound.task_id))
            return

        binding = validate_tooling_plane_tool_command_binding(
            inbound,
            expected_tenant_id=identity.tenant_id,
            expected_runner_id=identity.runner_id,
            task_runtime_binding_lookup=self._task_runtime_binding_lookup,
        )
        mapped_operation = self._operation_mapper(inbound=inbound, binding=binding)
        session_state.inflight_tool_commands[command_key] = _ToolCommandInflightEntry(
            tool_command_runtime_job_id=runtime_job_id,
            task_id=inbound.task_id,
            replay_requests=[],
        )
        self._dispatcher.start(
            inbound=inbound,
            command_key=command_key,
            mapped_operation=mapped_operation,
            session_state=session_state,
        )
