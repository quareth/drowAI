"""Remote-runtime terminal proxy dispatch integration coverage.

Scope:
- Exercise CloudRunnerRuntimeProvider -> RunnerOutboundDispatcher -> RunnerCloudClient
  routing for terminal open/input/resize/close requests.
- Verify runner-local terminal proxy operations complete through terminal.result events.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.dispatcher import (
    DispatchAttemptResult,
    RunnerOutboundDispatcher,
    RunnerOutboundTransport,
)
from backend.services.runtime_provider.cloud_runner_provider import CloudRunnerRuntimeProvider
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationRequest,
    RuntimePlacementMode,
)
from drowai_runner.cloud_client import RunnerCloudClient
from drowai_runner.config import RunnerConfig
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from runtime_shared.runner_protocol import parse_runner_envelope_json


class _RecordingPtyAdapter:
    def __init__(self) -> None:
        self.open_calls: list[tuple[str, str, int, int]] = []
        self.input_calls: list[tuple[str, str]] = []
        self.resize_calls: list[tuple[str, int, int]] = []
        self.close_calls: list[str] = []
        self._buffers: dict[str, bytearray] = {}

    def open_session(self, *, container_id: str, session_id: str, cols: int, rows: int) -> None:
        self.open_calls.append((container_id, session_id, cols, rows))
        self._buffers[session_id] = bytearray(b"runner$ ")

    def send_input(self, *, session_id: str, data: str) -> None:
        self.input_calls.append((session_id, data))
        self._buffers.setdefault(session_id, bytearray()).extend(data.encode("utf-8"))

    def read_output(self, *, session_id: str, max_bytes: int) -> bytes:
        buffer = self._buffers.get(session_id, bytearray())
        chunk = bytes(buffer[:max_bytes])
        del buffer[:max_bytes]
        self._buffers[session_id] = buffer
        return chunk

    def resize_session(self, *, session_id: str, cols: int, rows: int) -> None:
        self.resize_calls.append((session_id, cols, rows))

    def close_session(self, *, session_id: str) -> None:
        self.close_calls.append(session_id)
        self._buffers.pop(session_id, None)


class _TerminalLoopbackOperationService:
    def __init__(self, *, job_store, pty_adapter: _RecordingPtyAdapter) -> None:
        self._job_store = job_store
        self._terminal_proxy = RunnerTerminalProxy(job_store=job_store, pty_adapter=pty_adapter)

    def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
        if operation == "materialize_runtime":
            runtime_job_id = str(params["runtime_job_id"])
            task_id = str(params["task_id"])
            workspace_id = str(params["workspace_id"])
            if self._job_store.find_job(runtime_job_id) is None:
                self._job_store.start_job(
                    runtime_job_id=runtime_job_id,
                    tenant_id=str(params["tenant_id"]),
                    task_id=task_id,
                    workspace_id=workspace_id,
                    image="runtime:test",
                    container_id="cid-remote-runtime-terminal",
                )
            self._job_store.mark_running(runtime_job_id, container_id="cid-remote-runtime-terminal")
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": runtime_job_id,
                    "task_id": task_id,
                    "workspace_id": workspace_id,
                    "container_id": "cid-remote-runtime-terminal",
                },
            }
        if operation == "runtime_startup_progress":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": str(params["runtime_job_id"]),
                    "job_status": "running",
                    "container_status": "running",
                    "startup_phase": "container_running",
                },
            }
        if operation == "runtime_status":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": str(params["runtime_job_id"]),
                    "job_status": "running",
                    "container_status": "running",
                    "workspace_id": "task-123",
                },
            }
        if operation == "runtime_logs":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": str(params["runtime_job_id"]),
                    "logs": [{"message": "runtime started"}],
                },
            }
        if operation == "runtime_metrics":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": str(params["runtime_job_id"]),
                    "metrics": {
                        "status": "running",
                        "container_running": True,
                        "cpu_percent": 1.2,
                    },
                },
            }

        if operation == "terminal_open":
            response = self._terminal_proxy.open_terminal_session(
                runtime_job_id=str(params["runtime_job_id"]),
                session_name=str(params.get("session_name") or "runtime"),
                cols=int(params.get("cols") or 120),
                rows=int(params.get("rows") or 30),
            )
            return {
                "accepted": response.accepted,
                "status": response.status,
                "error_code": response.error_code,
                "error_message": response.error_message,
                "metadata": dict(response.metadata or {}),
            }
        if operation == "terminal_input":
            response = self._terminal_proxy.send_terminal_input(
                session_id=str(params["session_id"]),
                data=str(params.get("data") or ""),
            )
            return {
                "accepted": response.accepted,
                "status": response.status,
                "error_code": response.error_code,
                "error_message": response.error_message,
                "metadata": dict(response.metadata or {}),
            }
        if operation == "terminal_resize":
            response = self._terminal_proxy.resize_terminal_session(
                session_id=str(params["session_id"]),
                cols=int(params.get("cols") or 120),
                rows=int(params.get("rows") or 30),
            )
            return {
                "accepted": response.accepted,
                "status": response.status,
                "error_code": response.error_code,
                "error_message": response.error_message,
                "metadata": dict(response.metadata or {}),
            }
        if operation == "terminal_close":
            response = self._terminal_proxy.close_terminal_session(session_id=str(params["session_id"]))
            return {
                "accepted": response.accepted,
                "status": response.status,
                "error_code": response.error_code,
                "error_message": response.error_message,
                "metadata": dict(response.metadata or {}),
            }
        if operation == "terminal_read":
            response = self._terminal_proxy.read_terminal_output(
                session_id=str(params["session_id"]),
                max_bytes=int(params.get("max_bytes") or 16384),
            )
            return {
                "accepted": response.accepted,
                "status": response.status,
                "error_code": response.error_code,
                "error_message": response.error_message,
                "metadata": dict(response.metadata or {}),
            }
        raise AssertionError(operation)


class _LoopbackWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, payload: str) -> None:
        self.sent.append(payload)


class _RunnerLoopbackTransport(RunnerOutboundTransport):
    def __init__(
        self,
        *,
        client: RunnerCloudClient,
        identity: CloudChannelIdentity,
        channel_manager: RunnerChannelManager,
        channel_session,
    ) -> None:
        self._client = client
        self._identity = identity
        self._channel_manager = channel_manager
        self._channel_session = channel_session
        self._websocket = _LoopbackWebSocket()
        self._session_state = ConnectionSessionState()
        self.envelopes = []
        self.protocol_errors: list[str] = []
        self._pending_inbound_payloads: list[str] = []

    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del timeout_seconds
        self.envelopes.append(envelope)
        parsed_envelope = parse_runner_envelope_json(envelope.to_json())
        sent_before = len(self._websocket.sent)
        if self._client._remote_runtime_handler.is_request(parsed_envelope):
            self._client._remote_runtime_handler.handle(
                websocket=self._websocket,
                identity=self._identity,
                inbound=parsed_envelope,
                session_state=self._session_state,
            )
        self._pending_inbound_payloads.extend(self._websocket.sent[sent_before:])
        return DispatchAttemptResult(delivered=True, acked=True)

    def flush_inbound_events(self) -> None:
        payloads = list(self._pending_inbound_payloads)
        self._pending_inbound_payloads.clear()
        for payload_json in payloads:
            handled = self._channel_manager.handle_inbound_json(self._channel_session, payload_json)
            for envelope_response in handled.response_envelopes:
                error_code = str(getattr(envelope_response.payload, "error_code", "") or "").strip()
                if error_code:
                    self.protocol_errors.append(error_code)

    def pump_terminal_frames(self) -> None:
        sent_before = len(self._websocket.sent)
        self._client._terminal_frame_lifecycle.emit_for_active_sessions(
            websocket=self._websocket,
            identity=self._identity,
        )
        self._pending_inbound_payloads.extend(self._websocket.sent[sent_before:])


def _build_cloud_config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_LABELS": '{"site":"hq"}',
            "DROWAI_RUNNER_CAPABILITIES": '["docker"]',
            "DROWAI_RUNNER_MAX_ACTIVE_TASKS": "3",
        }
    )


def _request(
    *,
    task_id: int,
    runner_id: str,
    operation: str,
    payload: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> RuntimeOperationRequest:
    return RuntimeOperationRequest(
        tenant_id=1,
        task_id=task_id,
        actor_type=RuntimeActorType.SYSTEM,
        actor_id="integration-test",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id=f"task-{task_id}",
        operation=operation,
        runner_id=runner_id,
        payload=payload,
        metadata=dict(metadata or {}),
    )


def test_remote_runtime_terminal_operations_route_through_provider_dispatcher_and_runner_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "1")

    database_path = tmp_path / "remote-runtime-terminal-dispatch.db"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup_db: Session = session_factory()
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    setup_db.add(tenant)
    setup_db.flush()

    user = User(username="remote-runtime-terminal-user", password="secret", email="remote-runtime-terminal@example.com")
    setup_db.add(user)
    setup_db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    setup_db.add(site)
    setup_db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status="active",
        last_seen_at=datetime.now(tz=UTC),
    )
    setup_db.add(runner)
    setup_db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Remote Runtime Terminal Task",
        runtime_placement_mode="runner",
        runner_id=str(runner.id).lower(),
        status="starting",
    )
    setup_db.add(task)
    setup_db.commit()
    tenant_id = int(tenant.id)
    runner_uuid = UUID(str(runner.id))
    runner_id = str(runner_uuid)
    task_id = int(task.id)
    setup_db.close()

    channel_db: Session = session_factory()
    credential_id = RunnerCredentialService(channel_db).issue_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner_uuid,
    ).credential_id
    channel_db.commit()

    channel_manager = RunnerChannelManager(channel_db)
    channel_session = channel_manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant_id,
            runner_id=runner_uuid,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )
    channel_manager.handle_inbound_json(
        channel_session,
        json.dumps(
            {
                "message_id": "hello-remote-runtime-terminal",
                "type": "runner.hello",
                "schema_version": "runner_control.v1",
                "tenant_id": str(tenant_id),
                "runner_id": runner_id,
                "correlation_id": None,
                "runtime_job_id": None,
                "task_id": None,
                "created_at": "2026-05-23T12:00:00+00:00",
                "payload": {
                    "version": "1.0.0",
                    "capabilities": ["docker"],
                    "labels": {"site": "hq"},
                },
            }
        ),
    )
    channel_db.commit()

    config = _build_cloud_config(tmp_path)
    runner_client = RunnerCloudClient(config=config)
    job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    runner_client._composition._job_store = job_store

    pty_adapter = _RecordingPtyAdapter()
    runner_client._composition._operation_service = _TerminalLoopbackOperationService(
        job_store=job_store,
        pty_adapter=pty_adapter,
    )

    identity = CloudChannelIdentity(
        tenant_id=tenant_id,
        runner_id=runner_id,
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    dispatch_db: Session = session_factory()
    transport = _RunnerLoopbackTransport(
        client=runner_client,
        identity=identity,
        channel_manager=channel_manager,
        channel_session=channel_session,
    )
    dispatcher = RunnerOutboundDispatcher(dispatch_db)
    provider = CloudRunnerRuntimeProvider(session_factory=session_factory)

    def _dispatch_pending() -> None:
        result = asyncio.run(
            dispatcher.dispatch_for_connection(
                tenant_id=tenant_id,
                runner_id=runner_uuid,
                connection_id=channel_session.connection_id,
                transport=transport,
                max_messages=20,
            )
        )
        dispatch_db.commit()
        transport.flush_inbound_events()
        channel_db.commit()
        assert result.claimed_count >= 1

    start_result = asyncio.run(
        provider.provision_task_runtime(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="provision_task_runtime",
                payload={"target": "127.0.0.1"},
            )
        )
    )
    assert start_result.accepted is True
    _dispatch_pending()

    verify_db: Session = session_factory()
    start_runtime_job = verify_db.execute(
        select(RuntimeJob).where(RuntimeJob.id == UUID(str(start_result.metadata["runtime_job_id"])))
    ).scalar_one()
    assert start_runtime_job.status == "succeeded"
    refreshed_task = verify_db.execute(select(Task).where(Task.id == task_id)).scalar_one()
    assert refreshed_task.status == "running"

    status_result = asyncio.run(
        provider.get_runtime_status(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="get_runtime_status",
                payload={},
            )
        )
    )
    assert status_result.accepted is True
    _dispatch_pending()
    status_runtime_job = verify_db.execute(
        select(RuntimeJob).where(RuntimeJob.id == UUID(str(status_result.metadata["runtime_job_id"])))
    ).scalar_one()
    status_payload = status_runtime_job.result_json if isinstance(status_runtime_job.result_json, dict) else {}
    assert status_runtime_job.status == "succeeded"
    assert status_payload.get("message_type") == "runtime.status"

    logs_result = asyncio.run(
        provider.get_runtime_logs(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="get_runtime_logs",
                payload={"lines": 20},
            )
        )
    )
    assert logs_result.accepted is True
    _dispatch_pending()
    logs_runtime_job = verify_db.execute(
        select(RuntimeJob).where(RuntimeJob.id == UUID(str(logs_result.metadata["runtime_job_id"])))
    ).scalar_one()
    logs_payload = logs_runtime_job.result_json if isinstance(logs_runtime_job.result_json, dict) else {}
    assert logs_runtime_job.status == "succeeded"
    assert logs_payload.get("message_type") == "runtime.logs"

    metrics_result = asyncio.run(
        provider.get_runtime_metrics(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="get_runtime_metrics",
                payload={},
            )
        )
    )
    assert metrics_result.accepted is True
    _dispatch_pending()
    metrics_runtime_job = verify_db.execute(
        select(RuntimeJob).where(RuntimeJob.id == UUID(str(metrics_result.metadata["runtime_job_id"])))
    ).scalar_one()
    metrics_payload = (
        metrics_runtime_job.result_json if isinstance(metrics_runtime_job.result_json, dict) else {}
    )
    assert metrics_runtime_job.status == "succeeded"
    assert metrics_payload.get("message_type") == "runtime.metrics"

    open_result = asyncio.run(
        provider.open_terminal_session(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="open_terminal_session",
                payload={"session_name": "runtime", "cols": 120, "rows": 30},
            )
        )
    )
    assert open_result.accepted is True
    _dispatch_pending()
    assert transport.protocol_errors == []

    open_runtime_job = verify_db.execute(
        select(RuntimeJob).where(RuntimeJob.id == UUID(str(open_result.metadata["runtime_job_id"])))
    ).scalar_one()
    assert open_runtime_job.status == "succeeded"
    open_result_payload = open_runtime_job.result_json if isinstance(open_runtime_job.result_json, dict) else {}
    assert open_result_payload.get("terminal_operation") == "open"
    session_id = str(open_result_payload.get("session_id") or "")
    assert session_id

    baseline_read = asyncio.run(
        provider.read_terminal_output(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="read_terminal_output",
                payload={"session_id": session_id, "runtime_job_id": str(open_result.metadata["runtime_job_id"])},
            )
        )
    )
    assert baseline_read.accepted is True
    baseline_delegate = baseline_read.metadata.get("delegate_result")
    assert isinstance(baseline_delegate, dict)
    baseline_cursor = int(baseline_delegate.get("next_cursor", -1))

    pty_adapter._buffers.setdefault(session_id, bytearray()).extend(b"delayed frame from runner\n")
    transport.pump_terminal_frames()
    transport.flush_inbound_events()
    channel_db.commit()

    delayed_read = asyncio.run(
        provider.read_terminal_output(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="read_terminal_output",
                payload={
                    "session_id": session_id,
                    "runtime_job_id": str(open_result.metadata["runtime_job_id"]),
                    "cursor": baseline_cursor,
                },
            )
        )
    )
    assert delayed_read.accepted is True
    delayed_delegate = delayed_read.metadata.get("delegate_result")
    assert isinstance(delayed_delegate, dict)
    assert "delayed frame from runner" in str(delayed_delegate.get("data", ""))

    input_result = asyncio.run(
        provider.send_terminal_input(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="send_terminal_input",
                payload={"session_id": session_id, "data": "whoami\n"},
            )
        )
    )
    assert input_result.accepted is True
    _dispatch_pending()

    resize_result = asyncio.run(
        provider.resize_terminal_session(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="resize_terminal_session",
                payload={"session_id": session_id, "cols": 140, "rows": 42},
            )
        )
    )
    assert resize_result.accepted is True
    _dispatch_pending()

    close_result = asyncio.run(
        provider.close_terminal_session(
            _request(
                task_id=task_id,
                runner_id=runner_id,
                operation="close_terminal_session",
                payload={"session_id": session_id},
            )
        )
    )
    assert close_result.accepted is True
    _dispatch_pending()

    for operation_result, expected_operation in (
        (input_result, "input"),
        (resize_result, "resize"),
        (close_result, "close"),
    ):
        runtime_job = verify_db.execute(
            select(RuntimeJob).where(RuntimeJob.id == UUID(str(operation_result.metadata["runtime_job_id"])))
        ).scalar_one()
        result_json = runtime_job.result_json if isinstance(runtime_job.result_json, dict) else {}
        assert runtime_job.status == "succeeded"
        assert result_json.get("terminal_operation") == expected_operation
        assert result_json.get("session_id") == session_id

    remote_runtime_terminal_envelopes = [
        envelope
        for envelope in transport.envelopes
        if envelope.type in {"terminal.open", "terminal.input", "terminal.resize", "terminal.close"}
    ]
    assert remote_runtime_terminal_envelopes
    assert len({envelope.runtime_job_id for envelope in remote_runtime_terminal_envelopes}) == 4

    def _runtime_job_from_params(envelope) -> str:
        payload = envelope.payload
        if isinstance(payload, dict):
            params = payload.get("params", {})
            if isinstance(params, dict):
                return str(params.get("runtime_job_id") or "")
            return ""
        params = getattr(payload, "params", {})
        if isinstance(params, dict):
            return str(params.get("runtime_job_id") or "")
        return ""

    assert all(
        _runtime_job_from_params(envelope) == str(start_result.metadata["runtime_job_id"])
        for envelope in remote_runtime_terminal_envelopes
    )

    assert len(pty_adapter.open_calls) == 1
    assert pty_adapter.input_calls == [(session_id, "whoami\n")]
    assert pty_adapter.resize_calls == [(session_id, 140, 42)]
    assert pty_adapter.close_calls == [session_id]

    verify_db.close()
    dispatch_db.close()
    channel_db.close()
