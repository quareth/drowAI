"""Tests for Remote Runtime cloud runner runtime-provider dispatch behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.services.runner_control.runtime_job_service import RuntimeJobServiceError
from backend.services.runner_control.terminal_stream_registry import get_runner_terminal_stream_registry
from backend.services.runtime_provider.cloud_runner.terminal.stream_client import (
    CloudRunnerTerminalStreamAttacher,
)
from backend.services.runtime_provider.cloud_runner.tool_commands import (
    ack_waiter as tool_command_ack_waiter,
)
from backend.services.runtime_provider.cloud_runner_provider import CloudRunnerRuntimeProvider
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    build_runtime_result,
    is_pending_runner_operation_result,
)


@dataclass
class _FakeQueuedMessage:
    message_id: str
    status: str = "queued"


class _FakeRuntimeJobService:
    def __init__(self, runtime_job_ids: list[UUID] | None = None) -> None:
        self.created_requests = []
        self.assigned_requests = []
        self.transition_requests = []
        self._runtime_job_ids = list(runtime_job_ids or [uuid4()])
        self.created_runtime_job_ids: list[UUID] = []
        self.runtime_jobs_by_id: dict[object, object] = {}

    def create_runtime_job(self, request):
        runtime_job_id = self._runtime_job_ids[len(self.created_requests)] if len(self.created_requests) < len(self._runtime_job_ids) else uuid4()
        self.created_requests.append(request)
        self.created_runtime_job_ids.append(runtime_job_id)
        return SimpleNamespace(
            id=runtime_job_id,
            status="queued",
            runner_id=None,
        )

    def assign_runtime_job(self, *, tenant_id: int, runtime_job_id: UUID, runner_id: UUID):
        self.assigned_requests.append((tenant_id, runtime_job_id, runner_id))
        return SimpleNamespace(
            id=runtime_job_id,
            status="assigned",
            runner_id=runner_id,
        )

    def transition_runtime_job(
        self,
        *,
        tenant_id: int,
        runtime_job_id: UUID,
        next_status: str,
        payload_json=None,
        result_json=None,
        error_code=None,
        error_message=None,
        lease_expires_at=None,
    ):
        del payload_json, lease_expires_at
        self.transition_requests.append(
            {
                "tenant_id": tenant_id,
                "runtime_job_id": runtime_job_id,
                "next_status": next_status,
                "result_json": result_json,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        runtime_job = self.runtime_jobs_by_id.get(runtime_job_id)
        if runtime_job is None:
            runtime_job = self.runtime_jobs_by_id.get(str(runtime_job_id))
        if runtime_job is None:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_NOT_FOUND",
                message="Runtime job not found.",
            )

        current_status = str(getattr(runtime_job, "status", "") or "").strip().lower()
        if current_status in {"succeeded", "failed", "cancelled", "lost", "expired"}:
            raise RuntimeJobServiceError(
                error_code="RUNTIME_JOB_TRANSITION_STALE",
                message="Runtime job transition is stale because job is already terminal.",
            )

        runtime_job.status = next_status
        if result_json is not None:
            runtime_job.result_json = dict(result_json)
        if error_code is not None:
            runtime_job.error_code = error_code
        if error_message is not None:
            runtime_job.error_message = error_message
        return runtime_job


class _FakeCoordinationStore:
    def __init__(self) -> None:
        self.enqueued = []

    def enqueue_outbound_message(self, **kwargs):
        self.enqueued.append(kwargs)
        return _FakeQueuedMessage(message_id="msg-1", status="queued")


class _FakeSession:
    def __init__(self, runtime_job_service: _FakeRuntimeJobService) -> None:
        self.runtime_job_service = runtime_job_service
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self) -> None:
        self.commits += 1

    def execute(self, _statement):
        start_job_ids = [
            runtime_job_id
            for runtime_job_id, created in zip(
                self.runtime_job_service.created_runtime_job_ids,
                self.runtime_job_service.created_requests,
                strict=False,
            )
            if created.job_type == "task.start"
        ]
        runtime_job_id = start_job_ids[-1] if start_job_ids else None
        return SimpleNamespace(
            scalar_one_or_none=lambda: runtime_job_id,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


class _ScalarResult:
    def __init__(self, *, one=None, many=None) -> None:
        self._one = one
        self._many = list(many or [])

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._many))


class _ToolCommandSession:
    def __init__(
        self,
        *,
        runner,
        runtime_job_service: _FakeRuntimeJobService,
        existing_runtime_job=None,
        command_runtime_jobs=None,
        runtime_job_by_id=None,
        existing_outbound=None,
    ) -> None:
        self.runner = runner
        self.runtime_job_service = runtime_job_service
        self.existing_runtime_job = existing_runtime_job
        self.command_runtime_jobs = list(command_runtime_jobs or [])
        self.runtime_job_by_id = dict(runtime_job_by_id or {})
        self.existing_outbound = existing_outbound
        self.commits = 0

        if existing_runtime_job is not None:
            self.runtime_job_by_id.setdefault(str(existing_runtime_job.id), existing_runtime_job)
        for runtime_job in self.command_runtime_jobs:
            self.runtime_job_by_id.setdefault(str(runtime_job.id), runtime_job)
        runtime_job_service.runtime_jobs_by_id.update(self.runtime_job_by_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self) -> None:
        self.commits += 1

    def execute(self, statement):
        sql_text = str(statement)
        if "FROM runners" in sql_text:
            return _ScalarResult(one=self.runner)
        if "FROM runtime_jobs" in sql_text:
            params = statement.compile().params
            if self.runtime_job_by_id and "idempotency_key_1" not in params:
                runtime_job_id = next(
                    (value for key, value in params.items() if str(key).startswith("id_")),
                    None,
                )
                if runtime_job_id is not None:
                    runtime_job = self.runtime_job_by_id.get(str(runtime_job_id))
                    if runtime_job is None:
                        runtime_job = next(iter(self.runtime_job_by_id.values()))
                    if callable(runtime_job):
                        runtime_job = runtime_job()
                    return _ScalarResult(one=runtime_job)
            if "idempotency_key_1" in params:
                return _ScalarResult(one=self.existing_runtime_job)
            return _ScalarResult(many=self.command_runtime_jobs)
        if "FROM runner_control_messages" in sql_text:
            return _ScalarResult(one=self.existing_outbound)
        return _ScalarResult(one=None)


class _FakeTerminalStream:
    def __init__(self) -> None:
        self.inputs: list[object] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self.session_id = "stream-session-1"

    async def send_input(self, data):
        self.inputs.append(data)

    async def read_output(self, size: int = 4096, timeout: float | None = None):
        del size, timeout
        return b"stream-output"

    async def resize(self, cols: int, rows: int):
        self.resizes.append((cols, rows))

    async def close(self):
        self.closed = True


class _DisconnectedTerminalStream(_FakeTerminalStream):
    def channel_connected(self) -> bool:
        return False


class _RetireSession:
    def __init__(self, *, runner, start_job, runtime_job_service: _FakeRuntimeJobService) -> None:
        self.runner = runner
        self.start_job = start_job
        self.runtime_job_service = runtime_job_service
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self) -> None:
        self.commits += 1

    def execute(self, statement):
        sql_text = str(statement)
        if "FROM runners" in sql_text:
            return _ScalarResult(one=self.runner)
        if "FROM runtime_jobs" in sql_text:
            if "SELECT runtime_jobs.id \nFROM runtime_jobs" in sql_text:
                return _ScalarResult(one=self.start_job.id)
            return _ScalarResult(many=[self.start_job])
        return _ScalarResult(one=None)


def _request(
    operation: str,
    *,
    runner_id: str | None = None,
    execution_site_id: str | None = None,
    payload: dict | None = None,
):
    return RuntimeOperationRequest(
        tenant_id=12,
        task_id=34,
        actor_type=RuntimeActorType.SYSTEM,
        actor_id="scheduler",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id="task-34",
        operation=operation,
        runner_id=runner_id,
        execution_site_id=execution_site_id,
        payload=payload or {},
    )


def _build_provider(*, runtime_job_ids: list[UUID] | None = None):
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=runtime_job_ids)
    session = _FakeSession(runtime_job_service)
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    return provider, session, runtime_job_service, coordination_store


def _patch_active_task_start_runtime_job(monkeypatch, provider, runtime_job) -> None:
    monkeypatch.setattr(
        provider._runtime_job_queries,
        "_find_active_task_start_runtime_job",
        lambda **_kwargs: runtime_job,
    )


def _assert_dispatch_result(
    result,
    *,
    session,
    runtime_job_service,
    coordination_store,
    message_type: str,
) -> dict:
    assert result.provider == "cloud_runner"
    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.ACCEPTED
    assert session.commits == 1
    assert len(runtime_job_service.created_requests) == 1
    assert len(coordination_store.enqueued) == 1

    runtime_job_id = str(runtime_job_service.created_runtime_job_ids[-1])
    outbound = coordination_store.enqueued[0]
    payload_json = outbound["payload_json"]
    assert outbound["message_type"] == message_type
    assert payload_json["runtime_job_id"] == runtime_job_id
    assert payload_json["operation"] == message_type
    assert payload_json["workspace_id"] == "task-34"
    assert isinstance(payload_json["operation_id"], str)
    assert payload_json["operation_id"]
    assert result.metadata["runtime_job_id"] == runtime_job_id
    assert result.metadata["runner_runtime_job_id"] == runtime_job_id
    assert result.metadata["control_message_type"] == message_type
    return outbound


def test_cloud_runner_provider_pending_result_detection_is_provider_name_neutral():
    request = _request("provision_task_runtime", runner_id=str(uuid4()))
    accepted = build_runtime_result(
        request,
        accepted=True,
        provider="managed_runner",
        status=RuntimeOperationStatus.ACCEPTED,
    )
    running = build_runtime_result(
        request,
        accepted=True,
        provider="runner_control",
        status=RuntimeOperationStatus.RUNNING,
    )

    assert is_pending_runner_operation_result(accepted) is True
    assert is_pending_runner_operation_result(running) is True


def test_cloud_runner_provider_reuses_task_start_runtime_identity_for_follow_up_operations():
    start_runtime_job_id = uuid4()
    follow_up_runtime_job_id = uuid4()
    provider, session, runtime_job_service, coordination_store = _build_provider(
        runtime_job_ids=[start_runtime_job_id, follow_up_runtime_job_id]
    )
    runner_id = str(uuid4())

    start_result = asyncio.run(
        provider.provision_task_runtime(
            _request(
                "provision_task_runtime",
                runner_id=runner_id,
                payload={"target": "127.0.0.1"},
            )
        )
    )
    assert start_result.accepted is True
    assert start_result.metadata["runtime_job_id"] == str(start_runtime_job_id)
    assert start_result.metadata["runner_runtime_job_id"] == str(start_runtime_job_id)

    follow_up_result = asyncio.run(
        provider.get_runtime_status(
            _request(
                "get_runtime_status",
                runner_id=runner_id,
                payload={},
            )
        )
    )

    assert follow_up_result.accepted is True
    assert follow_up_result.metadata["runtime_job_id"] == str(follow_up_runtime_job_id)
    assert follow_up_result.metadata["runner_runtime_job_id"] == str(start_runtime_job_id)
    assert session.commits == 2
    assert len(coordination_store.enqueued) == 2
    assert coordination_store.enqueued[1]["runtime_job_id"] == follow_up_runtime_job_id
    assert coordination_store.enqueued[1]["payload_json"]["runtime_job_id"] == str(start_runtime_job_id)
    assert runtime_job_service.created_requests[0].job_type == "task.start"
    assert runtime_job_service.created_requests[1].job_type == "runtime.status"


def test_cloud_runner_provider_retire_without_task_runner_assignment_is_idempotent_when_no_runtime_job():
    provider, session, runtime_job_service, coordination_store = _build_provider()

    result = asyncio.run(
        provider.retire_task_runtime(
            _request("retire_task_runtime", payload={"force": True})
        )
    )

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["mode"] == "already_retired"
    assert session.commits == 0
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_retire_recovers_runner_from_active_task_start_job():
    start_runtime_job_id = uuid4()
    retire_runtime_job_id = uuid4()
    runner_id = uuid4()
    execution_site_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[retire_runtime_job_id])
    start_job = SimpleNamespace(
        id=start_runtime_job_id,
        runner_id=runner_id,
        execution_site_id=execution_site_id,
        payload_json={"workspace_id": "task-34"},
    )
    runner = SimpleNamespace(
        id=runner_id,
        tenant_id=12,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _RetireSession(
        runner=runner,
        start_job=start_job,
        runtime_job_service=runtime_job_service,
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )

    result = asyncio.run(
        provider.retire_task_runtime(
            _request("retire_task_runtime", payload={"force": True})
        )
    )

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.ACCEPTED
    assert runtime_job_service.assigned_requests[0][2] == runner_id
    assert coordination_store.enqueued[0]["runner_id"] == runner_id
    assert coordination_store.enqueued[0]["payload_json"]["runtime_job_id"] == str(start_runtime_job_id)
    assert coordination_store.enqueued[0]["payload_json"]["params"]["runtime_job_id"] == str(start_runtime_job_id)


def test_cloud_runner_provider_enqueues_task_start_message_for_provision():
    provider, session, runtime_job_service, coordination_store = _build_provider()
    runner_id = str(uuid4())
    execution_site_id = str(uuid4())

    result = asyncio.run(
        provider.provision_task_runtime(
            _request(
                "provision_task_runtime",
                runner_id=runner_id,
                execution_site_id=execution_site_id,
                payload={"target": "127.0.0.1"},
            )
        )
    )

    outbound = _assert_dispatch_result(
        result,
        session=session,
        runtime_job_service=runtime_job_service,
        coordination_store=coordination_store,
        message_type="task.start",
    )
    assert runtime_job_service.created_requests[0].job_type == "task.start"
    assert runtime_job_service.assigned_requests[0][2] == UUID(runner_id)
    assert outbound["payload_json"]["params"]["target"] == "127.0.0.1"
    assert result.metadata["runner_id_assigned"] == runner_id
    assert result.metadata["control_message_id"] == "msg-1"
    assert result.metadata["protocol_domain"] == "remote_runtime"


@pytest.mark.parametrize(
    ("method_name", "operation", "message_type", "payload", "expected_params"),
    [
        ("pause_task_runtime", "pause_task_runtime", "task.pause", {"reason": "operator"}, {"reason": "operator"}),
        ("resume_task_runtime", "resume_task_runtime", "task.resume", {"reason": "operator"}, {"reason": "operator"}),
        ("append_runtime_input", "append_runtime_input", "runtime.input", {"text": "continue"}, {"text": "continue"}),
        (
            "retry_vpn_connection",
            "retry_vpn_connection",
            "runtime.vpn.retry",
            {"attempt": 2},
            {"attempt": 2},
        ),
        ("check_vpn_status", "check_vpn_status", "runtime.vpn.status", {}, {}),
        ("get_runtime_status", "get_runtime_status", "runtime.status", {"detail": "full"}, {"detail": "full"}),
        (
            "get_runtime_startup_progress",
            "get_runtime_startup_progress",
            "runtime.startup_progress",
            {},
            {},
        ),
        ("get_runtime_logs", "get_runtime_logs", "runtime.logs", {"tail": 100}, {"tail": 100}),
        ("get_runtime_metrics", "get_runtime_metrics", "runtime.metrics", {"window": "1m"}, {"window": "1m"}),
        (
            "list_runtime_inventory",
            "list_runtime_inventory",
            "runtime.inventory",
            {},
            {"scope": "task", "filters": {}},
        ),
        (
            "cleanup_runtime_workspace",
            "cleanup_runtime_workspace",
            "runtime.workspace.cleanup",
            {},
            {"cleanup_scope": "workspace", "retain_outputs": True},
        ),
        (
            "query_runtime_artifacts",
            "query_runtime_artifacts",
            "runtime.workspace.query",
            {"prefix": "reports"},
            {"prefix": "reports"},
        ),
        (
            "read_runtime_artifact_file",
            "read_runtime_artifact_file",
            "runtime.workspace.read",
            {"path": "reports/a.txt", "binary": True, "max_bytes": 1024},
            {"artifact_path": "reports/a.txt", "binary": True, "max_bytes": 1024},
        ),
        (
            "write_runtime_artifact_file",
            "write_runtime_artifact_file",
            "runtime.workspace.write",
            {"path": "artifacts/out.txt", "content_base64": "b2s="},
            {"artifact_path": "artifacts/out.txt", "content_base64": "b2s=", "encoding": "utf-8"},
        ),
        (
            "write_runtime_artifact_file",
            "write_runtime_artifact_file",
            "runtime.workspace.write",
            {"path": "index/chunks_task-1.jsonl", "content_base64": "b2s=", "mode": "append"},
            {
                "artifact_path": "index/chunks_task-1.jsonl",
                "content_base64": "b2s=",
                "encoding": "utf-8",
                "mode": "append",
            },
        ),
        (
            "read_runtime_environment_metadata",
            "read_runtime_environment_metadata",
            "runtime.environment.metadata",
            {"key": "agent.version"},
            {"action": "read", "key": "agent.version"},
        ),
        (
            "write_runtime_environment_metadata",
            "write_runtime_environment_metadata",
            "runtime.environment.metadata",
            {"key": "agent.version", "value": "1.0.0"},
            {"action": "write", "key": "agent.version", "value": "1.0.0"},
        ),
        (
            "query_runtime_environment_metadata",
            "query_runtime_environment_metadata",
            "runtime.environment.metadata",
            {},
            {"action": "query", "filters": {}},
        ),
        (
            "open_terminal_session",
            "open_terminal_session",
            "terminal.open",
            {},
            {"session_name": "runtime", "cols": 80, "rows": 24},
        ),
        (
            "close_terminal_session",
            "close_terminal_session",
            "terminal.close",
            {"session_id": "sess-1"},
            {"session_id": "sess-1"},
        ),
    ],
)
def test_cloud_runner_provider_runner_surfaces_dispatch_expected_messages(
    method_name: str,
    operation: str,
    message_type: str,
    payload: dict,
    expected_params: dict,
):
    provider, session, runtime_job_service, coordination_store = _build_provider()
    runner_id = str(uuid4())

    method = getattr(provider, method_name)
    result = asyncio.run(method(_request(operation, runner_id=runner_id, payload=payload)))

    outbound = _assert_dispatch_result(
        result,
        session=session,
        runtime_job_service=runtime_job_service,
        coordination_store=coordination_store,
        message_type=message_type,
    )
    for key, expected in expected_params.items():
        assert outbound["payload_json"]["params"][key] == expected


def test_cloud_runner_provider_stop_includes_lifecycle_intent_cancel():
    provider, session, runtime_job_service, coordination_store = _build_provider()
    runner_id = str(uuid4())

    result = asyncio.run(
        provider.stop_task_runtime(
            _request(
                "stop_task_runtime",
                runner_id=runner_id,
                payload={"lifecycle_intent": "cancel"},
            )
        )
    )

    outbound = _assert_dispatch_result(
        result,
        session=session,
        runtime_job_service=runtime_job_service,
        coordination_store=coordination_store,
        message_type="task.stop",
    )
    assert outbound["payload_json"]["params"]["lifecycle_intent"] == "cancel"


def test_cloud_runner_provider_vpn_dispatch_preserves_transport_payload_and_redacts_job_payload():
    provider, session, runtime_job_service, coordination_store = _build_provider()
    runner_id = str(uuid4())
    vpn_payload = {
        "vpn_config": {
            "config_data": "[Interface]\\nPrivateKey=super-secret",
            "file_name": "task-34.ovpn",
        }
    }

    result = asyncio.run(
        provider.materialize_vpn_config(
            _request(
                "materialize_vpn_config",
                runner_id=runner_id,
                payload=vpn_payload,
            )
        )
    )

    outbound = _assert_dispatch_result(
        result,
        session=session,
        runtime_job_service=runtime_job_service,
        coordination_store=coordination_store,
        message_type="runtime.vpn.config",
    )
    assert outbound["payload_json"]["params"]["vpn_config"] == vpn_payload["vpn_config"]
    job_payload = runtime_job_service.created_requests[0].payload_json
    assert job_payload["params"]["vpn_config"] == "<redacted>"
    assert "super-secret" not in str(job_payload)


def test_cloud_runner_provider_materialize_workspace_is_management_plane_noop():
    provider, session, runtime_job_service, coordination_store = _build_provider()

    result = asyncio.run(provider.materialize_runtime_workspace(_request("materialize_runtime_workspace")))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["mode"] == "management_plane_noop"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []
    assert session.commits == 0


@pytest.mark.parametrize(
    ("method_name", "operation", "payload", "missing_fragment"),
    [
        (
            "read_runtime_environment_metadata",
            "read_runtime_environment_metadata",
            {},
            "`key` is required",
        ),
        (
            "write_runtime_environment_metadata",
            "write_runtime_environment_metadata",
            {"value": "x"},
            "`key` is required",
        ),
        (
            "write_runtime_environment_metadata",
            "write_runtime_environment_metadata",
            {"key": "x"},
            "`value` is required",
        ),
        (
            "close_terminal_session",
            "close_terminal_session",
            {},
            "`session_id` is required",
        ),
        (
            "read_runtime_artifact_file",
            "read_runtime_artifact_file",
            {},
            "`path` is required",
        ),
        (
            "write_runtime_artifact_file",
            "write_runtime_artifact_file",
            {"path": "artifacts/out.txt", "content_base64": "b2s=", "mode": "append"},
            "`mode=append` is only supported for index workspace writes.",
        ),
    ],
)
def test_cloud_runner_provider_rejects_invalid_runner_requests(
    method_name: str,
    operation: str,
    payload: dict,
    missing_fragment: str,
):
    provider, _, _, _ = _build_provider()
    runner_id = str(uuid4())

    method = getattr(provider, method_name)
    result = asyncio.run(method(_request(operation, runner_id=runner_id, payload=payload)))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_REMOTE_OPERATION_INVALID_REQUEST"
    assert missing_fragment in (result.error_message or "")


def test_cloud_runner_provider_rejects_runner_operations_without_assignment():
    provider, _, _, _ = _build_provider()

    result = asyncio.run(provider.pause_task_runtime(_request("pause_task_runtime")))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_ASSIGNMENT_REQUIRED"


def test_cloud_runner_provider_rejects_terminal_open_without_assignment_before_dispatch():
    provider, _, runtime_job_service, coordination_store = _build_provider()

    result = asyncio.run(provider.open_terminal_session(_request("open_terminal_session")))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_ASSIGNMENT_REQUIRED"
    assert "assigned runner_id" in str(result.error_message or "")
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_rejects_unsupported_environment_metadata_key():
    provider, _, _, _ = _build_provider()
    runner_id = str(uuid4())

    read_result = asyncio.run(
        provider.read_runtime_environment_metadata(
            _request(
                "read_runtime_environment_metadata",
                runner_id=runner_id,
                payload={"key": "LANG"},
            )
        )
    )
    write_result = asyncio.run(
        provider.write_runtime_environment_metadata(
            _request(
                "write_runtime_environment_metadata",
                runner_id=runner_id,
                payload={"key": "LANG", "value": "C.UTF-8"},
            )
        )
    )

    assert read_result.accepted is False
    assert read_result.status == RuntimeOperationStatus.REJECTED
    assert read_result.error_code == "RUNNER_ENV_METADATA_KEY_UNSUPPORTED"
    assert write_result.accepted is False
    assert write_result.status == RuntimeOperationStatus.REJECTED
    assert write_result.error_code == "RUNNER_ENV_METADATA_KEY_UNSUPPORTED"


def test_cloud_runner_provider_rejects_unsupported_environment_metadata_filters():
    provider, _, _, _ = _build_provider()
    runner_id = str(uuid4())

    unsupported_filter_result = asyncio.run(
        provider.query_runtime_environment_metadata(
            _request(
                "query_runtime_environment_metadata",
                runner_id=runner_id,
                payload={"filters": {"prefix": "agent"}},
            )
        )
    )
    unsupported_prefix_result = asyncio.run(
        provider.query_runtime_environment_metadata(
            _request(
                "query_runtime_environment_metadata",
                runner_id=runner_id,
                payload={"filters": {"key_prefix": "LANG"}},
            )
        )
    )

    assert unsupported_filter_result.accepted is False
    assert unsupported_filter_result.status == RuntimeOperationStatus.REJECTED
    assert unsupported_filter_result.error_code == "RUNNER_ENV_METADATA_FILTER_UNSUPPORTED"
    assert unsupported_prefix_result.accepted is False
    assert unsupported_prefix_result.status == RuntimeOperationStatus.REJECTED
    assert unsupported_prefix_result.error_code == "RUNNER_ENV_METADATA_FILTER_UNSUPPORTED"


@pytest.mark.parametrize(
    ("method_name", "operation", "expected_error_code"),
    [
        ("execute_runtime_command", "execute_runtime_command", "RUNNER_REMOTE_OPERATION_LOCAL_ONLY"),
        ("dispatch_tool_execution", "dispatch_tool_execution", "RUNNER_TOOL_COMMAND_COMPATIBILITY_ONLY"),
    ],
)
def test_cloud_runner_provider_deferred_and_local_only_surfaces_fail_closed(
    method_name: str,
    operation: str,
    expected_error_code: str,
):
    provider, _, _, _ = _build_provider()

    method = getattr(provider, method_name)
    result = asyncio.run(method(_request(operation)))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == expected_error_code


def test_cloud_runner_provider_dispatch_tool_execution_rejects_callable_payload() -> None:
    provider, _, _, _ = _build_provider()

    result = asyncio.run(
        provider.dispatch_tool_execution(
            _request(
                "dispatch_tool_execution",
                payload={"dispatch_callable": lambda _request: None},
            )
        )
    )

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_CALLABLE_UNSUPPORTED"
    assert "send_tool_command" in str(result.error_message or "")


def test_cloud_runner_provider_send_tool_command_rejects_when_feature_flag_disabled(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "0")
    provider, _, _, _ = _build_provider()
    runner_id = str(uuid4())

    result = asyncio.run(
        provider.send_tool_command(
            _request(
                "send_tool_command",
                runner_id=runner_id,
                payload={
                    "tool": "shell.exec",
                    "command_id": "cmd-1",
                    "command": "id",
                    "timeout_seconds": 10,
                },
            )
        )
    )

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_DISABLED"


def test_cloud_runner_provider_send_tool_command_rejects_when_runner_tool_capability_missing(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runtime_job_service = _FakeRuntimeJobService()
    runner = SimpleNamespace(id=uuid4(), capabilities_json=["tooling_plane.commands.v1"])
    session = _ToolCommandSession(runner=runner, runtime_job_service=runtime_job_service)
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner.id),
        payload={"tool": "shell.exec", "command_id": "cmd-missing-tool-cap", "command": "id"},
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_CAPABILITY_MISSING"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_when_channel_capability_missing(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runtime_job_service = _FakeRuntimeJobService()
    runner = SimpleNamespace(id=uuid4(), capabilities_json=["tool_command.v1"])
    session = _ToolCommandSession(runner=runner, runtime_job_service=runtime_job_service)
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner.id),
        payload={"tool": "shell.exec", "command_id": "cmd-missing-channel-cap", "command": "id"},
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_PROTOCOL_CAPABILITY_MISSING"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_non_container_lane(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, _, _ = _build_provider()
    runner_id = str(uuid4())
    request = _request(
        "send_tool_command",
        runner_id=runner_id,
        payload={
            "tool": "knowledge.cve_lookup",
            "command_id": "cmd-2",
            "command": "cve-lookup CVE-2024-1234",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "backend_scoped", "authority": "backend_direct"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_LANE_UNSUPPORTED"


def test_cloud_runner_provider_send_tool_command_rejects_missing_lane_dispatch_metadata(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, _, _ = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "knowledge.cve_lookup",
            "command_id": "cmd-missing-lane-metadata",
            "command": "cve-lookup CVE-2024-1234",
            "timeout_seconds": 10,
        },
    )

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_ROUTE_METADATA_REQUIRED"


def test_cloud_runner_provider_send_tool_command_rejects_lane_tool_mismatch(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, _, _ = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "knowledge.cve_lookup",
            "command_id": "cmd-lane-mismatch",
            "command": "cve-lookup CVE-2024-1234",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_LANE_UNSUPPORTED"
    assert result.metadata["selected_lane"] == "container_scoped"
    assert result.metadata["canonical_lane"] == "backend_scoped"
    assert result.metadata["tool"] == "knowledge.cve_lookup"


def test_cloud_runner_provider_send_tool_command_rejects_non_runner_runtime_placement(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-local-placement",
            "command": "id",
            "timeout_seconds": 10,
        },
    )
    request.runtime_placement_mode = RuntimePlacementMode.LOCAL
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_RUNTIME_PLACEMENT_UNSUPPORTED"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_secret_bearing_env(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    runner_id = str(uuid4())
    request = _request(
        "send_tool_command",
        runner_id=runner_id,
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-3",
            "command": "id",
            "env": {"api_key": "sk-secret"},
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "SECRET_BEARING_ENV_UNSUPPORTED"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_legacy_args(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-legacy-args",
            "command": "id",
            "args": {"command": "id"},
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_REMOTE_OPERATION_INVALID_REQUEST"
    assert "`args` is not accepted" in (result.error_message or "")
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


@pytest.mark.parametrize(
    "command_payload",
    [
        None,
        "",
        "   ",
    ],
)
def test_cloud_runner_provider_send_tool_command_rejects_missing_or_empty_command(
    monkeypatch,
    command_payload,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    request_payload = {
        "tool": "shell.exec",
        "command_id": "cmd-invalid-args",
        "timeout_seconds": 10,
    }
    if command_payload is not None:
        request_payload["command"] = command_payload
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload=request_payload,
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_REMOTE_OPERATION_INVALID_REQUEST"
    assert "`command` is required" in (result.error_message or "")
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_secret_bearing_params(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-secret-params",
            "command": "id",
            "params": {"secret_ref": "vault://tooling-plane/secret"},
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "SECRET_BEARING_PARAMS_UNSUPPORTED"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


@pytest.mark.parametrize(
    "identity_key",
    [
        "runtime_job_id",
        "runner_runtime_job_id",
        "task_runtime_job_id",
        "tool_command_runtime_job_id",
    ],
)
def test_cloud_runner_provider_send_tool_command_rejects_runtime_identity_params(
    monkeypatch,
    identity_key: str,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    provider, _, runtime_job_service, coordination_store = _build_provider()
    request = _request(
        "send_tool_command",
        runner_id=str(uuid4()),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-runtime-id-params",
            "command": "id",
            "params": {identity_key: "injected-value"},
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_PARAMS_IDENTITY_UNSUPPORTED"
    assert result.metadata["rejected_param_keys"] == [identity_key]
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_rejects_missing_active_task_runtime_identity(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runtime_job_service = _FakeRuntimeJobService()
    runner = SimpleNamespace(
        id=uuid4(),
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(runner=runner, runtime_job_service=runtime_job_service)
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(monkeypatch, provider, None)

    request = _request(
        "send_tool_command",
        runner_id=str(runner.id),
        payload={"tool": "shell.exec", "command_id": "cmd-no-task-runtime", "command": "id"},
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TASK_RUNTIME_REQUIRED"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_enqueues_tooling_plane_tool_command(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    acked_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="acknowledged",
        result_json={"source": "runner_ack", "ack_status": "accepted"},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): acked_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-1",
            "tool_call_id": "tool-call-1",
            "tool_batch_id": "batch-1",
            "command": "id",
            "timeout_seconds": 12.5,
            "timeout_policy": {"deadline_seconds": 12.5, "grace_seconds": 2},
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.ACCEPTED
    assert len(runtime_job_service.created_requests) == 1
    created_request = runtime_job_service.created_requests[0]
    assert created_request.job_type == "tool.command"
    assert created_request.payload_json["tool"] == "shell.exec"
    assert created_request.payload_json["command_id"] == "tool-call-1"
    assert created_request.payload_json["task_runtime_job_id"] == str(task_runtime_job_id)
    assert created_request.payload_json["command"] == "id"
    assert created_request.payload_json["timeout_policy"] == {
        "deadline_seconds": 12.5,
        "grace_seconds": 2,
    }
    assert created_request.payload_json["route_policy"] == {
        "selected_lane": "container_scoped",
        "selected_authority": "container_runner_transport",
    }
    assert created_request.payload_json["delivery_policy"] == {}
    assert len(coordination_store.enqueued) == 1
    outbound = coordination_store.enqueued[0]
    assert outbound["message_type"] == "tool.command"
    assert outbound["runtime_job_id"] == tool_command_runtime_job_id
    assert outbound["payload_json"]["command_id"] == "tool-call-1"
    assert outbound["payload_json"]["task_runtime_job_id"] == str(task_runtime_job_id)
    assert outbound["payload_json"]["workspace_id"] == "task-34"
    assert outbound["payload_json"]["tool"] == "shell.exec"
    assert outbound["payload_json"]["timeout_seconds"] == 12.5
    assert outbound["payload_json"]["timeout_policy"] == {
        "deadline_seconds": 12.5,
        "grace_seconds": 2,
    }
    assert outbound["payload_json"]["route_policy"] == {
        "selected_lane": "container_scoped",
        "selected_authority": "container_runner_transport",
    }
    assert outbound["payload_json"]["delivery_policy"] == {}
    assert result.metadata["runtime_job_id"] == str(tool_command_runtime_job_id)
    assert result.metadata["task_runtime_job_id"] == str(task_runtime_job_id)
    assert result.metadata["command_id"] == "tool-call-1"
    assert result.metadata["message_id"] == "msg-1"
    assert result.metadata["runner_id"] == str(runner_id)


def test_cloud_runner_provider_send_tool_command_propagates_pty_transport_params(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    acked_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="acknowledged",
        result_json={"source": "runner_ack", "ack_status": "accepted"},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): acked_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-pty",
            "tool_call_id": "tool-call-pty",
            "command": "id",
            "transport": "pty",
            "session_name": "cloud_call",
            "cleanup_session": True,
            "artifact_stamp": 123,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    created_request = runtime_job_service.created_requests[0]
    assert created_request.payload_json["params"] == {
        "transport": "pty",
        "session_name": "cloud_call",
        "cleanup_session": True,
        "artifact_stamp": 123,
    }
    outbound = coordination_store.enqueued[0]
    assert outbound["payload_json"]["params"] == created_request.payload_json["params"]


@pytest.mark.parametrize("offline_mode", ["queue", "fail"])
def test_cloud_runner_provider_send_tool_command_propagates_delivery_policy(monkeypatch, offline_mode: str):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    acked_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="acknowledged",
        result_json={"source": "runner_ack", "ack_status": "accepted"},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): acked_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )
    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": f"cmd-{offline_mode}",
            "command": "id",
            "delivery_policy": {
                "offline": offline_mode,
                "max_attempts": 3,
                "timeout_seconds": 2.5,
            },
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    created_request = runtime_job_service.created_requests[0]
    assert created_request.payload_json["delivery_policy"] == {
        "offline": offline_mode,
        "max_attempts": 3,
        "timeout_seconds": 2.5,
    }
    outbound = coordination_store.enqueued[0]
    assert outbound["payload_json"]["delivery_policy"] == {
        "offline": offline_mode,
        "max_attempts": 3,
        "timeout_seconds": 2.5,
    }


def test_cloud_runner_provider_send_tool_command_reuses_terminal_command_identity(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    existing_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService()
    existing_runtime_job = SimpleNamespace(
        id=existing_runtime_job_id,
        task_id=34,
        runner_id=runner_id,
        status="failed",
        payload_json={
            "command_id": "cmd-retry",
            "task_runtime_job_id": str(task_runtime_job_id),
            "workspace_id": "task-34",
            "operation_id": "persisted-op-1",
            "runtime_image": "ghcr.io/drowai/kali:tooling-plane",
        },
        result_json={"source": "runner_ack", "ack_status": "rejected"},
        error_code="RUNNER_ACK_FAILED",
        error_message="Runner reported message acknowledgment failure.",
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        existing_runtime_job=existing_runtime_job,
        existing_outbound=SimpleNamespace(message_id="msg-existing-tool-command"),
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-retry",
            "command": "id",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "RUNNER_ACK_FAILED"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []
    assert result.metadata["runtime_job_id"] == str(existing_runtime_job_id)
    assert result.metadata["task_runtime_job_id"] == str(task_runtime_job_id)
    assert result.metadata["command_id"] == "cmd-retry"
    assert result.metadata["operation_id"] == "persisted-op-1"
    assert result.metadata["runtime_image"] == "ghcr.io/drowai/kali:tooling-plane"
    assert result.metadata["message_id"] == "msg-existing-tool-command"
    assert result.metadata["runner_id"] == str(runner_id)
    assert result.metadata["control_message_id"] == "msg-existing-tool-command"
    assert result.metadata["runner_ack"]["source"] == "runner_ack"
    assert result.metadata["runner_ack"]["ack_status"] == "rejected"


@pytest.mark.parametrize(
    ("existing_task_id", "existing_runner_id", "existing_workspace_id", "existing_task_runtime_job_id"),
    [
        (34, "same", "task-34", "different"),
        (34, "same", "different", "same"),
        (999, "same", "task-34", "same"),
        (34, "different", "task-34", "same"),
    ],
)
def test_cloud_runner_provider_send_tool_command_rejects_cross_binding_command_id_reuse(
    monkeypatch,
    existing_task_id: int,
    existing_runner_id: str,
    existing_workspace_id: str,
    existing_task_runtime_job_id: str,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService()
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )

    payload_task_runtime_job_id = (
        str(task_runtime_job_id) if existing_task_runtime_job_id == "same" else str(uuid4())
    )
    payload_workspace_id = "task-34" if existing_workspace_id == "task-34" else "task-99"
    payload_runner_id = runner_id if existing_runner_id == "same" else uuid4()
    existing_runtime_job = SimpleNamespace(
        id=uuid4(),
        task_id=existing_task_id,
        runner_id=payload_runner_id,
        status="assigned",
        payload_json={
            "command_id": "cmd-conflict",
            "task_runtime_job_id": payload_task_runtime_job_id,
            "workspace_id": payload_workspace_id,
        },
        result_json={},
        error_code=None,
        error_message=None,
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        command_runtime_jobs=[existing_runtime_job],
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-conflict",
            "command": "id",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TOOL_COMMAND_BINDING_CONFLICT"
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_returns_failed_when_runner_ack_rejects(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    failed_ack_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="failed",
        result_json={"source": "runner_ack", "ack_status": "rejected"},
        error_code="RUNNER_ACK_FAILED",
        error_message="Runner reported message acknowledgment failure.",
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): failed_ack_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-ack-failed",
            "command": "id",
            "timeout_seconds": 12.5,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "RUNNER_ACK_FAILED"
    assert result.metadata["runtime_job_id"] == str(tool_command_runtime_job_id)
    assert result.metadata["runtime_job_status"] == "failed"
    assert result.metadata["runner_ack"]["source"] == "runner_ack"
    assert result.metadata["runner_ack"]["ack_status"] == "rejected"


def test_cloud_runner_provider_send_tool_command_returns_pending_when_ack_not_received(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    pending_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="dispatched",
        result_json={},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): pending_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-no-ack",
            "command": "id",
            "timeout_seconds": 12.5,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["ack_wait_timeout_seconds"] = 0.0

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.ACCEPTED
    assert result.error_code is None
    assert result.metadata["runtime_job_id"] == str(tool_command_runtime_job_id)
    assert result.metadata["runtime_job_status"] == "dispatched"
    assert result.metadata["ack_wait_timed_out"] is True
    assert len(coordination_store.enqueued) == 1


def test_cloud_runner_provider_send_tool_command_reuses_pending_command_identity_and_recovers_missing_outbound(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    existing_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService()
    existing_runtime_job = SimpleNamespace(
        id=existing_runtime_job_id,
        task_id=34,
        runner_id=runner_id,
        status="assigned",
        payload_json={
            "command_id": "cmd-retry-missing-outbound",
            "task_runtime_job_id": str(task_runtime_job_id),
            "workspace_id": "task-34",
            "operation_id": "persisted-op-retry",
            "runtime_image": "ghcr.io/drowai/kali:tooling-plane",
            "tool": "shell.exec",
        },
        result_json={},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        existing_runtime_job=existing_runtime_job,
        runtime_job_by_id={str(existing_runtime_job_id): existing_runtime_job},
        existing_outbound=None,
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-retry-missing-outbound",
            "command": "id",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["ack_wait_timeout_seconds"] = 0.0

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.ACCEPTED
    assert result.metadata["runtime_job_id"] == str(existing_runtime_job_id)
    assert result.metadata["runtime_job_status"] == "assigned"
    assert result.metadata["ack_wait_timed_out"] is True
    assert runtime_job_service.created_requests == []
    assert len(coordination_store.enqueued) == 1
    assert coordination_store.enqueued[0]["runtime_job_id"] == existing_runtime_job_id


def test_cloud_runner_provider_send_tool_command_reuses_pending_command_identity_without_duplicate_enqueue(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    existing_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService()
    existing_runtime_job = SimpleNamespace(
        id=existing_runtime_job_id,
        task_id=34,
        runner_id=runner_id,
        status="assigned",
        payload_json={
            "command_id": "cmd-retry-pending",
            "task_runtime_job_id": str(task_runtime_job_id),
            "workspace_id": "task-34",
            "operation_id": "persisted-op-pending",
            "runtime_image": "ghcr.io/drowai/kali:tooling-plane",
            "tool": "shell.exec",
        },
        result_json={},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        existing_runtime_job=existing_runtime_job,
        runtime_job_by_id={str(existing_runtime_job_id): existing_runtime_job},
        existing_outbound=SimpleNamespace(message_id="msg-existing-pending-tool-command"),
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-retry-pending",
            "command": "id",
            "timeout_seconds": 10,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["ack_wait_timeout_seconds"] = 0.0

    first = asyncio.run(provider.send_tool_command(request))
    second = asyncio.run(provider.send_tool_command(request))

    assert first.accepted is True
    assert first.status == RuntimeOperationStatus.ACCEPTED
    assert first.metadata["runtime_job_id"] == str(existing_runtime_job_id)
    assert first.metadata["runtime_job_status"] == "assigned"
    assert first.metadata["ack_wait_timed_out"] is True
    assert second.accepted is True
    assert second.status == RuntimeOperationStatus.ACCEPTED
    assert second.metadata["runtime_job_id"] == str(existing_runtime_job_id)
    assert second.metadata["runtime_job_status"] == "assigned"
    assert second.metadata["ack_wait_timed_out"] is True
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_send_tool_command_waits_for_tool_result_success(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    succeeded_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="succeeded",
        result_json={
            "source": "runner_event",
            "status": "succeeded",
            "success": True,
            "exit_code": 0,
            "stdout": "uid=0(root) gid=0(root)",
            "stderr": "",
            "artifacts": ["artifacts/cmd-1/stdout.txt"],
            "command_id": "tool-call-success",
            "tool": "shell.exec",
            "result": {"duration_seconds": 0.4},
            "metadata": {"runtime_ms": 400},
            "operation_id": "tool-op-1",
        },
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): succeeded_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-success",
            "command": "id",
            "timeout_seconds": 12.5,
            "timeout_policy": {"deadline_seconds": 0.01, "grace_seconds": 0.01},
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["wait_for_result"] = True

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["runtime_job_id"] == str(tool_command_runtime_job_id)
    delegate = result.metadata["delegate_result"]
    assert delegate["success"] is True
    assert delegate["status"] == "succeeded"
    assert delegate["stdout"] == "uid=0(root) gid=0(root)"
    assert delegate["exit_code"] == 0
    assert delegate["artifacts"] == ["artifacts/cmd-1/stdout.txt"]
    assert delegate["result"] == {"duration_seconds": 0.4}


def test_cloud_runner_provider_send_tool_command_waits_for_ack_failure_terminal_result(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    failed_ack_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="failed",
        result_json={"source": "runner_ack", "ack_status": "rejected"},
        error_code="RUNNER_ACK_FAILED",
        error_message="Runner rejected tool.command delivery.",
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): failed_ack_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-ack-failed-wait",
            "command": "id",
            "timeout_seconds": 12.5,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["wait_for_result"] = True

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "RUNNER_ACK_FAILED"
    delegate = result.metadata["delegate_result"]
    assert delegate["success"] is False
    assert delegate["status"] == "failed"
    assert delegate["error_code"] == "RUNNER_ACK_FAILED"


def test_cloud_runner_provider_send_tool_command_waits_for_failed_tool_result_projection(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    failed_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="failed",
        result_json={
            "source": "runner_event",
            "status": "failed",
            "success": False,
            "exit_code": 23,
            "stdout": "",
            "stderr": "permission denied",
            "artifacts": ["artifacts/cmd-2/stderr.txt"],
            "command_id": "tool-call-failed",
            "tool": "shell.exec",
            "error_code": "TOOL_FAILED",
            "error_message": "Tool execution failed.",
            "result": {"duration_seconds": 0.9},
            "metadata": {"runner_status": "failed"},
            "operation_id": "tool-op-2",
        },
        error_code="TOOL_FAILED",
        error_message="Tool execution failed.",
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): failed_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-failed",
            "command": "id",
            "timeout_seconds": 12.5,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["wait_for_result"] = True

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "TOOL_FAILED"
    assert result.error_message == "Tool execution failed."
    delegate = result.metadata["delegate_result"]
    assert delegate["status"] == "failed"
    assert delegate["success"] is False
    assert delegate["error_code"] == "TOOL_FAILED"
    assert delegate["error_message"] == "Tool execution failed."
    assert delegate["exit_code"] == 23
    assert delegate["stderr"] == "permission denied"


def test_cloud_runner_provider_send_tool_command_wait_for_result_times_out(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    pending_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="accepted",
        result_json={},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): pending_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-result-timeout",
            "command": "id",
            "timeout_seconds": 12.5,
            "timeout_policy": {"deadline_seconds": 0.0, "grace_seconds": 0.0},
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["ack_wait_timeout_seconds"] = 0.0
    request.metadata["wait_for_result"] = True

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "TOOL_RESULT_TIMEOUT"
    assert result.metadata["delegate_result"]["status"] == "timed_out"
    assert result.metadata["wait_timeout_seconds"] == 0.0
    assert pending_runtime_job.status == "failed"
    assert pending_runtime_job.error_code == "TOOL_RESULT_TIMEOUT"
    assert pending_runtime_job.result_json["status"] == "timed_out"

    with pytest.raises(RuntimeJobServiceError) as late_result_error:
        runtime_job_service.transition_runtime_job(
            tenant_id=12,
            runtime_job_id=tool_command_runtime_job_id,
            next_status="succeeded",
            result_json={"status": "succeeded", "success": True, "command_id": "tool-call-result-timeout"},
            error_code=None,
            error_message=None,
        )
    assert late_result_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"


def test_cloud_runner_provider_send_tool_command_reports_artifact_upload_timeout(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    pending_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="accepted",
        result_json={
            "command_id": "tool-call-artifact-timeout",
            "tool": "shell.exec",
            "status": "succeeded",
            "success": True,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "artifacts": ["artifacts/out.txt"],
            "metadata": {
                "artifact_manifest": {
                    "status": "ready_for_upload_request",
                    "declared_count": 1,
                    "accepted_count": 1,
                }
            },
        },
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): pending_runtime_job},
    )
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: _FakeCoordinationStore(),
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-artifact-timeout",
            "command": "id",
            "timeout_seconds": 12.5,
            "timeout_policy": {"deadline_seconds": 0.0, "grace_seconds": 0.0},
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["ack_wait_timeout_seconds"] = 0.0
    request.metadata["wait_for_result"] = True

    result = asyncio.run(provider.send_tool_command(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "RUNNER_ARTIFACT_UPLOAD_TIMEOUT"
    assert result.metadata["delegate_result"]["error_code"] == "RUNNER_ARTIFACT_UPLOAD_TIMEOUT"
    assert result.metadata["delegate_result"]["artifacts"] == ["artifacts/out.txt"]
    assert pending_runtime_job.status == "failed"
    assert pending_runtime_job.error_code == "RUNNER_ARTIFACT_UPLOAD_TIMEOUT"


def test_cloud_runner_provider_send_tool_command_wait_for_result_respects_shared_timeout_budget(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    tool_command_runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[tool_command_runtime_job_id])
    pending_runtime_job = SimpleNamespace(
        id=tool_command_runtime_job_id,
        status="dispatched",
        result_json={},
        error_code=None,
        error_message=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
    )
    session = _ToolCommandSession(
        runner=runner,
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(tool_command_runtime_job_id): pending_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )
    current_time = 1000.0

    def fake_monotonic() -> float:
        return current_time

    async def fake_sleep(delay: float) -> None:
        nonlocal current_time
        current_time += delay

    monkeypatch.setattr(tool_command_ack_waiter, "monotonic", fake_monotonic)
    monkeypatch.setattr(tool_command_ack_waiter.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(provider._tool_commands._result_waiter, "_monotonic", fake_monotonic)

    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "tool-call-shared-timeout-budget",
            "command": "id",
            "timeout_seconds": 12.5,
            "timeout_policy": {"deadline_seconds": 0.2, "grace_seconds": 0.1},
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["wait_for_result"] = True

    started_at = fake_monotonic()
    result = asyncio.run(provider.send_tool_command(request))
    elapsed = fake_monotonic() - started_at

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "TOOL_RESULT_TIMEOUT"
    assert elapsed == pytest.approx(0.3)
    assert elapsed <= 0.31


def test_cloud_runner_provider_send_tool_command_cancellation_returns_cancelled_result(monkeypatch):
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")
    runner_id = uuid4()
    task_runtime_job_id = uuid4()
    runtime_job_id = uuid4()
    runtime_job_service = _FakeRuntimeJobService(runtime_job_ids=[runtime_job_id])
    pending_runtime_job = SimpleNamespace(
        id=runtime_job_id,
        status="accepted",
        result_json={},
        error_code=None,
        error_message=None,
    )
    session = _ToolCommandSession(
        runner=SimpleNamespace(
            id=runner_id,
            capabilities_json=["tool_command.v1", "tooling_plane.commands.v1"],
        ),
        runtime_job_service=runtime_job_service,
        runtime_job_by_id={str(runtime_job_id): pending_runtime_job},
    )
    coordination_store = _FakeCoordinationStore()
    provider = CloudRunnerRuntimeProvider(
        session_factory=lambda: session,
        runtime_job_service_factory=lambda _db: runtime_job_service,
        coordination_store_factory=lambda _db: coordination_store,
    )
    _patch_active_task_start_runtime_job(
        monkeypatch,
        provider,
        SimpleNamespace(id=task_runtime_job_id),
    )
    request = _request(
        "send_tool_command",
        runner_id=str(runner_id),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-cancel",
            "command": "id",
            "timeout_seconds": 5.0,
        },
    )
    request.metadata["lane_dispatch"] = {"lane": "container_scoped", "authority": "container_runner_transport"}
    request.metadata["wait_for_result"] = True

    async def _run_and_cancel():
        waiter_task = asyncio.create_task(
            provider.send_tool_command(request)
        )
        await asyncio.sleep(0)
        waiter_task.cancel()
        return await waiter_task

    result = asyncio.run(_run_and_cancel())

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "TOOL_RESULT_CANCELLED"
    assert result.metadata["delegate_result"]["status"] == "cancelled"
    assert pending_runtime_job.status == "cancelled"
    assert pending_runtime_job.error_code == "TOOL_RESULT_CANCELLED"

    with pytest.raises(RuntimeJobServiceError) as late_result_error:
        runtime_job_service.transition_runtime_job(
            tenant_id=12,
            runtime_job_id=runtime_job_id,
            next_status="succeeded",
            result_json={"status": "succeeded", "success": True, "command_id": "cmd-cancel"},
            error_code=None,
            error_message=None,
        )
    assert late_result_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"


def test_cloud_runner_provider_read_terminal_output_requires_session_id_without_stream() -> None:
    provider, _, _, _ = _build_provider()

    result = asyncio.run(provider.read_terminal_output(_request("read_terminal_output")))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_REMOTE_OPERATION_INVALID_REQUEST"
    assert "`session_id` is required" in str(result.error_message or "")


def test_cloud_runner_provider_read_terminal_output_uses_frame_buffer_without_stream() -> None:
    provider, _, _, _ = _build_provider()

    result = asyncio.run(
        provider.read_terminal_output(
            _request(
                "read_terminal_output",
                payload={"session_id": "sess-1", "cursor": 0, "size": 4096},
            )
        )
    )

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["delegate_result"]["session_id"] == "sess-1"
    assert result.metadata["delegate_result"]["success"] is True


def test_cloud_runner_provider_stream_terminal_io_bypasses_runtime_jobs() -> None:
    provider, _session, runtime_job_service, coordination_store = _build_provider()
    stream = _FakeTerminalStream()

    input_result = asyncio.run(
        provider.send_terminal_input(
            _request("send_terminal_input", payload={"socket": stream, "data": "pwd\n"})
        )
    )
    read_result = asyncio.run(
        provider.read_terminal_output(
            _request("read_terminal_output", payload={"socket": stream, "size": 128, "timeout": 0})
        )
    )
    resize_result = asyncio.run(
        provider.resize_terminal_session(
            _request("resize_terminal_session", payload={"socket": stream, "cols": 120, "rows": 40})
        )
    )

    assert input_result.status == RuntimeOperationStatus.SUCCEEDED
    assert read_result.metadata["delegate_result"]["data"] == b"stream-output"
    assert resize_result.status == RuntimeOperationStatus.SUCCEEDED
    assert stream.inputs == ["pwd\n"]
    assert stream.resizes == [(120, 40)]
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


@pytest.mark.parametrize(
    ("method_name", "operation", "payload"),
    [
        ("send_terminal_input", "send_terminal_input", {"data": "x"}),
        ("resize_terminal_session", "resize_terminal_session", {"cols": 90}),
    ],
)
def test_cloud_runner_provider_sessionless_terminal_actions_fail_before_dispatch(
    method_name: str,
    operation: str,
    payload: dict,
) -> None:
    provider, _session, runtime_job_service, coordination_store = _build_provider()

    result = asyncio.run(getattr(provider, method_name)(_request(operation, payload=payload)))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_REMOTE_OPERATION_INVALID_REQUEST"
    assert "`session_id` is required" in str(result.error_message or "")
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_provider_stream_terminal_input_rejects_when_channel_disconnected() -> None:
    provider, _session, runtime_job_service, coordination_store = _build_provider()
    stream = _DisconnectedTerminalStream()

    result = asyncio.run(
        provider.send_terminal_input(
            _request(
                "send_terminal_input",
                runner_id=str(uuid4()),
                payload={"socket": stream, "data": "pwd\n"},
            )
        )
    )

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TERMINAL_STREAM_UNAVAILABLE"
    assert stream.inputs == []
    assert runtime_job_service.created_requests == []
    assert coordination_store.enqueued == []


def test_cloud_runner_terminal_stream_attacher_rejects_without_live_channel() -> None:
    runtime_job_service = _FakeRuntimeJobService()
    runner_id = uuid4()
    runner = SimpleNamespace(id=runner_id, capabilities_json=["terminal_stream_v1"])
    session = _ToolCommandSession(runner=runner, runtime_job_service=runtime_job_service)
    attacher = CloudRunnerTerminalStreamAttacher(
        session_factory=lambda: session,
        provider_name="cloud_runner",
    )
    request = _request("open_terminal_session", runner_id=str(runner_id))
    result = build_runtime_result(
        request,
        accepted=True,
        provider="cloud_runner",
        status=RuntimeOperationStatus.ACCEPTED,
        metadata={
            "delegate_result": {
                "session_id": "sess-1",
                "runtime_job_id": "runtime-1",
            }
        },
    )

    updated = attacher._attach_terminal_stream_client(request=request, result=result)

    assert updated is not result
    assert updated.accepted is False
    assert updated.status == RuntimeOperationStatus.REJECTED
    assert updated.error_code == "RUNNER_TERMINAL_STREAM_UNAVAILABLE"
    assert "channel is not connected" in str(updated.error_message)


def test_cloud_runner_terminal_stream_attacher_attaches_when_channel_is_live() -> None:
    runtime_job_service = _FakeRuntimeJobService()
    runner_id = uuid4()
    runner = SimpleNamespace(id=runner_id, capabilities_json=["terminal_stream_v1"])
    session = _ToolCommandSession(runner=runner, runtime_job_service=runtime_job_service)
    attacher = CloudRunnerTerminalStreamAttacher(
        session_factory=lambda: session,
        provider_name="cloud_runner",
    )
    registry = get_runner_terminal_stream_registry()

    async def _send(_envelope):
        return None

    registry.register_channel(tenant_id=12, runner_id=runner_id, sender=_send)
    try:
        request = _request("open_terminal_session", runner_id=str(runner_id))
        result = build_runtime_result(
            request,
            accepted=True,
            provider="cloud_runner",
            status=RuntimeOperationStatus.ACCEPTED,
            metadata={
                "delegate_result": {
                    "session_id": "sess-1",
                    "runtime_job_id": "runtime-1",
                }
            },
        )

        updated = attacher._attach_terminal_stream_client(request=request, result=result)
    finally:
        registry.unregister_channel(tenant_id=12, runner_id=runner_id)

    assert updated is not result
    assert updated.metadata["stream_mode"] is True
    assert updated.metadata["delegate_result"]["stream_mode"] is True
    assert updated.metadata["delegate_result"]["socket"].session_id == "sess-1"


def test_cloud_runner_provider_waited_terminal_result_rejects_mismatched_session_id(monkeypatch) -> None:
    runtime_job_id = uuid4()
    runner_id = str(uuid4())

    runtime_job = SimpleNamespace(
        status="succeeded",
        result_json={
            "terminal_operation": "close",
            "session_id": "unexpected-session",
            "sequence": 3,
            "result": {},
        },
        error_code=None,
        error_message=None,
    )

    class _TerminalResultSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: runtime_job)

    provider = CloudRunnerRuntimeProvider(session_factory=lambda: _TerminalResultSession())

    def _fake_dispatch_remote_operation(**_kwargs):
        return SimpleNamespace(
            accepted=True,
            metadata={
                "runtime_job_id": str(runtime_job_id),
                "runtime_job_status": "accepted",
            },
        )

    monkeypatch.setattr(
        provider._remote_dispatcher,
        "_dispatch_remote_operation",
        _fake_dispatch_remote_operation,
    )
    request = _request(
        "close_terminal_session",
        runner_id=runner_id,
        payload={"session_id": "expected-session"},
    )
    request.metadata["wait_for_result"] = True
    request.metadata["wait_timeout_seconds"] = 0.1

    result = asyncio.run(provider.close_terminal_session(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_TERMINAL_RESULT_MISMATCH"
    assert "expected-session" in str(result.error_message or "")


def test_cloud_runner_provider_waited_runtime_result_returns_delegate_payload(monkeypatch) -> None:
    runtime_job_id = uuid4()
    runner_id = str(uuid4())

    runtime_job = SimpleNamespace(
        status="succeeded",
        result_json={
            "message_type": "runtime.status",
            "result": {"container_status": "running", "job_status": "running"},
        },
        error_code=None,
        error_message=None,
    )

    class _RuntimeResultSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: runtime_job)

    provider = CloudRunnerRuntimeProvider(session_factory=lambda: _RuntimeResultSession())

    def _fake_dispatch_remote_operation(**_kwargs):
        return SimpleNamespace(
            accepted=True,
            metadata={
                "runtime_job_id": str(runtime_job_id),
                "runtime_job_status": "accepted",
            },
        )

    monkeypatch.setattr(
        provider._remote_dispatcher,
        "_dispatch_remote_operation",
        _fake_dispatch_remote_operation,
    )
    request = _request("get_runtime_status", runner_id=runner_id, payload={})
    request.metadata["wait_for_result"] = True
    request.metadata["wait_timeout_seconds"] = 0.1

    result = asyncio.run(provider.get_runtime_status(request))

    assert result.accepted is True
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    delegate = result.metadata.get("delegate_result")
    assert isinstance(delegate, dict)
    assert delegate["container_status"] == "running"
    assert delegate["job_status"] == "running"


def test_cloud_runner_provider_waited_runtime_result_times_out_when_job_is_pending(monkeypatch) -> None:
    runtime_job_id = uuid4()
    runner_id = str(uuid4())

    runtime_job = SimpleNamespace(
        status="accepted",
        result_json={},
        error_code=None,
        error_message=None,
    )

    class _PendingRuntimeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: runtime_job)

    provider = CloudRunnerRuntimeProvider(session_factory=lambda: _PendingRuntimeSession())

    def _fake_dispatch_remote_operation(**_kwargs):
        return SimpleNamespace(
            accepted=True,
            metadata={
                "runtime_job_id": str(runtime_job_id),
                "runtime_job_status": "accepted",
            },
        )

    monkeypatch.setattr(
        provider._remote_dispatcher,
        "_dispatch_remote_operation",
        _fake_dispatch_remote_operation,
    )
    request = _request("get_runtime_metrics", runner_id=runner_id, payload={})
    request.metadata["wait_for_result"] = True
    request.metadata["wait_timeout_seconds"] = 0.0

    result = asyncio.run(provider.get_runtime_metrics(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_OPERATION_RESULT_TIMEOUT"
