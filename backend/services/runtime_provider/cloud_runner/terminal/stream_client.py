"""Terminal stream attachment for cloud runner runtime operations.

This module owns runner terminal-stream capability detection, stream-client
attachment to successful terminal-open results, and live-channel checks. It
does not own terminal result waiting or public terminal operation bodies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner
from backend.services.runner_control.terminal_stream_registry import (
    CloudTerminalStreamClient,
    TERMINAL_STREAM_CAPABILITY,
    get_runner_terminal_stream_registry,
)

from ..constants import _DEFAULT_RUNTIME_IMAGE
from ..error_codes import _RUNNER_TERMINAL_STREAM_UNAVAILABLE
from ..normalization import (
    _normalize_optional_uuid,
    _normalize_tenant_id,
    _resolve_optional_text,
)
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)


class CloudRunnerTerminalStreamAttacher:
    """Attaches non-durable stream clients to terminal-open results."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._provider_name = provider_name

    def _attach_terminal_stream_client(
        self,
        *,
        request: RuntimeOperationRequest,
        result: RuntimeOperationResult,
    ) -> RuntimeOperationResult:
        """Attach the non-durable cloud stream client when the runner supports it."""
        if not result.ok or not result.accepted:
            return result
        runner_id = _normalize_optional_uuid(request.runner_id)
        if runner_id is None:
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but runner identity is missing.",
            )
        if not self._runner_supports_terminal_stream(
            tenant_id=_normalize_tenant_id(request.tenant_id),
            runner_id=runner_id,
        ):
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but runner does not advertise terminal_stream_v1.",
            )
        stream_registry = get_runner_terminal_stream_registry()
        tenant_id = _normalize_tenant_id(request.tenant_id)
        if not stream_registry.has_channel(tenant_id=tenant_id, runner_id=runner_id):
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but runner channel is not connected.",
            )

        metadata = dict(result.metadata)
        delegate = metadata.get("delegate_result")
        if not isinstance(delegate, Mapping):
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but terminal open did not return delegate metadata.",
            )
        session_id = _resolve_optional_text(delegate.get("session_id"))
        if session_id is None:
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but terminal open did not return a session id.",
            )
        runtime_job_id = _resolve_optional_text(delegate.get("runtime_job_id")) or _resolve_optional_text(
            metadata.get("runner_runtime_job_id") or metadata.get("runtime_job_id")
        )
        if runtime_job_id is None:
            return self._terminal_stream_unavailable_result(
                request=request,
                message="Terminal stream is required but terminal open did not return a runtime job id.",
            )

        stream_registry.register_stream(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=request.task_id,
            session_id=session_id,
        )
        stream_client = CloudTerminalStreamClient(
            registry=stream_registry,
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=request.task_id,
            session_id=session_id,
            runtime_job_id=runtime_job_id,
            workspace_id=request.workspace_id,
            runtime_image=str(metadata.get("runtime_image") or _DEFAULT_RUNTIME_IMAGE),
        )
        merged_delegate = dict(delegate)
        merged_delegate["socket"] = stream_client
        merged_delegate["exec_id"] = session_id
        merged_delegate["stream_mode"] = True
        metadata["delegate_result"] = merged_delegate
        metadata["stream_mode"] = True
        return build_runtime_result(
            request,
            accepted=result.accepted,
            provider=self._provider_name,
            status=result.status,
            error_code=result.error_code,
            error_message=result.error_message,
            metadata=metadata,
        )

    def _terminal_stream_unavailable_result(
        self,
        *,
        request: RuntimeOperationRequest,
        message: str,
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_TERMINAL_STREAM_UNAVAILABLE,
            error_message=message,
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": "open_terminal_session",
            },
        )

    def _runner_supports_terminal_stream(self, *, tenant_id: int, runner_id: UUID) -> bool:
        """Return whether the runner advertised the MVP stream terminal capability."""
        with self._session_factory() as db:
            runner = db.execute(
                select(Runner).where(
                    Runner.tenant_id == int(tenant_id),
                    Runner.id == runner_id,
                )
            ).scalar_one_or_none()
            if runner is None:
                return False
            capabilities = runner.capabilities_json
            if isinstance(capabilities, Mapping):
                return TERMINAL_STREAM_CAPABILITY in {str(key).strip() for key in capabilities}
            if isinstance(capabilities, list):
                return TERMINAL_STREAM_CAPABILITY in {str(item).strip() for item in capabilities}
            return False

    @staticmethod
    def _stream_client_channel_connected(stream_client: object) -> bool:
        """Return whether a push stream client still has its live runner channel."""
        checker = getattr(stream_client, "channel_connected", None)
        if not callable(checker):
            return True
        return bool(checker())
