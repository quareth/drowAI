"""Runner-control management API router for tenant-scoped cloud runner records.

Scope:
- Exposes authenticated execution-site, install-token, runner-read, and
  credential-revocation endpoints for runner control management plane operations.

Boundaries:
- Keeps handlers thin and delegates orchestration to runner-control services.
- Applies centralized tenant authorization before runner management operations.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from collections import OrderedDict, deque
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Lock
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketException, status
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect
from starlette.requests import HTTPConnection
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import SessionLocal, get_db
from backend.models.core import Task
from backend.models.runner_control import ExecutionSite, RuntimeJob
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService
from backend.services.runner_control.channel.auth import (
    RunnerChannelAuthContext,
    RunnerChannelAuthError,
    RunnerChannelAuthService,
)
from backend.services.runner_control.channel.types import (
    RunnerAckObservation,
    RunnerChannelSession,
)
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.dispatcher import DispatchAttemptResult
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.registration_service import (
    RunnerRegistrationError,
    RunnerRegistrationRequest as RunnerRegistrationServiceRequest,
    RunnerRegistrationService,
)
from backend.services.runner_control.readiness_service import RunnerReadinessService
from backend.services.runner_control.registry_service import RunnerRegistryError, RunnerRegistryService
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobCreateRequest,
    RuntimeJobService,
    RuntimeJobServiceError,
)
from backend.services.runner_control.runtime_event_service import (
    bind_runtime_event_publish_loop,
    reset_runtime_event_publish_loop,
)
from backend.services.runner_control.terminal_stream_registry import get_runner_terminal_stream_registry
from backend.services.runner_control.schemas import (
    ExecutionSiteCreateRequest,
    ExecutionSiteResponse,
    InstallTokenCreateRequest,
    InstallTokenCreateResponse,
    ManagementUrlResponse,
    ManagementUrlUpdateRequest,
    RunnerEnrollmentCreateRequest,
    RunnerEnrollmentCreateResponse,
    RunnerRegistrationRequest,
    RunnerRegistrationResponse,
    RunnerReadinessResponse,
    RunnerSiteResponse,
    RunnerCredentialSummaryResponse,
    RunnerDetailResponse,
    RunnerListItemResponse,
    RunnerRevokeResponse,
    RuntimeJobResponse,
    TaskRunnerAssignmentRequest,
    TaskRunnerAssignmentResponse,
)
from backend.services.platform.management_url import ManagementUrlError, ManagementUrlService, normalize_management_url
from backend.services.platform.generated_artifacts import build_runner_enrollment_toml
from backend.services.task.access_service import get_task_in_tenant_or_404
from backend.services.tenant.authorization import ACTION_RUNNER_MANAGE, decide_action
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType, parse_runner_envelope_json

router = APIRouter(prefix="/api/runner-control", tags=["runner-control"])
logger = logging.getLogger(__name__)
_CHANNEL_AUTH_FAILED_DETAIL = "Runner channel authentication failed."
_RUNNER_AUTH_FAILURE_LOG_BURST = 3
_RUNNER_AUTH_FAILURE_LOG_WINDOW_SECONDS = 60.0
_RUNNER_AUTH_FAILURE_LOG_MAX_KEYS = 2048
_RUNNER_OUTBOUND_MAX_MESSAGES_PER_POLL = 25
_RUNNER_CHANNEL_RECEIVE_TIMEOUT_SECONDS = 0.25
_ASSIGNMENT_RUNTIME_JOB_TYPE = "runner_control.runtime.assignment_probe"
_ASSIGNMENT_PROBE_MESSAGE_TYPE = "runner.assignment.probe"
_NO_ELIGIBLE_RUNNER_ERROR_CODE = "NO_ELIGIBLE_RUNNER"
_ASSIGNMENT_ROUTER_POD_ID = "runner-control-router"
_VPN_POST_COMMIT_TASKS: set[asyncio.Task[None]] = set()
_VPN_POST_COMMIT_TASK_KEYS: set[int] = set()


def _release_vpn_post_commit_task(completed: asyncio.Task[None], *, task_id: int) -> None:
    """Release in-flight reconciliation bookkeeping for one task."""
    _VPN_POST_COMMIT_TASKS.discard(completed)
    _VPN_POST_COMMIT_TASK_KEYS.discard(task_id)
_RUNNER_CHANNEL_CLOSE_EVENTS: dict[int, str] = {
    status.WS_1000_NORMAL_CLOSURE: "RUNNER_CHANNEL_CLOSED_NORMAL",
    status.WS_1001_GOING_AWAY: "RUNNER_CHANNEL_CLOSED_GOING_AWAY",
    status.WS_1002_PROTOCOL_ERROR: "RUNNER_CHANNEL_CLOSED_PROTOCOL_ERROR",
    status.WS_1003_UNSUPPORTED_DATA: "RUNNER_CHANNEL_CLOSED_UNSUPPORTED_DATA",
    status.WS_1008_POLICY_VIOLATION: "RUNNER_CHANNEL_CLOSED_POLICY_VIOLATION",
    status.WS_1011_INTERNAL_ERROR: "RUNNER_CHANNEL_CLOSED_INTERNAL_ERROR",
}
_PACKAGE_LOG_SECRET_PATTERNS = (
    (re.compile(r"\brit_[A-Za-z0-9_-]+"), "<MASKED_INSTALL_TOKEN>"),
    (re.compile(r"\brsec_[A-Za-z0-9_-]+"), "<MASKED_RUNNER_SECRET>"),
)


def _enforce_runner_management_action(*, role: str) -> None:
    """Enforce runner management through the shared tenant authorization policy."""

    decision = decide_action(role=role, action=ACTION_RUNNER_MANAGE)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runner management requires tenant runner.manage permission.",
        )


class _AuthFailureLogRateLimiter:
    """Bounded in-memory limiter for repeated auth-failure warning logs."""

    def __init__(self, *, max_events: int, window_seconds: float, max_keys: int) -> None:
        self._max_events = max(1, int(max_events))
        self._window_seconds = max(1.0, float(window_seconds))
        self._max_keys = max(1, int(max_keys))
        self._events_by_key: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def should_log(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events_by_key.get(key)
            if events is None:
                events = deque()
                self._events_by_key[key] = events
            else:
                self._events_by_key.move_to_end(key)

            while events and now - events[0] >= self._window_seconds:
                events.popleft()

            if len(events) >= self._max_events:
                return False

            events.append(now)
            if len(self._events_by_key) > self._max_keys:
                self._events_by_key.popitem(last=False)
            return True


def _build_auth_failure_log_scope(
    *,
    client_ip: str,
    tenant_id: str,
    runner_id: str,
    error_code: str,
) -> str:
    return "|".join((client_ip, tenant_id, runner_id, error_code))


_RUNNER_AUTH_FAILURE_LOG_RATE_LIMITER = _AuthFailureLogRateLimiter(
    max_events=_RUNNER_AUTH_FAILURE_LOG_BURST,
    window_seconds=_RUNNER_AUTH_FAILURE_LOG_WINDOW_SECONDS,
    max_keys=_RUNNER_AUTH_FAILURE_LOG_MAX_KEYS,
)


class _RunnerChannelClosedError(RuntimeError):
    """Raised when a pending outbound ack waiter is interrupted by channel close."""


@dataclass(frozen=True, slots=True)
class _RunnerAckEvent:
    """Runner ack payload details keyed by outbound message id."""

    message_id: str
    status: str
    error_code: str | None


@dataclass(slots=True)
class _RunnerChannelLoopState:
    """Mutable close-state shared between channel tasks."""

    close_code: int = status.WS_1000_NORMAL_CLOSURE
    close_reason: str = "Runner channel closed."


class _RunnerAckWaiters:
    """Track pending outbound ack waits keyed by outbound message id."""

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Future[_RunnerAckEvent]]] = {}
        self._channel_error: _RunnerChannelClosedError | None = None

    def register(self, *, message_id: str) -> asyncio.Future[_RunnerAckEvent]:
        future: asyncio.Future[_RunnerAckEvent] = asyncio.get_running_loop().create_future()
        if self._channel_error is not None:
            future.set_exception(self._channel_error)
            return future
        bucket = self._waiters.setdefault(message_id, [])
        bucket.append(future)
        return future

    def resolve(self, observation: RunnerAckObservation) -> None:
        message_id = str(observation.acked_message_id).strip()
        if not message_id:
            return
        event = _RunnerAckEvent(
            message_id=message_id,
            status=str(observation.status or "accepted").strip().lower() or "accepted",
            error_code=str(observation.error_code).strip() if observation.error_code else None,
        )
        waiters = self._waiters.pop(message_id, [])
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(event)

    def cancel_waiter(self, *, message_id: str, waiter: asyncio.Future[_RunnerAckEvent]) -> None:
        bucket = self._waiters.get(message_id)
        if not bucket:
            return
        if waiter in bucket:
            bucket.remove(waiter)
        if not bucket:
            self._waiters.pop(message_id, None)
        if not waiter.done():
            waiter.cancel()

    def fail_all(self, *, error_message: str = "Runner websocket is unavailable.") -> None:
        if self._channel_error is None:
            self._channel_error = _RunnerChannelClosedError(error_message)
        pending = list(self._waiters.items())
        self._waiters.clear()
        for _message_id, waiters in pending:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(self._channel_error)


class _WebSocketRunnerOutboundTransport:
    """Production outbound transport backed by the active runner websocket."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        ack_waiters: _RunnerAckWaiters,
        send_lock: asyncio.Lock,
    ) -> None:
        self._websocket = websocket
        self._ack_waiters = ack_waiters
        self._send_lock = send_lock

    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        message_id = str(envelope.message_id).strip()
        waiter = self._ack_waiters.register(message_id=message_id)
        try:
            async with self._send_lock:
                await self._websocket.send_json(envelope.to_dict())
        except Exception:
            self._ack_waiters.cancel_waiter(message_id=message_id, waiter=waiter)
            return DispatchAttemptResult(
                delivered=False,
                acked=False,
                error_code="RUNNER_OFFLINE",
                error_message="Runner websocket is unavailable.",
                retryable=True,
            )

        try:
            event = await asyncio.wait_for(waiter, timeout=max(0.1, float(timeout_seconds)))
        except TimeoutError:
            self._ack_waiters.cancel_waiter(message_id=message_id, waiter=waiter)
            return DispatchAttemptResult(
                delivered=True,
                acked=False,
                timed_out=True,
                error_code="RUNNER_ACK_TIMEOUT",
                error_message="Runner acknowledgment timeout.",
                retryable=True,
            )
        except _RunnerChannelClosedError:
            return DispatchAttemptResult(
                delivered=False,
                acked=False,
                error_code="RUNNER_OFFLINE",
                error_message="Runner websocket is unavailable.",
                retryable=True,
            )

        if event.status in {"failed", "error", "rejected"}:
            return DispatchAttemptResult(
                delivered=True,
                acked=False,
                error_code=event.error_code or "RUNNER_ACK_REJECTED",
                error_message="Runner reported message acknowledgment failure.",
                retryable=False,
            )
        return DispatchAttemptResult(delivered=True, acked=True)


def _process_runner_channel_inbound_payload(
    *,
    bind,
    session: RunnerChannelSession,
    payload_json: str,
) -> tuple[object, bool, tuple[tuple[int, int], ...]]:
    """Process one runner inbound message in a worker thread with its own DB session."""
    factory = sessionmaker(bind=bind, autoflush=False, autocommit=False)
    thread_db = factory()
    try:
        manager = RunnerChannelManager(thread_db)
        thread_session = RunnerChannelSession(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            credential_id=session.credential_id,
            connection_id=session.connection_id,
            allowed_protocol_versions=session.allowed_protocol_versions,
            hello_received=session.hello_received,
        )
        result = manager.handle_inbound_json(thread_session, payload_json)
        thread_db.commit()
        actions = _pending_runner_vpn_materializations(thread_db, runner_id=session.runner_id)
        return result, thread_session.hello_received, actions
    except Exception:
        thread_db.rollback()
        raise
    finally:
        thread_db.close()


async def _runner_channel_inbound_loop(
    *,
    websocket: WebSocket,
    manager: RunnerChannelManager,
    session,
    db: Session,
    send_lock: asyncio.Lock,
    ack_waiters: _RunnerAckWaiters,
    loop_state: _RunnerChannelLoopState,
) -> None:
    stream_registry = get_runner_terminal_stream_registry()
    while True:
        try:
            inbound = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_RUNNER_CHANNEL_RECEIVE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            continue
        except WebSocketDisconnect as exc:
            ack_waiters.fail_all()
            loop_state.close_code = int(exc.code or loop_state.close_code)
            return

        stream_envelope = _try_parse_runner_envelope(inbound)
        if stream_envelope is not None:
            if stream_registry.handle_stream_ack(stream_envelope):
                continue
            if await _handle_terminal_stream_frame(
                session=session,
                envelope=stream_envelope,
            ):
                continue

        bind = db.get_bind()
        if bind.dialect.name == "sqlite":
            result = manager.handle_inbound_json(session, inbound)
            db.commit()
            post_commit_vpn_actions = _pending_runner_vpn_materializations(
                db,
                runner_id=session.runner_id,
            )
        else:
            token = bind_runtime_event_publish_loop(asyncio.get_running_loop())
            try:
                result, hello_received, post_commit_vpn_actions = await asyncio.to_thread(
                    _process_runner_channel_inbound_payload,
                    bind=bind,
                    session=session,
                    payload_json=inbound,
                )
                session.hello_received = hello_received
            finally:
                reset_runtime_event_publish_loop(token)

        for vpn_task_id, vpn_user_id in post_commit_vpn_actions:
            if vpn_task_id in _VPN_POST_COMMIT_TASK_KEYS:
                continue
            vpn_task = asyncio.create_task(
                _materialize_vpn_after_runtime_started(
                    task_id=vpn_task_id,
                    user_id=vpn_user_id,
                )
            )
            _VPN_POST_COMMIT_TASK_KEYS.add(vpn_task_id)
            _VPN_POST_COMMIT_TASKS.add(vpn_task)
            vpn_task.add_done_callback(
                lambda completed, task_id=vpn_task_id: _release_vpn_post_commit_task(
                    completed,
                    task_id=task_id,
                )
            )

        for envelope in result.response_envelopes:
            async with send_lock:
                await websocket.send_json(envelope.to_dict())

        if result.ack_observation is not None:
            ack_waiters.resolve(result.ack_observation)

        if result.should_close:
            loop_state.close_code = int(result.close_code or loop_state.close_code)
            loop_state.close_reason = str(result.close_reason or loop_state.close_reason)
            ack_waiters.fail_all(error_message=loop_state.close_reason)
            async with send_lock:
                await websocket.close(code=loop_state.close_code, reason=loop_state.close_reason)
            return


def _pending_runner_vpn_materializations(
    db: Session,
    *,
    runner_id: UUID,
) -> tuple[tuple[int, int], ...]:
    """Project durable pending VPN work from committed task state."""
    rows = db.execute(
        select(Task.id, Task.user_id).where(
            Task.runner_id == str(runner_id),
            Task.status == "running",
            Task.vpn_enabled.is_(True),
            Task.vpn_connection_status == "configured",
        )
    ).all()
    return tuple((int(task_id), int(user_id)) for task_id, user_id in rows)


async def _materialize_vpn_after_runtime_started(*, task_id: int, user_id: int) -> None:
    """Run VPN materialization only after the runtime.started commit succeeds."""
    from backend.services.task.lifecycle_service import TaskLifecycleService
    from backend.services.tenant.rls import clear_rls_session_context, set_task_worker_rls_context

    db = SessionLocal()
    lifecycle_service = TaskLifecycleService(db)
    task = None
    try:
        set_task_worker_rls_context(
            db,
            task_id=int(task_id),
            actor_type="system",
            user_id=int(user_id),
        )
        task = db.execute(select(Task).where(Task.id == int(task_id))).scalar_one_or_none()
        if task is None or not bool(getattr(task, "vpn_enabled", False)):
            return
        await lifecycle_service.materialize_task_vpn_config_async(
            task=task,
            user_id=int(user_id),
            db=db,
            only_if_configured=True,
        )
    except Exception as exc:
        db.rollback()
        try:
            set_task_worker_rls_context(
                db,
                task_id=int(task_id),
                actor_type="system",
                user_id=int(user_id),
            )
            task = db.execute(select(Task).where(Task.id == int(task_id))).scalar_one_or_none()
            if task is not None and bool(getattr(task, "vpn_enabled", False)):
                lifecycle_service.record_vpn_startup_failure(
                    task=task,
                    db=db,
                    error_message=f"VPN materialization failed after runtime start: {exc}",
                    provider_name="managed_runner",
                )
        except Exception:
            db.rollback()
            logger.exception("Failed to persist post-runtime-start VPN failure for task %s", task_id)
        logger.exception("Post-runtime-start VPN materialization failed for task %s", task_id)
    finally:
        try:
            clear_rls_session_context(db)
        except Exception:
            pass
        db.close()


def _try_parse_runner_envelope(payload_json: str) -> RunnerEnvelope | None:
    """Parse an inbound envelope only for stream fast-path checks."""
    try:
        return parse_runner_envelope_json(payload_json)
    except Exception:
        return None


async def _handle_terminal_stream_frame(
    *,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
) -> bool:
    """Consume known terminal stream frames without durable message ingest."""
    if envelope.message_type is not RunnerMessageType.TERMINAL_FRAME:
        return False
    if envelope.tenant_id != str(session.tenant_id) or envelope.runner_id != str(session.runner_id):
        return False
    if envelope.task_id is None:
        return False
    payload = envelope.payload
    session_id = str(getattr(payload, "session_id", "") or "").strip()
    data = str(getattr(payload, "data", "") or "")
    if not session_id:
        return False
    return await get_runner_terminal_stream_registry().ingest_stream_frame(
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        task_id=int(envelope.task_id),
        session_id=session_id,
        data=data,
    )


def authenticate_runner_channel(
    connection: HTTPConnection,
    db: Session = Depends(get_db),
) -> RunnerChannelAuthContext:
    """Authenticate runner channel headers for HTTP and websocket transports."""

    headers = connection.headers
    service = RunnerChannelAuthService(db)

    try:
        identity = service.authenticate(
            tenant_id_header=headers.get("x-runner-tenant-id"),
            runner_id_header=headers.get("x-runner-id"),
            runner_secret_header=headers.get("x-runner-credential-secret"),
        )
        db.commit()
        return identity
    except RunnerChannelAuthError as exc:
        db.rollback()
        masked_fields = RunnerCredentialService.build_masked_log_fields(
            runner_secret=headers.get("x-runner-credential-secret"),
        )
        client_ip = (connection.client.host if connection.client is not None else "") or "unknown"
        tenant_id_for_log = str(headers.get("x-runner-tenant-id", "")).strip() or "<MISSING>"
        runner_id_for_log = str(headers.get("x-runner-id", "")).strip() or "<MISSING>"
        log_scope = _build_auth_failure_log_scope(
            client_ip=client_ip,
            tenant_id=tenant_id_for_log,
            runner_id=runner_id_for_log,
            error_code=exc.error_code,
        )
        if _RUNNER_AUTH_FAILURE_LOG_RATE_LIMITER.should_log(log_scope):
            logger.warning(
                "runner_control.channel.auth.failed error_code=%s tenant_id=%s runner_id=%s client_ip=%s fields=%s",
                exc.error_code,
                tenant_id_for_log,
                runner_id_for_log,
                client_ip,
                masked_fields,
            )
        if connection.scope.get("type") == "websocket":
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=_CHANNEL_AUTH_FAILED_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_CHANNEL_AUTH_FAILED_DETAIL,
        ) from exc


@router.post("/execution-sites", response_model=ExecutionSiteResponse, status_code=status.HTTP_201_CREATED)
def create_execution_site(
    payload: ExecutionSiteCreateRequest,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> ExecutionSiteResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    try:
        site = service.create_execution_site(
            tenant_id=tenant_context.tenant_id,
            name=payload.name,
            slug=payload.slug,
            network_label=payload.network_label,
            labels=payload.labels,
        )
        db.commit()
        db.refresh(site)
        return _to_execution_site_response(site)
    except RunnerRegistryError as exc:
        db.rollback()
        raise _http_error_for_registry(exc) from exc


@router.get("/execution-sites", response_model=list[ExecutionSiteResponse])
def list_execution_sites(
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> list[ExecutionSiteResponse]:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    sites = service.list_execution_sites(tenant_id=tenant_context.tenant_id)
    return [_to_execution_site_response(site) for site in sites]


@router.get("/runner-sites", response_model=list[RunnerSiteResponse])
def list_runner_sites(
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> list[RunnerSiteResponse]:
    """Return product-facing Runner Site records for Management UI."""
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    sites = service.list_execution_sites(tenant_id=tenant_context.tenant_id)
    connectivity = service.list_runner_site_connectivity(tenant_id=tenant_context.tenant_id)
    return [_to_runner_site_response(site, connectivity.get(site.id)) for site in sites]


@router.get("/readiness", response_model=RunnerReadinessResponse)
def get_runner_readiness(
    execution_site_id: UUID | None = None,
    required_protocol_version: str | None = Query(default=None, max_length=64),
    required_runtime_version: str | None = Query(default=None, max_length=64),
    required_capabilities: list[str] | None = Query(default=None),
    minimum_available_tasks: int = Query(default=1, ge=1, le=1000),
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> RunnerReadinessResponse:
    """Return product-facing readiness for the current tenant only."""
    _enforce_runner_management_action(role=tenant_context.role)
    result = RunnerReadinessService(db).get_readiness(
        RunnerAssignmentRequest(
            tenant_id=tenant_context.tenant_id,
            execution_site_id=execution_site_id,
            required_protocol_version=required_protocol_version,
            required_runtime_version=required_runtime_version,
            required_capabilities=tuple(required_capabilities or ()),
            minimum_available_tasks=minimum_available_tasks,
        )
    )
    return RunnerReadinessResponse(
        status=result.status,
        ready=result.ready,
        reason_codes=list(result.reason_codes),
        runner_site_count=result.runner_site_count,
        connected_runner_count=result.connected_runner_count,
        evaluated_runner_count=result.evaluated_runner_count,
        selected_runner_id=result.selected_runner_id,
        execution_site_id=result.execution_site_id,
    )


@router.delete("/runner-sites/{runner_site_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_runner_site(
    runner_site_id: UUID,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> Response:
    """Guard and hard-delete a Runner Site for Management UI."""
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    try:
        service.delete_runner_site(
            tenant_id=tenant_context.tenant_id,
            execution_site_id=runner_site_id,
            actor_user_id=int(tenant_context.user_id),
        )
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except RunnerRegistryError as exc:
        db.rollback()
        raise _http_error_for_registry(exc) from exc


@router.get("/management-url", response_model=ManagementUrlResponse)
def get_management_url(
    request: Request,
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> ManagementUrlResponse:
    """Return the canonical Runner-facing Management URL."""
    _enforce_runner_management_action(role=tenant_context.role)
    try:
        resolved = ManagementUrlService().resolve(request=request)
    except ManagementUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ManagementUrlResponse(
        management_url=resolved.management_url,
        source=resolved.source,
    )


@router.put("/management-url", response_model=ManagementUrlResponse)
def update_management_url(
    payload: ManagementUrlUpdateRequest,
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> ManagementUrlResponse:
    """Persist the canonical Runner-facing Management URL."""
    _enforce_runner_management_action(role=tenant_context.role)
    try:
        resolved = ManagementUrlService().set_url(payload.management_url)
    except ManagementUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ManagementUrlResponse(
        management_url=resolved.management_url,
        source=resolved.source,
    )


@router.post("/enrollments", response_model=RunnerEnrollmentCreateResponse, status_code=status.HTTP_201_CREATED)
def create_runner_enrollment(
    payload: RunnerEnrollmentCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> RunnerEnrollmentCreateResponse:
    """Create one-time Runner enrollment material without exposing tenant id."""
    return _create_runner_enrollment_response(
        payload=payload,
        request=request,
        db=db,
        tenant_context=tenant_context,
    )


@router.post("/enrollments/package", response_class=FileResponse, status_code=status.HTTP_201_CREATED)
def create_runner_enrollment_package(
    payload: RunnerEnrollmentCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> FileResponse:
    """Create Runner enrollment and return a preconfigured Runner Site package."""
    enrollment = _create_runner_enrollment_response(
        payload=payload,
        request=request,
        db=db,
        tenant_context=tenant_context,
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="drowai-runner-site-package-"))
    enrollment_path = temp_dir / "enrollment.toml"
    output_path = temp_dir / enrollment.package_name
    try:
        enrollment_path.write_text(enrollment.enrollment_toml, encoding="utf-8")
        enrollment_path.chmod(0o600)
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts/package_execution_site.py"),
                "--enrollment-toml",
                str(enrollment_path),
                "--output",
                str(output_path),
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.returncode != 0 or not output_path.is_file():
            logger.error(
                "runner_control.enrollment.package_failed enrollment_id=%s returncode=%s stdout=%s stderr=%s",
                enrollment.enrollment_id,
                proc.returncode,
                _trim_package_log(proc.stdout),
                _trim_package_log(proc.stderr),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Runner Site package generation failed.",
            )
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Runner Site package generation failed.",
        ) from exc

    background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
    return FileResponse(
        output_path,
        status_code=status.HTTP_201_CREATED,
        media_type="application/gzip",
        filename=enrollment.package_name,
        background=background_tasks,
    )


def _create_runner_enrollment_response(
    *,
    payload: RunnerEnrollmentCreateRequest,
    request: Request,
    db: Session,
    tenant_context: TenantRequestContext,
) -> RunnerEnrollmentCreateResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    persist_management_url_override = False
    try:
        if payload.management_url is not None and payload.management_url.strip():
            management_url = normalize_management_url(payload.management_url)
            persist_management_url_override = True
        else:
            resolved_management = ManagementUrlService().resolve(request=request)
            management_url = resolved_management.management_url
    except ManagementUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    service = RunnerRegistryService(db)
    slug = (payload.site_slug or _slugify_runner_site_name(payload.site_name)).strip()
    try:
        site = _find_or_create_runner_site(
            db=db,
            service=service,
            tenant_id=tenant_context.tenant_id,
            name=payload.site_name,
            slug=slug,
            network_label=payload.network_label,
            labels=payload.labels,
        )
        issued = service.issue_install_token(
            tenant_id=tenant_context.tenant_id,
            execution_site_id=site.id,
            created_by_user_id=int(tenant_context.user_id),
            ttl_seconds=payload.ttl_seconds,
        )
        db.commit()
        db.refresh(site)
    except RunnerRegistryError as exc:
        db.rollback()
        raise _http_error_for_registry(exc) from exc

    if persist_management_url_override:
        try:
            ManagementUrlService().set_url(management_url)
        except ManagementUrlError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    allow_insecure = (
        payload.allow_insecure_management_url
        if payload.allow_insecure_management_url is not None
        else management_url.startswith("http://")
    )
    enrollment_toml = build_runner_enrollment_toml(
        management_url=management_url,
        enrollment_token=issued.plaintext_token,
        tls_verify=payload.tls_verify,
        allow_insecure_management_url=allow_insecure,
        labels={
            "deployment": "runner-site",
            "site": site.slug,
            **dict(payload.labels or {}),
        },
    )
    package_name = f"drowai-runner-site-{site.slug}.tar.gz"
    return RunnerEnrollmentCreateResponse(
        runner_site=_to_runner_site_response(site),
        enrollment_id=issued.install_token_id,
        expires_at=issued.expires_at,
        status="waiting",
        enrollment_toml=enrollment_toml,
        package_name=package_name,
        install_commands=[
            f"tar xzf {package_name}",
            "cd drowai-runner-site",
            "docker compose up -d --build",
        ],
    )


@router.post("/install-tokens", response_model=InstallTokenCreateResponse, status_code=status.HTTP_201_CREATED)
def create_install_token(
    payload: InstallTokenCreateRequest,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> InstallTokenCreateResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    try:
        issued = service.issue_install_token(
            tenant_id=tenant_context.tenant_id,
            execution_site_id=payload.execution_site_id,
            created_by_user_id=int(tenant_context.user_id),
            ttl_seconds=payload.ttl_seconds,
        )
        db.commit()
        return InstallTokenCreateResponse(
            install_token_id=issued.install_token_id,
            execution_site_id=payload.execution_site_id,
            install_token=issued.plaintext_token,
            expires_at=issued.expires_at,
        )
    except RunnerRegistryError as exc:
        db.rollback()
        raise _http_error_for_registry(exc) from exc


@router.get("/runners", response_model=list[RunnerListItemResponse])
def list_runners(
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> list[RunnerListItemResponse]:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    runners = service.list_runners(tenant_id=tenant_context.tenant_id)
    return [_to_runner_list_item(runner) for runner in runners]


@router.get("/runners/{runner_id}", response_model=RunnerDetailResponse)
def get_runner(
    runner_id: UUID,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> RunnerDetailResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    try:
        runner = service.get_runner(tenant_id=tenant_context.tenant_id, runner_id=runner_id)
    except RunnerRegistryError as exc:
        raise _http_error_for_registry(exc) from exc

    credentials = service.list_runner_credentials(tenant_id=tenant_context.tenant_id, runner_id=runner.id)
    return _to_runner_detail(runner, credentials)


@router.post("/runners/{runner_id}/revoke", response_model=RunnerRevokeResponse)
def revoke_runner(
    runner_id: UUID,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> RunnerRevokeResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    service = RunnerRegistryService(db)
    try:
        revoked_count = service.revoke_runner_credentials(
            tenant_id=tenant_context.tenant_id,
            runner_id=runner_id,
            actor_user_id=int(tenant_context.user_id),
        )
        db.commit()
        return RunnerRevokeResponse(
            runner_id=runner_id,
            revoked_credential_count=revoked_count,
        )
    except RunnerRegistryError as exc:
        db.rollback()
        raise _http_error_for_registry(exc) from exc


@router.post("/register", response_model=RunnerRegistrationResponse, status_code=status.HTTP_201_CREATED)
def register_runner(
    payload: RunnerRegistrationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> RunnerRegistrationResponse:
    channel_endpoint = f"{str(request.base_url).rstrip('/')}/api/runner-control/channel"
    service = RunnerRegistrationService(db, channel_endpoint=channel_endpoint)
    try:
        result = service.register_runner(
            RunnerRegistrationServiceRequest(
                tenant_id=payload.tenant_id,
                install_token=payload.install_token,
                runner_name=payload.runner_name,
                runner_version=payload.runner_version,
                labels=payload.labels,
                capabilities=payload.capabilities,
            )
        )
        db.commit()
    except RunnerRegistrationError as exc:
        db.rollback()
        masked_fields = RunnerCredentialService.build_masked_log_fields(
            install_token=payload.install_token,
        )
        logger.warning(
            "runner_control.registration.failed error_code=%s tenant_id=%s fields=%s",
            exc.error_code,
            payload.tenant_id,
            masked_fields,
        )
        if exc.error_code == "RUNNER_METADATA_INVALID":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runner registration failed.",
        ) from exc

    masked_fields = RunnerCredentialService.build_masked_log_fields(
        install_token=payload.install_token,
        runner_secret=result.credential_secret,
        credential_fingerprint=result.credential_fingerprint,
    )
    logger.info(
        "runner_control.registration.succeeded tenant_id=%s runner_id=%s fields=%s",
        result.tenant_id,
        result.runner_id,
        masked_fields,
    )
    endpoint_metadata = result.endpoint_metadata
    return RunnerRegistrationResponse(
        runner_id=result.runner_id,
        tenant_id=result.tenant_id,
        credential_id=result.credential_id,
        credential_fingerprint=result.credential_fingerprint,
        credential_secret=result.credential_secret,
        channel_endpoint=str(endpoint_metadata.get("channel_endpoint", "")),
        protocol_version=str(endpoint_metadata.get("protocol_version", "")),
        heartbeat_interval_seconds=int(endpoint_metadata.get("heartbeat_interval_seconds", 30)),
    )


@router.post(
    "/tasks/{task_id}/assign-runner",
    response_model=TaskRunnerAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_runner_to_task(
    task_id: int,
    payload: TaskRunnerAssignmentRequest,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> TaskRunnerAssignmentResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    task = get_task_in_tenant_or_404(
        db=db,
        task_id=task_id,
        tenant_id=tenant_context.tenant_id,
    )

    assignment_result = RunnerAssignmentService(db).select_runner(
        RunnerAssignmentRequest(
            tenant_id=tenant_context.tenant_id,
            execution_site_id=payload.execution_site_id,
            required_protocol_version=payload.required_protocol_version,
            required_runtime_version=payload.required_runtime_version,
            required_capabilities=tuple(payload.required_capabilities),
            required_labels=payload.required_labels,
            minimum_available_tasks=payload.minimum_available_tasks,
        )
    )
    if assignment_result.selection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": _NO_ELIGIBLE_RUNNER_ERROR_CODE,
                "reason_codes": list(assignment_result.reason_codes),
            },
        )

    selected_runner_id = assignment_result.selection.runner_id
    idempotency_key = payload.idempotency_key or f"runner_control:assignment:task:{task.id}:{uuid4().hex}"

    runtime_job_service = RuntimeJobService(db)
    try:
        runtime_job = runtime_job_service.create_runtime_job(
            RuntimeJobCreateRequest(
                tenant_id=tenant_context.tenant_id,
                task_id=task.id,
                job_type=_ASSIGNMENT_RUNTIME_JOB_TYPE,
                idempotency_key=idempotency_key,
                payload_json=payload.payload_json,
                correlation_id=payload.correlation_id,
            )
        )
        assigned_runtime_job = runtime_job_service.assign_runtime_job(
            tenant_id=tenant_context.tenant_id,
            runtime_job_id=runtime_job.id,
            runner_id=selected_runner_id,
        )
        DBRunnerCoordinationStore(db, pod_id=_ASSIGNMENT_ROUTER_POD_ID).enqueue_outbound_message(
            tenant_id=tenant_context.tenant_id,
            runner_id=selected_runner_id,
            message_id=f"runner-control-assignment-probe-{uuid4().hex}",
            message_type=_ASSIGNMENT_PROBE_MESSAGE_TYPE,
            payload_json={
                "runtime_job_id": str(assigned_runtime_job.id),
                "task_id": task.id,
                "operation": "assign_runner_to_task",
                "probe": payload.payload_json or {},
            },
            idempotency_key=f"probe:{assigned_runtime_job.id}",
            runtime_job_id=assigned_runtime_job.id,
            task_id=task.id,
            correlation_id=payload.correlation_id,
        )
        task.runner_id = str(selected_runner_id)
        task.execution_site_id = str(assignment_result.selection.execution_site_id)
        db.flush()
        db.commit()
        db.refresh(assigned_runtime_job)
    except RuntimeJobServiceError as exc:
        db.rollback()
        raise _http_error_for_runtime_job(exc) from exc

    return TaskRunnerAssignmentResponse(
        runtime_job_id=assigned_runtime_job.id,
        runtime_job_status=assigned_runtime_job.status,
        task_id=task.id,
        runner_id=selected_runner_id,
        execution_site_id=assignment_result.selection.execution_site_id,
        idempotency_key=assigned_runtime_job.idempotency_key,
        reason_codes=[],
    )


@router.get("/runtime-jobs/{runtime_job_id}", response_model=RuntimeJobResponse)
def get_runtime_job(
    runtime_job_id: UUID,
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> RuntimeJobResponse:
    _enforce_runner_management_action(role=tenant_context.role)
    runtime_job = db.execute(
        select(RuntimeJob).where(
            RuntimeJob.id == runtime_job_id,
            RuntimeJob.tenant_id == tenant_context.tenant_id,
        )
    ).scalar_one_or_none()
    if runtime_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime job not found.")
    if runtime_job.task_id is not None:
        get_task_in_tenant_or_404(
            db=db,
            task_id=int(runtime_job.task_id),
            tenant_id=tenant_context.tenant_id,
        )
    return _to_runtime_job_response(runtime_job)


@router.get("/runtime-jobs", response_model=list[RuntimeJobResponse])
def list_runtime_jobs(
    task_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> list[RuntimeJobResponse]:
    _enforce_runner_management_action(role=tenant_context.role)
    get_task_in_tenant_or_404(
        db=db,
        task_id=task_id,
        tenant_id=tenant_context.tenant_id,
    )

    statement = select(RuntimeJob).where(
        RuntimeJob.tenant_id == tenant_context.tenant_id,
        RuntimeJob.task_id == task_id,
    )

    rows = db.execute(statement.order_by(RuntimeJob.created_at.desc(), RuntimeJob.id.desc())).scalars().all()
    return [_to_runtime_job_response(runtime_job) for runtime_job in rows]


@router.websocket("/channel")
async def runner_channel(
    websocket: WebSocket,
    identity: RunnerChannelAuthContext = Depends(authenticate_runner_channel),
    db: Session = Depends(get_db),
) -> None:
    """Authenticated runner outbound websocket channel for Runner Control control traffic."""

    manager = RunnerChannelManager(db)
    session = None
    loop_state = _RunnerChannelLoopState()
    ack_waiters = _RunnerAckWaiters()
    send_lock = asyncio.Lock()
    inbound_task: asyncio.Task[None] | None = None
    transport = _WebSocketRunnerOutboundTransport(
        websocket=websocket,
        ack_waiters=ack_waiters,
        send_lock=send_lock,
    )
    stream_registry = get_runner_terminal_stream_registry()

    async def _send_stream_envelope(envelope: RunnerEnvelope) -> None:
        async with send_lock:
            await websocket.send_json(envelope.to_dict())

    try:
        session = manager.open_session(
            identity,
            remote_ip_address=websocket.client.host if websocket.client else None,
        )
        db.commit()
        await websocket.accept()
        stream_registry.register_channel(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            sender=_send_stream_envelope,
        )
        inbound_task = asyncio.create_task(
            _runner_channel_inbound_loop(
                websocket=websocket,
                manager=manager,
                session=session,
                db=db,
                send_lock=send_lock,
                ack_waiters=ack_waiters,
                loop_state=loop_state,
            )
        )
        while True:
            if inbound_task.done():
                inbound_task.result()
                break
            await manager.dispatch_outbound_messages(
                session,
                transport=transport,
                max_messages=_RUNNER_OUTBOUND_MAX_MESSAGES_PER_POLL,
            )
            db.commit()
            await asyncio.sleep(_RUNNER_CHANNEL_RECEIVE_TIMEOUT_SECONDS)
    except WebSocketDisconnect as exc:
        loop_state.close_code = int(exc.code or loop_state.close_code)
    except Exception:
        db.rollback()
        loop_state.close_code = status.WS_1011_INTERNAL_ERROR
        loop_state.close_reason = "Runner channel internal error."
        logger.exception(
            "runner_control.channel.internal_error tenant_id=%s runner_id=%s connection_id=%s",
            identity.tenant_id,
            identity.runner_id,
            session.connection_id if session is not None else "unknown",
        )
        ack_waiters.fail_all(error_message=loop_state.close_reason)
        await websocket.close(code=loop_state.close_code, reason=loop_state.close_reason)
    finally:
        stream_registry.unregister_channel(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
        )
        ack_waiters.fail_all(error_message=loop_state.close_reason)
        if inbound_task is not None and not inbound_task.done():
            inbound_task.cancel()
            with suppress(asyncio.CancelledError):
                await inbound_task
        if session is not None:
            try:
                manager.close_session(session)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "runner_control.channel.close_finalize_failed tenant_id=%s runner_id=%s connection_id=%s",
                    identity.tenant_id,
                    identity.runner_id,
                    session.connection_id,
                )
        logger.info(
            "runner_control.channel.closed event=%s code=%s tenant_id=%s runner_id=%s connection_id=%s",
            _RUNNER_CHANNEL_CLOSE_EVENTS.get(int(loop_state.close_code), "RUNNER_CHANNEL_CLOSED_UNKNOWN"),
            int(loop_state.close_code),
            identity.tenant_id,
            identity.runner_id,
            session.connection_id if session is not None else "unknown",
        )


def _http_error_for_registry(exc: RunnerRegistryError) -> HTTPException:
    if exc.error_code in {
        "RUNNER_SITE_NOT_FOUND",
        "RUNNER_SITE_ACTIVE_EXECUTIONS",
        "RUNNER_SITE_LAST_CONNECTED",
    }:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if exc.error_code == "RUNNER_SITE_NOT_FOUND"
            else status.HTTP_409_CONFLICT
        )
        return HTTPException(
            status_code=error_status,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
                **exc.details,
            },
        )
    if exc.error_code in {"RUNNER_NOT_FOUND", "EXECUTION_SITE_NOT_FOUND"}:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if exc.error_code in {"EXECUTION_SITE_CONFLICT"}:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if exc.error_code in {"RUNNER_VALIDATION_ERROR"}:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Runner control request failed.")


def _http_error_for_runtime_job(exc: RuntimeJobServiceError) -> HTTPException:
    if exc.error_code in {
        "TASK_NOT_FOUND",
        "RUNNER_NOT_FOUND",
        "RUNTIME_JOB_NOT_FOUND",
    }:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if exc.error_code in {
        "RUNNER_TENANT_MISMATCH",
        "RUNTIME_JOB_TASK_TENANT_MISMATCH",
        "RUNTIME_JOB_ASSIGNMENT_CONFLICT",
        "RUNTIME_JOB_IDEMPOTENCY_CONFLICT",
        "RUNTIME_JOB_TRANSITION_STALE",
        "RUNTIME_JOB_TRANSITION_INVALID",
    }:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": exc.error_code, "message": str(exc)},
        )
    if exc.error_code in {"RUNTIME_JOB_VALIDATION_ERROR"}:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error_code": exc.error_code, "message": str(exc)},
    )


def _to_execution_site_response(site) -> ExecutionSiteResponse:
    return ExecutionSiteResponse(
        id=site.id,
        tenant_id=site.tenant_id,
        name=site.name,
        slug=site.slug,
        network_label=site.network_label,
        status=site.status,
        labels=site.labels_json,
        created_at=site.created_at,
        updated_at=site.updated_at,
    )


def _to_runner_site_response(site, connectivity=None) -> RunnerSiteResponse:
    return RunnerSiteResponse(
        id=site.id,
        name=site.name,
        slug=site.slug,
        network_label=site.network_label,
        status=site.status,
        connectivity_status=getattr(connectivity, "connectivity_status", "waiting"),
        runner_count=int(getattr(connectivity, "runner_count", 0) or 0),
        connected_runner_count=int(getattr(connectivity, "connected_runner_count", 0) or 0),
        last_seen_at=getattr(connectivity, "last_seen_at", None),
        labels=site.labels_json,
        created_at=site.created_at,
        updated_at=site.updated_at,
    )


def _find_or_create_runner_site(
    *,
    db: Session,
    service: RunnerRegistryService,
    tenant_id: int,
    name: str,
    slug: str,
    network_label: str | None,
    labels: dict[str, str] | None,
) -> ExecutionSite:
    existing = db.execute(
        select(ExecutionSite).where(
            ExecutionSite.tenant_id == tenant_id,
            ExecutionSite.slug == slug,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.name = name
        existing.network_label = network_label
        existing.labels_json = labels or existing.labels_json
        existing.status = "active"
        db.flush()
        return existing
    return service.create_execution_site(
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        network_label=network_label,
        labels=labels,
    )


def _slugify_runner_site_name(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    parts = [part for part in slug.split("-") if part]
    return "-".join(parts) or f"runner-site-{uuid4().hex[:8]}"


def _trim_package_log(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    for pattern, replacement in _PACKAGE_LOG_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:1000]


def _to_runner_list_item(runner) -> RunnerListItemResponse:
    return RunnerListItemResponse(
        id=runner.id,
        execution_site_id=runner.execution_site_id,
        name=runner.name,
        status=runner.status,
        version=runner.version,
        labels=runner.labels_json,
        capabilities=runner.capabilities_json,
        capacity=runner.capacity_json,
        last_seen_at=runner.last_seen_at,
        created_at=runner.created_at,
        updated_at=runner.updated_at,
    )


def _to_runner_detail(runner, credentials) -> RunnerDetailResponse:
    return RunnerDetailResponse(
        id=runner.id,
        tenant_id=runner.tenant_id,
        execution_site_id=runner.execution_site_id,
        name=runner.name,
        status=runner.status,
        version=runner.version,
        labels=runner.labels_json,
        capabilities=runner.capabilities_json,
        capacity=runner.capacity_json,
        last_seen_at=runner.last_seen_at,
        created_at=runner.created_at,
        updated_at=runner.updated_at,
        credentials=[
            RunnerCredentialSummaryResponse(
                id=credential.id,
                credential_fingerprint=credential.credential_fingerprint,
                status=credential.status,
                expires_at=credential.expires_at,
                last_used_at=credential.last_used_at,
                revoked_at=credential.revoked_at,
                created_at=credential.created_at,
            )
            for credential in credentials
        ],
    )


def _to_runtime_job_response(runtime_job: RuntimeJob) -> RuntimeJobResponse:
    return RuntimeJobResponse(
        id=runtime_job.id,
        tenant_id=runtime_job.tenant_id,
        task_id=runtime_job.task_id,
        runner_id=runtime_job.runner_id,
        execution_site_id=runtime_job.execution_site_id,
        job_type=runtime_job.job_type,
        status=runtime_job.status,
        idempotency_key=runtime_job.idempotency_key,
        correlation_id=runtime_job.correlation_id,
        payload_json=runtime_job.payload_json,
        result_json=runtime_job.result_json,
        error_code=runtime_job.error_code,
        error_message=runtime_job.error_message,
        lease_expires_at=runtime_job.lease_expires_at,
        created_at=runtime_job.created_at,
        updated_at=runtime_job.updated_at,
    )
