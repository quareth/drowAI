"""Cloud control-channel client for outbound runner connectivity.

This module implements runner-side cloud mode: optional registration, outbound
channel authentication, hello/heartbeat exchange, inbound control-message acks,
and reconnect handling with bounded exponential backoff plus jitter.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable

from drowai_runner.artifact_uploader import RunnerArtifactUploader
from drowai_runner.config import RunnerConfig
from drowai_runner.workspace import RunnerWorkspaceManager

from drowai_runner.control_channel.constants import _DATA_PLANE_ARTIFACT_UPLOAD_MAX_RETRY_ATTEMPTS
from drowai_runner.control_channel.composition import RunnerControlChannelComposition
from drowai_runner.control_channel.heartbeat_reporter import RunnerHeartbeatReporter
from drowai_runner.control_channel.artifacts.manifest import ArtifactManifestSender
from drowai_runner.control_channel.artifacts.promote import ArtifactPromoteHandler
from drowai_runner.control_channel.artifacts.upload import ArtifactUploadHandler
from drowai_runner.control_channel.entrypoint import _docker_client_factory
from drowai_runner.control_channel.identity.environment import (
    _resolve_runner_version,
)
from drowai_runner.control_channel.identity.registration import RunnerRegistrationClient
from drowai_runner.control_channel.identity.resolver import CloudChannelIdentityResolver
from drowai_runner.control_channel.runtime.handler import RemoteRuntimeHandler
from drowai_runner.control_channel.runtime.result_event import RemoteRuntimeResultEventBuilder
from drowai_runner.control_channel.runtime.validation import RemoteRuntimeRequestValidator
from drowai_runner.control_channel.session.pump import ConnectedSessionPump
from drowai_runner.control_channel.terminal.frames import TerminalFrameLifecycle
from drowai_runner.control_channel.terminal.models import (
    _ActiveTerminalSession,
    _TerminalFramePublisher,
)
from drowai_runner.control_channel.terminal.stream import TerminalStreamHandler
from drowai_runner.control_channel.tool_commands.dispatcher import (
    ToolCommandDispatcher,
)
from drowai_runner.control_channel.tool_commands.handler import (
    ToolCommandHandler,
)
from drowai_runner.control_channel.tool_commands.operation_map import (
    _map_tooling_plane_tool_command_operation as _map_tooling_plane_tool_command_operation_fn,
)
from drowai_runner.control_channel.transport.connection import CloudChannelConnector
from drowai_runner.control_channel.transport.endpoint import _join_url_path
from drowai_runner.control_channel.transport.reconnect import (
    compute_reconnect_delay_seconds,
    format_reconnect_error_reason,
)

logger = logging.getLogger(__name__)


class RunnerCloudClient:
    """Outbound cloud channel client for runner cloud mode."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep_fn
        self._random = random_fn or random.random
        self._runner_version = _resolve_runner_version()
        tenant_id = int(config.tenant_id) if config.tenant_id is not None else None
        registration_url = _join_url_path(config.cloud_base_url, "/api/runner-control/register")
        channel_endpoint = _join_url_path(config.cloud_base_url, "/api/runner-control/channel")
        self._registration_client = RunnerRegistrationClient(
            registration_url=registration_url,
            tls_verify=self._config.tls_verify,
        )
        self._identity_resolver = CloudChannelIdentityResolver(
            config=self._config,
            tenant_id=tenant_id,
            runner_version=self._runner_version,
            channel_endpoint=channel_endpoint,
            registration_client=self._registration_client,
        )
        self._terminal_frame_sequences: dict[str, int] = {}
        self._active_terminal_sessions: dict[str, _ActiveTerminalSession] = {}
        self._terminal_frame_publishers: dict[str, _TerminalFramePublisher] = {}
        self._workspace_manager = RunnerWorkspaceManager(self._config.runner_root)
        self._workspace_manager.initialize_runner_root()
        self._composition = RunnerControlChannelComposition(
            config=self._config,
            workspace_manager=self._workspace_manager,
            docker_client_factory=_docker_client_factory,
        )
        self._heartbeat_reporter = RunnerHeartbeatReporter(
            config=self._config,
            runner_version=self._runner_version,
            job_store_provider=self._composition.job_store,
        )
        self._artifact_uploader = RunnerArtifactUploader(
            max_attempts=_DATA_PLANE_ARTIFACT_UPLOAD_MAX_RETRY_ATTEMPTS
        )
        self._artifact_manifest_sender = ArtifactManifestSender(
            workspace_manager=self._workspace_manager,
        )
        self._terminal_frame_lifecycle = TerminalFrameLifecycle(
            active_terminal_sessions=self._active_terminal_sessions,
            terminal_frame_sequences=self._terminal_frame_sequences,
            terminal_frame_publishers=self._terminal_frame_publishers,
            operation_service_provider=self._composition.operation_service,
        )
        self._terminal_stream_handler = TerminalStreamHandler(
            operation_service_provider=self._composition.operation_service,
            frame_lifecycle=self._terminal_frame_lifecycle,
        )
        self._remote_runtime_validator = RemoteRuntimeRequestValidator(
            job_store_provider=self._composition.job_store,
        )
        self._remote_runtime_result_event_builder = RemoteRuntimeResultEventBuilder(
            frame_lifecycle=self._terminal_frame_lifecycle,
        )
        self._remote_runtime_handler = RemoteRuntimeHandler(
            validator=self._remote_runtime_validator,
            result_event_builder=self._remote_runtime_result_event_builder,
            terminal_stream_handler=self._terminal_stream_handler,
            frame_lifecycle=self._terminal_frame_lifecycle,
            operation_service_provider=self._composition.operation_service,
            active_terminal_sessions=self._active_terminal_sessions,
        )
        self._tool_command_dispatcher = ToolCommandDispatcher(
            operation_service_provider=self._composition.operation_service,
            manifest_sender=self._artifact_manifest_sender,
        )
        self._tool_command_handler = ToolCommandHandler(
            operation_mapper=_map_tooling_plane_tool_command_operation_fn,
            task_runtime_binding_lookup=self._remote_runtime_validator.lookup_task_runtime_binding,
            dispatcher=self._tool_command_dispatcher,
        )
        self._artifact_promote_handler = ArtifactPromoteHandler(
            validate_runtime_request=self._remote_runtime_validator.validate,
            operation_service_provider=self._composition.operation_service,
            workspace_manager=self._workspace_manager,
            manifest_sender=self._artifact_manifest_sender,
        )
        self._artifact_upload_handler = ArtifactUploadHandler(
            artifact_uploader=self._artifact_uploader,
        )
        self._connector = CloudChannelConnector(config=self._config)
        self._session_pump = ConnectedSessionPump(
            connect=self._connector.connect,
            config=self._config,
            runner_version=self._runner_version,
            send_heartbeat=self._heartbeat_reporter.send_heartbeat,
            drain_tool_dispatch=self._tool_command_dispatcher.drain,
            reset_terminal_state=self._terminal_frame_lifecycle.reset_session_state,
            close_active_terminal_sessions=self._terminal_frame_lifecycle.close_active_sessions,
            handle_artifact_promote=self._artifact_promote_handler.handle_promote,
            is_remote_runtime_request=self._remote_runtime_handler.is_request,
            handle_remote_runtime=self._remote_runtime_handler.handle,
            handle_tool_command=self._tool_command_handler.handle,
            handle_artifact_upload=self._artifact_upload_handler.handle_upload,
            task_runtime_binding_lookup=self._remote_runtime_validator.lookup_task_runtime_binding,
        )

    def run_forever(self) -> int:
        """Run cloud client loop until interrupted."""
        identity = self._identity_resolver.resolve()
        reconnect_attempt = 0
        while True:
            try:
                self._session_pump.run(identity)
                reconnect_attempt = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                reconnect_attempt += 1
                delay = compute_reconnect_delay_seconds(
                    attempt=reconnect_attempt,
                    random_fraction=self._random(),
                )
                logger.warning(
                    "runner.cloud.reconnect_scheduled attempt=%s delay_seconds=%.3f runner_id=%s tenant_id=%s error=%s reason=%s",
                    reconnect_attempt,
                    delay,
                    identity.runner_id,
                    identity.tenant_id,
                    type(exc).__name__,
                    format_reconnect_error_reason(exc),
                )
                self._sleep(delay)
