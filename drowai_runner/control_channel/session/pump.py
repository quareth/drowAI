"""Connected-session pump for the cloud control channel websocket loop.

Owns the connected-session orchestration: hello setup, heartbeat cadence,
tool-dispatch drain timing, the inbound parse loop, top-level domain message
routing, and the ``finally`` terminal-session cleanup.

Boundary: the pump receives every collaborator explicitly (the websocket
connector, runner config/version, the heartbeat sender, the tool-dispatch drain,
the terminal-session reset/close callbacks, the domain handler callables, and the
task/runtime binding lookup). Domain handler bodies remain in their collaborators;
this module only routes to them. It owns only the websocket-loop locals and one
``ConnectionSessionState`` per connected session, and must not import
``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed

from drowai_runner.config import RunnerConfig
from drowai_runner.protocol_handler import (
    RunnerTaskRuntimeBinding,
    build_runner_hello_envelope,
    parse_inbound_envelope,
)
from runtime_shared.runner_protocol import (
    RunnerEnvelope,
    RunnerMessageType,
    RunnerProtocolValidationError,
)

from drowai_runner.control_channel.constants import DEFAULT_RECV_TIMEOUT_SECONDS
from drowai_runner.control_channel.helpers import _stream_capabilities
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.ack import (
    handle_default_inbound_ack,
    send_tool_command_parse_error_ack,
)
from drowai_runner.control_channel.session.state import ConnectionSessionState


class ConnectedSessionPump:
    """Drive one connected cloud channel websocket session end to end."""

    def __init__(
        self,
        *,
        connect: Callable[[CloudChannelIdentity], AbstractContextManager[Any]],
        config: RunnerConfig,
        runner_version: str,
        send_heartbeat: Callable[..., None],
        drain_tool_dispatch: Callable[..., None],
        reset_terminal_state: Callable[[], None],
        close_active_terminal_sessions: Callable[[], None],
        handle_artifact_promote: Callable[..., None],
        is_remote_runtime_request: Callable[[RunnerEnvelope], bool],
        handle_remote_runtime: Callable[..., None],
        handle_tool_command: Callable[..., None],
        handle_artifact_upload: Callable[..., None],
        task_runtime_binding_lookup: Callable[[str], RunnerTaskRuntimeBinding | None],
    ) -> None:
        self._connect = connect
        self._config = config
        self._runner_version = runner_version
        self._send_heartbeat = send_heartbeat
        self._drain_tool_dispatch = drain_tool_dispatch
        self._reset_terminal_state = reset_terminal_state
        self._close_active_terminal_sessions = close_active_terminal_sessions
        self._handle_artifact_promote = handle_artifact_promote
        self._is_remote_runtime_request = is_remote_runtime_request
        self._handle_remote_runtime = handle_remote_runtime
        self._handle_tool_command = handle_tool_command
        self._handle_artifact_upload = handle_artifact_upload
        self._task_runtime_binding_lookup = task_runtime_binding_lookup

    def run(self, identity: CloudChannelIdentity) -> None:
        with self._connect(identity) as websocket:
            capabilities = _stream_capabilities(self._config.capabilities)
            hello = build_runner_hello_envelope(
                tenant_id=identity.tenant_id,
                runner_id=identity.runner_id,
                runner_version=self._runner_version,
                labels=dict(self._config.labels or {}),
                capabilities=capabilities,
                protocol_version=identity.protocol_version,
            )
            websocket.send(hello.to_json())

            self._reset_terminal_state()
            self._send_heartbeat(websocket=websocket, identity=identity)
            last_heartbeat_at = datetime.now(tz=UTC)
            heartbeat_every_seconds = max(1, int(identity.heartbeat_interval_seconds))
            session_state = ConnectionSessionState()
            try:
                while True:
                    self._drain_tool_dispatch(
                        websocket=websocket,
                        identity=identity,
                        session_state=session_state,
                    )
                    now = datetime.now(tz=UTC)
                    if (now - last_heartbeat_at).total_seconds() >= heartbeat_every_seconds:
                        self._send_heartbeat(websocket=websocket, identity=identity)
                        last_heartbeat_at = now
                    try:
                        raw_message = websocket.recv(timeout=DEFAULT_RECV_TIMEOUT_SECONDS)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        break
                    except KeyboardInterrupt:
                        self._drain_tool_dispatch(
                            websocket=websocket,
                            identity=identity,
                            session_state=session_state,
                            wait_for_inflight=True,
                            suppress_dispatch_errors=True,
                        )
                        raise

                    if raw_message is None:
                        continue
                    try:
                        inbound = parse_inbound_envelope(raw_message)
                    except RunnerProtocolValidationError:
                        send_tool_command_parse_error_ack(
                            websocket=websocket,
                            identity=identity,
                            raw_message=raw_message,
                        )
                        continue

                    normalized_message_id = str(inbound.message_id).strip()
                    if not normalized_message_id:
                        continue

                    if inbound.message_type is RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE:
                        self._handle_artifact_promote(
                            websocket=websocket,
                            identity=identity,
                            inbound=inbound,
                            session_state=session_state,
                        )
                        continue

                    if self._is_remote_runtime_request(inbound):
                        self._handle_remote_runtime(
                            websocket=websocket,
                            identity=identity,
                            inbound=inbound,
                            session_state=session_state,
                        )
                        continue

                    if inbound.message_type is RunnerMessageType.TOOL_COMMAND:
                        self._handle_tool_command(
                            websocket=websocket,
                            identity=identity,
                            inbound=inbound,
                            session_state=session_state,
                        )
                        continue

                    if inbound.message_type is RunnerMessageType.ARTIFACT_UPLOAD_REQUEST:
                        self._handle_artifact_upload(
                            websocket=websocket,
                            identity=identity,
                            inbound=inbound,
                            session_state=session_state,
                        )
                        continue

                    handle_default_inbound_ack(
                        websocket=websocket,
                        identity=identity,
                        inbound=inbound,
                        normalized_message_id=normalized_message_id,
                        session_state=session_state,
                        task_runtime_binding_lookup=self._task_runtime_binding_lookup,
                    )
            finally:
                self._close_active_terminal_sessions()
