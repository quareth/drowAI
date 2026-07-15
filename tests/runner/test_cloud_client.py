"""Tests for runner cloud client protocol, registration, and reconnect behavior."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from drowai_runner import cloud_client
from drowai_runner.artifact_uploader import ArtifactUploadBatchResult, ArtifactUploadFailure
from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.config import RunnerConfig
from drowai_runner.cloud_client import RunnerCloudClient
from drowai_runner.control_channel.artifacts.models import _PendingArtifactUploadContext
from drowai_runner.control_channel.artifacts.upload import ArtifactUploadHandler
from drowai_runner.control_channel.identity.models import (
    CloudChannelIdentity,
    RegistrationResult,
)
from drowai_runner.control_channel.identity.persistence import (
    _persist_runner_id,
    _persist_runner_secret,
    _persist_runner_tenant_id,
)
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.runtime.operation_map import map_remote_runtime_operation
from drowai_runner.control_channel.session import pump as cloud_client_pump
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.terminal.models import _ActiveTerminalSession
from drowai_runner.control_channel import constants as control_channel_constants
from drowai_runner.control_channel.tool_commands.models import (
    _ToolCommandCacheEntry,
)
from drowai_runner.control_channel.transport import connection as connection_module
from drowai_runner.control_channel.transport.reconnect import (
    compute_reconnect_delay_seconds,
    format_reconnect_error_reason,
)
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.logs_metrics import RunnerLogsMetricsAdapter
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.protocol_handler import (
    RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE,
    RUNNER_DEFERRED_RUNTIME_ERROR_CODE,
    RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
)
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.runner_protocol import (
    RUNNER_TOOL_STDIO_MAX_BYTES,
    RunnerArtifactUploadCompleteItem,
    RunnerArtifactManifestItem,
    RunnerArtifactManifestPayload,
    RunnerToolResultPayload,
    parse_runner_envelope_json,
)


def _build_cloud_config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_REGISTRATION_TOKEN": "rit_test_registration_token",
            "DROWAI_RUNNER_LABELS": '{"site":"hq"}',
            "DROWAI_RUNNER_CAPABILITIES": '["docker"]',
            "DROWAI_RUNNER_MAX_ACTIVE_TASKS": "3",
        }
    )


def _control_message(
    *,
    tenant_id: int,
    runner_id: str,
    message_type: str,
    message_id: str,
    schema_version: str = "runner_control.v1",
    runtime_job_id: str | None = None,
    task_id: int | None = None,
    payload: dict | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": "corr-1",
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload or {"command": "noop"},
        }
    )


def _tooling_plane_tool_command_message(
    *,
    tenant_id: int,
    runner_id: str,
    message_id: str,
    runtime_job_id: str,
    task_id: int,
    task_runtime_job_id: str,
    workspace_id: str,
    command_id: str,
    tool: str = "shell.exec",
    command: str | None = None,
    params: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": "tool.command",
            "schema_version": "tooling_plane.v1",
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": f"corr-{message_id}",
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-24T12:00:00+00:00",
            "payload": {
                "operation_id": f"op-{message_id}",
                "workspace_id": workspace_id,
                "task_runtime_job_id": task_runtime_job_id,
                "runtime_image": "drowai-runtime-local:latest",
                "tool": tool,
                "command": command or "id",
                "cwd": "/workspace",
                "env": {},
                "command_id": command_id,
                "timeout_seconds": 30.0,
                "timeout_policy": {"deadline_seconds": 30.0, "grace_seconds": 1.0},
                "route_policy": {
                    "selected_lane": "container_scoped",
                    "selected_authority": "container_runner_transport",
                },
                "delivery_policy": {"offline": "queue", "max_attempts": 2, "timeout_seconds": 4.0},
                "tool_call_id": "tool-call-1",
                "tool_batch_id": "tool-batch-1",
                "execution_strategy": "per_call",
                "params": dict(params or {"cwd": "/workspace"}),
            },
        }
    )


def _seed_runner_job(
    *,
    config: RunnerConfig,
    runtime_job_id: str,
    tenant_id: str,
    task_id: str,
    workspace_id: str,
) -> None:
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id=runtime_job_id,
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=workspace_id,
        image="runtime:test",
    )


class _FakeRemoteRuntimeOperationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((operation, dict(params)))
        if operation == "materialize_runtime":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"container_id": "cid-start"},
            }
        if operation in {"pause_runtime", "resume_runtime"}:
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"operation": operation},
            }
        if operation == "stop_runtime":
            lifecycle_intent = str(params.get("lifecycle_intent") or "").strip().lower()
            lifecycle_outcome = "cancelled" if lifecycle_intent == "cancel" else "stopped"
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "operation": "stop_runtime",
                    "lifecycle_outcome": lifecycle_outcome,
                    "cleanup_performed": False,
                },
            }
        if operation == "retire_runtime":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "operation": "retire_runtime",
                    "cleanup_performed": True,
                    "workspace_removed": True,
                },
            }
        if operation == "runtime_status":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"job_status": "running", "container_status": "running"},
            }
        if operation == "runtime_logs":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"logs": "line-1\nline-2", "lines": int(params.get("lines") or 200)},
            }
        if operation == "runtime_metrics":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"metrics": {"cpu_percent": 5.5}},
            }
        if operation == "check_vpn_status":
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"vpn_connected": True},
            }
        return {
            "accepted": False,
            "status": "failed",
            "error_code": "UNEXPECTED_OPERATION",
            "error_message": operation,
        }


@pytest.mark.parametrize(
    ("message_type", "params", "expected_operation", "expected_params"),
    [
        (
            "runtime.workspace.query",
            {"prefix": "reports"},
            "query_runtime_artifacts",
            {"workspace_id": "task-77", "prefix": "reports"},
        ),
        (
            "runtime.workspace.read",
            {"artifact_path": "reports/a.txt", "binary": True, "max_bytes": 1024},
            "read_runtime_artifact_file",
            {
                "workspace_id": "task-77",
                "artifact_path": "reports/a.txt",
                "binary": True,
                "max_bytes": 1024,
                "encoding": "utf-8",
            },
        ),
        (
            "runtime.workspace.write",
            {"artifact_path": "artifacts/out.txt", "content_base64": "b2s="},
            "write_runtime_artifact_file",
            {
                "workspace_id": "task-77",
                "artifact_path": "artifacts/out.txt",
                "content_base64": "b2s=",
                "encoding": "utf-8",
            },
        ),
        (
            "runtime.workspace.write",
            {"artifact_path": "index/chunks_task-77.jsonl", "content_base64": "b2s=", "mode": "append"},
            "write_runtime_artifact_file",
            {
                "workspace_id": "task-77",
                "artifact_path": "index/chunks_task-77.jsonl",
                "content_base64": "b2s=",
                "encoding": "utf-8",
                "mode": "append",
            },
        ),
    ],
)
def test_remote_runtime_live_workspace_requests_map_to_runner_workspace_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message_type: str,
    params: dict[str, object],
    expected_operation: str,
    expected_params: dict[str, object],
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    inbound = parse_runner_envelope_json(
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type=message_type,
            message_id=f"msg-{message_type}",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-workspace-1",
            task_id=77,
            payload={
                "operation_id": "op-workspace",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": message_type,
                "params": params,
            },
        )
    )

    operation, operation_params = map_remote_runtime_operation(
        inbound=inbound,
        context=_RemoteRuntimeRequestContext(
            runtime_job_id="job-remote-runtime-workspace-1",
            task_id=77,
            workspace_id="task-77",
        ),
    )

    assert operation == expected_operation
    for key, expected in expected_params.items():
        assert operation_params[key] == expected


def test_format_reconnect_error_reason_redacts_secrets() -> None:
    reason = format_reconnect_error_reason(
        RuntimeError("authorization: Bearer TOP_SECRET_TOKEN")
    )
    assert "TOP_SECRET_TOKEN" not in reason
    assert "<redacted>" in reason


def test_compute_reconnect_delay_is_bounded_with_jitter() -> None:
    delay_1 = compute_reconnect_delay_seconds(attempt=1, random_fraction=0.0)
    delay_2 = compute_reconnect_delay_seconds(attempt=2, random_fraction=1.0)
    delay_8 = compute_reconnect_delay_seconds(attempt=8, random_fraction=1.0)

    assert delay_1 == pytest.approx(1.0)
    assert delay_2 == pytest.approx(2.4)
    assert delay_8 <= 30.0
    assert delay_8 > 1.0


def test_cloud_client_resolves_identity_via_registration_and_masks_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "11")
    caplog.set_level("INFO")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    monkeypatch.setattr(
        client._registration_client,
        "register",
        lambda _payload: RegistrationResult(
            runner_id="runner-registered",
            tenant_id=11,
            credential_secret="rsec_registered_secret",
            channel_endpoint="http://cloud.example.test/api/runner-control/channel",
            protocol_version="runner_control.v1",
            heartbeat_interval_seconds=15,
        ),
    )

    identity = client._identity_resolver.resolve()
    output = caplog.text

    assert identity.runner_id == "runner-registered"
    assert identity.tenant_id == 11
    assert identity.credential_secret == "rsec_registered_secret"
    assert "<KEY_SET>" in output
    assert "rit_test_registration_token" not in output
    assert "rsec_registered_secret" not in output

    secret_path = config.runner_root / "credentials" / "runner.secret"
    runner_id_path = config.runner_root / "credentials" / "runner.secret.runner_id"
    tenant_id_path = config.runner_root / "credentials" / "runner.secret.tenant_id"
    assert secret_path.exists()
    assert secret_path.read_text(encoding="utf-8").strip() == "rsec_registered_secret"
    assert runner_id_path.exists()
    assert runner_id_path.read_text(encoding="utf-8").strip() == "runner-registered"
    assert tenant_id_path.exists()
    assert tenant_id_path.read_text(encoding="utf-8").strip() == "11"


def test_cloud_client_reuses_persisted_identity_after_restart_without_registration_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "11")
    initial_config = _build_cloud_config(tmp_path)
    initial_client = RunnerCloudClient(config=initial_config)
    monkeypatch.setattr(
        initial_client._registration_client,
        "register",
        lambda _payload: RegistrationResult(
            runner_id="runner-registered",
            tenant_id=11,
            credential_secret="rsec_registered_secret",
            channel_endpoint="http://cloud.example.test/api/runner-control/channel",
            protocol_version="runner_control.v1",
            heartbeat_interval_seconds=15,
        ),
    )

    first_identity = initial_client._identity_resolver.resolve()
    assert first_identity.runner_id == "runner-registered"

    restarted_config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_LABELS": '{"site":"hq"}',
            "DROWAI_RUNNER_CAPABILITIES": '["docker"]',
            "DROWAI_RUNNER_MAX_ACTIVE_TASKS": "3",
        }
    )
    restarted_client = RunnerCloudClient(config=restarted_config)
    monkeypatch.setattr(
        restarted_client._registration_client,
        "register",
        lambda _payload: pytest.fail("restart path should not attempt cloud registration"),
    )
    restart_identity = restarted_client._identity_resolver.resolve()

    assert restart_identity.runner_id == "runner-registered"
    assert restart_identity.tenant_id == 11
    assert restart_identity.credential_secret == "rsec_registered_secret"


def test_cloud_client_reuses_negotiated_protocol_version_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "11")
    initial_config = _build_cloud_config(tmp_path)
    initial_client = RunnerCloudClient(config=initial_config)
    monkeypatch.setattr(
        initial_client._registration_client,
        "register",
        lambda _payload: RegistrationResult(
            runner_id="runner-registered",
            tenant_id=11,
            credential_secret="rsec_registered_secret",
            channel_endpoint="http://cloud.example.test/api/runner-control/channel",
            protocol_version="data_plane.v1",
            heartbeat_interval_seconds=15,
        ),
    )

    first_identity = initial_client._identity_resolver.resolve()
    assert first_identity.protocol_version == "data_plane.v1"

    restarted_client = RunnerCloudClient(config=_build_cloud_config(tmp_path))
    monkeypatch.setattr(
        restarted_client._registration_client,
        "register",
        lambda _payload: pytest.fail("restart path should not attempt cloud registration"),
    )
    restart_identity = restarted_client._identity_resolver.resolve()

    assert restart_identity.protocol_version == "data_plane.v1"


def test_cloud_client_stored_identity_without_persisted_version_falls_back_to_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "11")
    config = _build_cloud_config(tmp_path)
    _persist_runner_secret(config, "rsec_legacy_secret")
    _persist_runner_id(config, "runner-legacy")
    _persist_runner_tenant_id(config, 11)

    client = RunnerCloudClient(config=config)
    monkeypatch.setattr(
        client._registration_client,
        "register",
        lambda _payload: pytest.fail("stored-credential path should not register"),
    )

    identity = client._identity_resolver.resolve()

    assert identity.runner_id == "runner-legacy"
    assert identity.protocol_version == "data_plane.v1"


def test_connected_session_sends_hello_heartbeat_and_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 2, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 3, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 5, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="task.start",
                    message_id="msg-task-start",
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=1,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    message_types = [json.loads(payload)["type"] for payload in fake_socket.sent]
    assert message_types[:2] == ["runner.hello", "runner.heartbeat"]
    assert "runner.heartbeat" in message_types
    assert "runner.ack" in message_types

    heartbeat_payload = next(
        json.loads(payload)["payload"]
        for payload in fake_socket.sent
        if json.loads(payload)["type"] == "runner.heartbeat"
    )
    assert set(heartbeat_payload["capacity"]) >= {
        "active_tasks",
        "max_active_tasks",
        "available_tasks",
        "max_parallel_commands_per_task",
        "docker_available",
        "runtime_image",
        "runtime_image_available",
        "version",
        "capabilities",
        "labels",
    }


def test_connected_session_omits_ssl_for_plain_ws_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    captured: dict[str, object] = {}

    class _FakeWebSocket:
        def send(self, payload: str) -> None:
            del payload

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    def _capture_connect(url: str, **kwargs):  # noqa: ANN202
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeWebSocket()

    monkeypatch.setattr(connection_module, "ws_connect", _capture_connect)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert captured["url"] == "ws://cloud.example.test/api/runner-control/channel"
    assert "ssl" not in captured["kwargs"]
    assert captured["kwargs"]["ping_interval"] is None
    assert captured["kwargs"]["ping_timeout"] is None


def test_connected_session_assignment_ack_is_idempotent_and_runtime_commands_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runner.assignment.probe",
                    message_id="msg-assignment-1",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=77,
                )
            if self.recv_calls == 2:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runner.assignment.probe",
                    message_id="msg-assignment-1",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=77,
                )
            if self.recv_calls == 3:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-other",
                    message_type="task.start",
                    message_id="msg-task-mismatch",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=77,
                )
            if self.recv_calls == 4:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="task.start",
                    message_id="msg-task-deferred",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=77,
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    ack_messages = [
        json.loads(payload)
        for payload in fake_socket.sent
        if json.loads(payload)["type"] == "runner.ack"
    ]
    assert len(ack_messages) == 4

    assignment_ack_1 = ack_messages[0]["payload"]
    assignment_ack_2 = ack_messages[1]["payload"]
    assert assignment_ack_1["acked_message_id"] == "msg-assignment-1"
    assert assignment_ack_1["status"] == "accepted"
    assert assignment_ack_1["error_code"] is None
    assert assignment_ack_2 == assignment_ack_1

    mismatch_ack = ack_messages[2]["payload"]
    assert mismatch_ack["acked_message_id"] == "msg-task-mismatch"
    assert mismatch_ack["status"] == "rejected"
    assert mismatch_ack["error_code"] == RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE

    deferred_ack = ack_messages[3]["payload"]
    assert deferred_ack["acked_message_id"] == "msg-task-deferred"
    assert deferred_ack["status"] == "failed"
    assert deferred_ack["error_code"] == RUNNER_DEFERRED_RUNTIME_ERROR_CODE


def test_connected_session_task_scoped_message_after_taskless_probe_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runner.assignment.probe",
                    message_id="msg-assignment-taskless",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=None,
                )
            if self.recv_calls == 2:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="task.start",
                    message_id="msg-task-after-taskless-probe",
                    runtime_job_id="8dd40e4f-f5be-4429-ae38-f68420680e13",
                    task_id=77,
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    ack_messages = [
        json.loads(payload)
        for payload in fake_socket.sent
        if json.loads(payload)["type"] == "runner.ack"
    ]
    assert len(ack_messages) == 2

    probe_ack = ack_messages[0]["payload"]
    assert probe_ack["acked_message_id"] == "msg-assignment-taskless"
    assert probe_ack["status"] == "accepted"
    assert probe_ack["error_code"] is None

    task_ack = ack_messages[1]["payload"]
    assert task_ack["acked_message_id"] == "msg-task-after-taskless-probe"
    assert task_ack["status"] == "rejected"
    assert task_ack["error_code"] == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE


def test_connected_session_executes_remote_runtime_runtime_inventory_and_emits_result_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="c955db18-ac61-44bd-81c9-ef7a7e6141fc",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runtime.inventory",
                    message_id="msg-remote-runtime-runtime-inventory",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="c955db18-ac61-44bd-81c9-ef7a7e6141fc",
                    task_id=77,
                    payload={
                        "operation_id": "op-remote-runtime-1",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "runtime.inventory",
                        "params": {"scope": "task", "filters": {}},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    sent_types = [payload["type"] for payload in sent_payloads]
    assert sent_types[0] == "runner.hello"
    assert "runner.ack" in sent_types
    assert "runtime.inventory" in sent_types

    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["acked_message_id"] == "msg-remote-runtime-runtime-inventory"
    assert ack_payload["status"] == "accepted"
    assert ack_payload["error_code"] is None

    runtime_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.inventory")
    assert runtime_payload["operation_id"] == "op-remote-runtime-1"
    assert runtime_payload["status"] == "succeeded"
    assert runtime_payload["error_code"] is None
    assert runtime_payload["result"]["runtime_job_id"] == "c955db18-ac61-44bd-81c9-ef7a7e6141fc"
    assert runtime_payload["result"]["task_id"] == 77
    assert runtime_payload["result"]["workspace_id"] == "task-77"
    assert runtime_payload["result"]["items"] == [
        {
            "runtime_job_id": "c955db18-ac61-44bd-81c9-ef7a7e6141fc",
            "task_id": "77",
            "workspace_id": "task-77",
            "status": "starting",
        }
    ]


def test_connected_session_closes_active_terminal_sessions_on_channel_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeOperationService:
        def __init__(self) -> None:
            self.close_calls: list[str] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            assert operation == "terminal_close"
            session_id = str(params.get("session_id") or "")
            self.close_calls.append(session_id)
            return {"accepted": True, "status": "succeeded"}

    fake_operation_service = _FakeOperationService()
    monkeypatch.setattr(client._composition, "_operation_service", fake_operation_service)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            client._active_terminal_sessions["session-a"] = _ActiveTerminalSession(  # type: ignore[attr-defined]
                runtime_job_id="job-a",
                task_id=77,
            )
            client._active_terminal_sessions["session-b"] = _ActiveTerminalSession(  # type: ignore[attr-defined]
                runtime_job_id="job-b",
                task_id=77,
            )
            client._terminal_frame_sequences["session-a"] = 4
            client._terminal_frame_sequences["session-b"] = 9
            raise ConnectionClosed(
                rcvd=Close(code=1000, reason="channel_closed"),
                sent=Close(code=1000, reason="channel_closed"),
                rcvd_then_sent=True,
            )

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    client._session_pump.run(identity)

    assert sorted(fake_operation_service.close_calls) == ["session-a", "session-b"]
    assert client._active_terminal_sessions == {}
    assert client._terminal_frame_sequences == {}


def test_connected_session_executes_remote_runtime_terminal_open_and_emits_terminal_result_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-terminal-open-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)
    fake_operation_service = _FakeRemoteRuntimeOperationService()
    monkeypatch.setattr(client._composition, "_operation_service", fake_operation_service)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="terminal.open",
                    message_id="msg-remote-runtime-terminal-open",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-remote-runtime-terminal-open-1",
                    task_id=77,
                    payload={
                        "operation_id": "op-terminal-open",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "terminal.open",
                        "params": {"session_name": "runtime", "cols": 120, "rows": 30},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    sent_types = [payload["type"] for payload in sent_payloads]
    assert sent_types[0] == "runner.hello"
    assert "runner.ack" in sent_types
    assert "terminal.result" in sent_types

    assert fake_operation_service.calls[0][0] == "terminal_open"
    assert fake_operation_service.calls[0][1]["runtime_job_id"] == "job-remote-runtime-terminal-open-1"

    terminal_payload = next(
        payload["payload"] for payload in sent_payloads if payload["type"] == "terminal.result"
    )
    assert terminal_payload["operation_id"] == "op-terminal-open"
    assert terminal_payload["terminal_operation"] == "open"
    assert terminal_payload["status"] == "failed"
    assert terminal_payload["result"]["runtime_job_id"] == "job-remote-runtime-terminal-open-1"
    assert terminal_payload["result"]["task_id"] == 77
    assert terminal_payload["result"]["workspace_id"] == "task-77"


def test_connected_session_terminal_proxy_operations_emit_terminal_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    client._composition._job_store = store

    class _RecordingPtyAdapter:
        def __init__(self) -> None:
            self.open_calls: list[tuple[str, str, int, int]] = []
            self.input_calls: list[tuple[str, str]] = []
            self.resize_calls: list[tuple[str, int, int]] = []
            self.close_calls: list[str] = []
            self.buffers: dict[str, bytearray] = {}

        def open_session(self, *, container_id: str, session_id: str, cols: int, rows: int) -> None:
            self.open_calls.append((container_id, session_id, cols, rows))
            self.buffers[session_id] = bytearray(b"banner$ ")

        def send_input(self, *, session_id: str, data: str) -> None:
            self.input_calls.append((session_id, data))
            self.buffers.setdefault(session_id, bytearray()).extend(data.encode("utf-8"))

        def read_output(self, *, session_id: str, max_bytes: int) -> bytes:
            buffer = self.buffers.get(session_id, bytearray())
            chunk = bytes(buffer[:max_bytes])
            del buffer[:max_bytes]
            self.buffers[session_id] = buffer
            return chunk

        def resize_session(self, *, session_id: str, cols: int, rows: int) -> None:
            self.resize_calls.append((session_id, cols, rows))

        def close_session(self, *, session_id: str) -> None:
            self.close_calls.append(session_id)
            self.buffers.pop(session_id, None)

    class _TerminalOperationService:
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
                        tenant_id="3",
                        task_id=task_id,
                        workspace_id=workspace_id,
                        image="runtime:test",
                        container_id="cid-77",
                    )
                self._job_store.mark_running(runtime_job_id, container_id="cid-77")
                return {
                    "accepted": True,
                    "status": "succeeded",
                    "metadata": {
                        "runtime_job_id": runtime_job_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "container_id": "cid-77",
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
                response = self._terminal_proxy.close_terminal_session(
                    session_id=str(params["session_id"]),
                )
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

    pty_adapter = _RecordingPtyAdapter()
    client._composition._operation_service = _TerminalOperationService(
        job_store=store,
        pty_adapter=pty_adapter,
    )

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages: list[str] = []
    start_runtime_job_id = "job-remote-runtime-terminal-sequence-1"
    control_open_job_id = "job-remote-runtime-terminal-open-control-1"
    control_input_job_id = "job-remote-runtime-terminal-input-control-1"
    control_resize_job_id = "job-remote-runtime-terminal-resize-control-1"
    control_close_job_id = "job-remote-runtime-terminal-close-control-1"

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls <= len(messages):
                return messages[self.recv_calls - 1]
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    messages.extend(
        [
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="task.start",
                message_id="msg-remote-runtime-terminal-start",
                schema_version="remote_runtime.v1",
                runtime_job_id=start_runtime_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-start",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "task.start",
                    "params": {},
                },
            ),
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="terminal.open",
                message_id="msg-remote-runtime-terminal-open-seq",
                schema_version="remote_runtime.v1",
                runtime_job_id=control_open_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-open-seq",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "terminal.open",
                    "params": {
                        "runtime_job_id": start_runtime_job_id,
                        "session_name": "runtime",
                        "cols": 120,
                        "rows": 30,
                    },
                },
            ),
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="terminal.input",
                message_id="msg-remote-runtime-terminal-input-seq",
                schema_version="remote_runtime.v1",
                runtime_job_id=control_input_job_id,
                task_id=77,
                    payload={
                        "operation_id": "op-remote-runtime-terminal-input-seq",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "terminal.input",
                        "session_id": "agent_task_77_runtime",
                        "data": "whoami\n",
                        "params": {
                            "runtime_job_id": start_runtime_job_id,
                            "session_id": "agent_task_77_runtime",
                            "data": "whoami\n",
                        },
                    },
                ),
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="terminal.resize",
                message_id="msg-remote-runtime-terminal-resize-seq",
                schema_version="remote_runtime.v1",
                runtime_job_id=control_resize_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-resize-seq",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "terminal.resize",
                    "session_id": "agent_task_77_runtime",
                    "cols": 140,
                    "rows": 42,
                    "params": {
                        "runtime_job_id": start_runtime_job_id,
                        "session_id": "agent_task_77_runtime",
                        "cols": 140,
                        "rows": 42,
                    },
                },
            ),
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="terminal.close",
                message_id="msg-remote-runtime-terminal-close-seq",
                schema_version="remote_runtime.v1",
                runtime_job_id=control_close_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-close-seq",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "terminal.close",
                    "session_id": "agent_task_77_runtime",
                    "params": {
                        "runtime_job_id": start_runtime_job_id,
                        "session_id": "agent_task_77_runtime",
                    },
                },
            ),
        ]
    )

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    terminal_result_events = [payload for payload in sent_payloads if payload["type"] == "terminal.result"]
    terminal_frame_events = [payload for payload in sent_payloads if payload["type"] == "terminal.frame"]
    terminal_results = [payload["payload"] for payload in terminal_result_events]
    terminal_frames = [payload["payload"] for payload in terminal_frame_events]

    assert len(terminal_results) == 4
    assert [payload["terminal_operation"] for payload in terminal_results] == [
        "open",
        "input",
        "resize",
        "close",
    ]
    assert all(payload["status"] == "succeeded" for payload in terminal_results)
    assert all(payload["result"]["runtime_job_id"] == start_runtime_job_id for payload in terminal_results)
    assert terminal_frames
    assert [frame["sequence"] for frame in terminal_frames] == list(range(len(terminal_frames)))
    assert all(frame["session_id"] == "agent_task_77_runtime" for frame in terminal_frames)
    assert all(event["runtime_job_id"] == start_runtime_job_id for event in terminal_frame_events)

    assert len(pty_adapter.open_calls) == 1
    assert pty_adapter.open_calls[0][0] == "cid-77"
    assert pty_adapter.input_calls == [("agent_task_77_runtime", "whoami\n")]
    assert pty_adapter.resize_calls == [("agent_task_77_runtime", 140, 42)]
    assert pty_adapter.close_calls == ["agent_task_77_runtime"]


def test_cloud_client_stream_terminal_input_acks_without_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    start_runtime_job_id = "job-remote-runtime-terminal-stream-1"
    _seed_runner_job(
        config=config,
        runtime_job_id=start_runtime_job_id,
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)

    class _StreamOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {"accepted": True, "status": "succeeded", "metadata": {"operation": operation}}

    operation_service = _StreamOperationService()
    monkeypatch.setattr(client._composition, "_operation_service", operation_service)
    monkeypatch.setattr(
        client._terminal_frame_lifecycle,
        "emit_for_active_sessions",
        lambda **_kwargs: None,
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

    websocket = _FakeWebSocket()
    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )
    inbound = parse_runner_envelope_json(
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="terminal.input",
            message_id="terminal-stream-input-1",
            schema_version="remote_runtime.v1",
            runtime_job_id="control-terminal-input-1",
            task_id=77,
            payload={
                "operation_id": "terminal.input:stream-test",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "terminal.input",
                "params": {
                    "runtime_job_id": start_runtime_job_id,
                    "session_id": "agent_task_77_runtime",
                    "data": "id\n",
                    "stream_mode": True,
                },
            },
        )
    )

    session_state = ConnectionSessionState()
    client._remote_runtime_handler.handle(
        websocket=websocket,
        identity=identity,
        inbound=inbound,
        session_state=session_state,
    )

    sent_payloads = [json.loads(payload) for payload in websocket.sent]
    assert [payload["type"] for payload in sent_payloads] == ["runner.ack"]
    assert sent_payloads[0]["payload"]["acked_message_id"] == "terminal-stream-input-1"
    assert operation_service.calls == [
        (
            "terminal_input",
            {
                "session_id": "agent_task_77_runtime",
                "data": "id\n",
            },
        )
    ]
    assert client._active_terminal_sessions["agent_task_77_runtime"].runtime_job_id == start_runtime_job_id


def test_connected_session_emits_delayed_terminal_frames_without_additional_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    client._composition._job_store = store

    class _DelayedOutputPtyAdapter:
        def __init__(self) -> None:
            self.buffers: dict[str, bytearray] = {}
            self.opened_session_ids: list[str] = []

        def open_session(self, *, container_id: str, session_id: str, cols: int, rows: int) -> None:
            del container_id, cols, rows
            self.opened_session_ids.append(session_id)
            self.buffers[session_id] = bytearray()

        def send_input(self, *, session_id: str, data: str) -> None:
            del session_id, data

        def read_output(self, *, session_id: str, max_bytes: int) -> bytes:
            buffer = self.buffers.get(session_id, bytearray())
            chunk = bytes(buffer[:max_bytes])
            del buffer[:max_bytes]
            self.buffers[session_id] = buffer
            return chunk

        def resize_session(self, *, session_id: str, cols: int, rows: int) -> None:
            del session_id, cols, rows

        def close_session(self, *, session_id: str) -> None:
            self.buffers.pop(session_id, None)

    class _DelayedTerminalOperationService:
        def __init__(self, *, job_store, pty_adapter: _DelayedOutputPtyAdapter) -> None:
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
                        tenant_id="3",
                        task_id=task_id,
                        workspace_id=workspace_id,
                        image="runtime:test",
                        container_id="cid-77",
                    )
                self._job_store.mark_running(runtime_job_id, container_id="cid-77")
                return {
                    "accepted": True,
                    "status": "succeeded",
                    "metadata": {
                        "runtime_job_id": runtime_job_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "container_id": "cid-77",
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

    pty_adapter = _DelayedOutputPtyAdapter()
    client._composition._operation_service = _DelayedTerminalOperationService(job_store=store, pty_adapter=pty_adapter)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages: list[str] = []
    start_runtime_job_id = "job-remote-runtime-terminal-delayed-output-1"
    control_open_job_id = "job-remote-runtime-terminal-delayed-open-control-1"

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls <= len(messages):
                return messages[self.recv_calls - 1]
            if self.recv_calls == len(messages) + 1:
                if pty_adapter.opened_session_ids:
                    pty_adapter.buffers[pty_adapter.opened_session_ids[0]].extend(b"delayed output\\n")
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        if any('"terminal.frame"' in payload for payload in self.sent):
                            break
                        time.sleep(0.005)
                raise TimeoutError
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    messages.extend(
        [
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="task.start",
                message_id="msg-remote-runtime-terminal-delayed-start",
                schema_version="remote_runtime.v1",
                runtime_job_id=start_runtime_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-delayed-start",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "task.start",
                    "params": {},
                },
            ),
            _control_message(
                tenant_id=3,
                runner_id="runner-3",
                message_type="terminal.open",
                message_id="msg-remote-runtime-terminal-delayed-open",
                schema_version="remote_runtime.v1",
                runtime_job_id=control_open_job_id,
                task_id=77,
                payload={
                    "operation_id": "op-remote-runtime-terminal-delayed-open",
                    "workspace_id": "task-77",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": "terminal.open",
                    "params": {
                        "runtime_job_id": start_runtime_job_id,
                        "session_name": "runtime",
                        "cols": 120,
                        "rows": 30,
                    },
                },
            ),
        ]
    )

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(
        control_channel_constants,
        "_TERMINAL_FRAME_POLL_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    terminal_frame_events = [payload for payload in sent_payloads if payload["type"] == "terminal.frame"]
    terminal_frames = [payload["payload"] for payload in terminal_frame_events]

    assert terminal_frames
    assert terminal_frames[-1]["data"] == "delayed output\\n"
    assert terminal_frames[-1]["session_id"] == "agent_task_77_runtime"
    assert terminal_frames[-1]["sequence"] >= 0
    assert all(event["runtime_job_id"] == start_runtime_job_id for event in terminal_frame_events)


def test_connected_session_executes_remote_runtime_vpn_config_and_materializes_task_local_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-vpn-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)
    secret_payload = "[Interface]\nPrivateKey=super-secret\n"

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runtime.vpn.config",
                    message_id="msg-remote-runtime-runtime-vpn-config",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-remote-runtime-vpn-1",
                    task_id=77,
                    payload={
                        "operation_id": "op-vpn-config",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "runtime.vpn.config",
                        "params": {
                            "vpn_config": {
                                "config_data": secret_payload,
                                "private_key": "embedded-private-key",
                                "file_name": "task-77.ovpn",
                            }
                        },
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    vpn_path = config.runner_root / "control" / "task-77" / "vpn" / "task.ovpn"
    assert vpn_path.exists()
    assert vpn_path.read_text(encoding="utf-8") == secret_payload

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    sent_types = [payload["type"] for payload in sent_payloads]
    assert "runner.ack" in sent_types
    assert "runtime.vpn.config" in sent_types
    vpn_result = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.vpn.config")
    assert vpn_result["status"] == "succeeded"
    assert vpn_result["result"]["vpn_file"] == "vpn/task-77.ovpn"
    assert "super-secret" not in json.dumps(sent_payloads)
    assert "embedded-private-key" not in json.dumps(sent_payloads)


def test_connected_session_executes_remote_runtime_runtime_workspace_cleanup_with_scope_and_retain_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-cleanup-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    workspace_manager = RunnerWorkspaceManager(config.runner_root)
    workspace = workspace_manager.initialize_task_workspace("task-77")
    (workspace / "artifacts" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (workspace / "logs" / "drop.log").write_text("drop\n", encoding="utf-8")
    (workspace / "results" / "drop.json").write_text("{}\n", encoding="utf-8")
    (workspace / "commands.jsonl").write_text("{}\n", encoding="utf-8")
    (workspace / "results.jsonl").write_text("{}\n", encoding="utf-8")
    (workspace / "agent_state.json").write_text("{}\n", encoding="utf-8")

    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 6

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages = [
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.workspace.cleanup",
            message_id="msg-remote-runtime-cleanup-runtime",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-cleanup-1",
            task_id=77,
            payload={
                "operation_id": "op-cleanup-runtime",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.workspace.cleanup",
                "params": {"cleanup_scope": "runtime", "retain_outputs": True},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.workspace.cleanup",
            message_id="msg-remote-runtime-cleanup-workspace-retain",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-cleanup-1",
            task_id=77,
            payload={
                "operation_id": "op-cleanup-workspace",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.workspace.cleanup",
                "params": {"cleanup_scope": "workspace", "retain_outputs": True},
            },
        ),
    ]

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = list(messages)

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert workspace.exists()
    assert (workspace / "artifacts" / "keep.txt").exists()
    assert not (workspace / "logs").exists()
    assert not (workspace / "results").exists()
    assert not (workspace / "locks").exists()
    assert not (workspace / "commands.jsonl").exists()
    assert not (workspace / "results.jsonl").exists()
    assert not (workspace / "agent_state.json").exists()
    assert not (workspace / "scope.md").exists()
    assert not (workspace / "config.json").exists()

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    cleanup_events = [payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.workspace.cleanup"]
    assert len(cleanup_events) == 2
    first_result = cleanup_events[0]["result"]
    second_result = cleanup_events[1]["result"]
    assert first_result["cleanup_scope"] == "runtime"
    assert first_result["retain_outputs"] is True
    assert "artifacts" in first_result["retained_paths"]
    assert second_result["cleanup_scope"] == "workspace"
    assert second_result["retain_outputs"] is True
    assert second_result["workspace_removed"] is False
    assert second_result["retained_paths"] == ["artifacts"]


def test_connected_session_heartbeat_reports_recovered_active_jobs_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    job_store.start_job(
        runtime_job_id="job-active-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
        image="runtime:test",
    )
    restarted_client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [
            datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 23, 12, 0, 2, tzinfo=UTC),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 3, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=1,
    )

    with pytest.raises(KeyboardInterrupt):
        restarted_client._session_pump.run(identity)

    heartbeats = [json.loads(payload) for payload in fake_socket.sent if json.loads(payload)["type"] == "runner.heartbeat"]
    heartbeat_capacity = heartbeats[0]["payload"]["capacity"]
    assert heartbeat_capacity["active_tasks"] == 1
    assert heartbeat_capacity["available_tasks"] == heartbeat_capacity["max_active_tasks"] - 1
    assert heartbeat_capacity["active_runtime_jobs"] == [
        {
            "runtime_job_id": "job-active-1",
            "task_id": "77",
            "workspace_id": "task-77",
            "status": "starting",
        }
    ]


def test_connected_session_executes_remote_runtime_start_lifecycle_and_snapshot_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)
    fake_operations = _FakeRemoteRuntimeOperationService()
    client._composition._operation_service = fake_operations  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 20

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages = [
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.start",
            message_id="msg-remote-runtime-task-start",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={
                "operation_id": "op-start",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "task.start",
                "params": {},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.pause",
            message_id="msg-remote-runtime-task-pause",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-pause", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "task.pause", "params": {}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.resume",
            message_id="msg-remote-runtime-task-resume",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-resume", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "task.resume", "params": {}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.stop",
            message_id="msg-remote-runtime-task-stop",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={
                "operation_id": "op-stop",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "task.stop",
                "params": {"lifecycle_intent": "cancel"},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.retire",
            message_id="msg-remote-runtime-task-retire",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-retire", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "task.retire", "params": {}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.status",
            message_id="msg-remote-runtime-runtime-status",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-status", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "runtime.status", "params": {}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.logs",
            message_id="msg-remote-runtime-runtime-logs",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-logs", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "runtime.logs", "params": {"lines": 20}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.metrics",
            message_id="msg-remote-runtime-runtime-metrics",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-metrics", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "runtime.metrics", "params": {}},
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.vpn.status",
            message_id="msg-remote-runtime-runtime-vpn-status",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-1",
            task_id=77,
            payload={"operation_id": "op-vpn-status", "workspace_id": "task-77", "runtime_image": "drowai-runtime-local:latest", "operation": "runtime.vpn.status", "params": {}},
        ),
    ]

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = list(messages)

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    emitted_types = [payload["type"] for payload in sent_payloads]
    assert emitted_types.count("runner.ack") == len(messages)
    assert "runtime.started" in emitted_types
    assert "runtime.paused" in emitted_types
    assert "runtime.resumed" in emitted_types
    assert "runtime.stopped" in emitted_types
    assert "runtime.retired" in emitted_types
    assert "runtime.status" in emitted_types
    assert "runtime.logs" in emitted_types
    assert "runtime.metrics" in emitted_types
    assert "runtime.vpn.status" in emitted_types

    operation_names = [operation for operation, _params in fake_operations.calls]
    assert operation_names == [
        "materialize_runtime",
        "pause_runtime",
        "resume_runtime",
        "stop_runtime",
        "retire_runtime",
        "runtime_status",
        "runtime_logs",
        "runtime_metrics",
        "check_vpn_status",
    ]

    stop_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.stopped")
    assert stop_payload["result"]["lifecycle_outcome"] == "cancelled"
    assert stop_payload["result"]["cleanup_performed"] is False

    retire_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.retired")
    assert retire_payload["result"]["cleanup_performed"] is True
    assert retire_payload["result"]["workspace_removed"] is True


def test_connected_session_accepts_retire_when_local_job_store_lost_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    fake_operations = _FakeRemoteRuntimeOperationService()
    client._composition._operation_service = fake_operations  # type: ignore[assignment]

    message = _control_message(
        tenant_id=3,
        runner_id="runner-3",
        message_type="task.retire",
        message_id="msg-remote-runtime-task-retire-lost-state",
        schema_version="remote_runtime.v1",
        runtime_job_id="job-lost-state",
        task_id=78,
        payload={
            "operation_id": "op-retire-lost-state",
            "workspace_id": "task-78",
            "runtime_image": "drowai-runtime-local:latest",
            "operation": "task.retire",
            "params": {"runtime_job_id": "job-lost-state"},
        },
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [message]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)
    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack = next(payload for payload in sent_payloads if payload["type"] == "runner.ack")
    retired = next(payload for payload in sent_payloads if payload["type"] == "runtime.retired")
    assert ack["payload"]["status"] == "accepted"
    assert retired["payload"]["result"]["runtime_job_id"] == "job-lost-state"
    assert fake_operations.calls == [
        (
            "retire_runtime",
            {
                "runtime_job_id": "job-lost-state",
                "tenant_id": "3",
                "task_id": 78,
                "workspace_id": "task-78",
            },
        )
    ]


def test_connected_session_remote_runtime_inventory_is_scoped_to_requested_runtime_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-scope-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
        image="runtime:test",
    )
    store.start_job(
        runtime_job_id="job-foreign-1",
        tenant_id="3",
        task_id="88",
        workspace_id="task-88",
        image="runtime:test",
    )
    store.mark_running("job-foreign-1", container_id="cid-foreign")
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runtime.inventory",
                    message_id="msg-remote-runtime-runtime-inventory-scoped",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-scope-1",
                    task_id=77,
                    payload={
                        "operation_id": "op-remote-runtime-scoped",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "runtime.inventory",
                        "params": {"scope": "task"},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    runtime_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.inventory")
    items = runtime_payload["result"]["items"]
    assert items == [
        {
            "runtime_job_id": "job-scope-1",
            "task_id": "77",
            "workspace_id": "task-77",
            "status": "starting",
        }
    ]
    assert "container_id" not in json.dumps(items)


def test_connected_session_rejects_unassigned_remote_runtime_cleanup_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    workspace_manager = RunnerWorkspaceManager(config.runner_root)
    workspace = workspace_manager.initialize_task_workspace("task-77")
    marker = workspace / "logs" / "keep.log"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep\n", encoding="utf-8")
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 3

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runtime.workspace.cleanup",
                    message_id="msg-remote-runtime-cleanup-missing-job",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-missing-cleanup",
                    task_id=77,
                    payload={
                        "operation_id": "op-cleanup-missing",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "runtime.workspace.cleanup",
                        "params": {"cleanup_scope": "workspace", "retain_outputs": False},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "rejected"
    assert ack_payload["error_code"] == "RUNTIME_JOB_NOT_ASSIGNED"
    assert not any(payload["type"] == "runtime.workspace.cleanup" for payload in sent_payloads)
    assert marker.exists()


def test_connected_session_rejects_workspace_mismatch_for_vpn_and_env_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-guard-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    workspace_manager = RunnerWorkspaceManager(config.runner_root)
    workspace_manager.initialize_task_workspace("task-77")
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages = [
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.vpn.config",
            message_id="msg-remote-runtime-vpn-mismatch",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-guard-1",
            task_id=77,
            payload={
                "operation_id": "op-vpn-mismatch",
                "workspace_id": "task-999",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.vpn.config",
                "params": {"vpn_config": {"config_data": "[Interface]\nPrivateKey=secret\n"}},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.environment.metadata",
            message_id="msg-remote-runtime-env-mismatch",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-remote-runtime-guard-1",
            task_id=77,
            payload={
                "operation_id": "op-env-mismatch",
                "workspace_id": "task-999",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.environment.metadata",
                "params": {"action": "write", "key": "agent.version", "value": "1.2.3"},
            },
        ),
    ]

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = list(messages)

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payloads = [payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack"]
    assert [ack["error_code"] for ack in ack_payloads] == [
        "RUNTIME_WORKSPACE_MISMATCH",
        "RUNTIME_WORKSPACE_MISMATCH",
    ]
    assert not any(payload["type"] == "runtime.vpn.config" for payload in sent_payloads)
    assert not any(payload["type"] == "runtime.environment.metadata" for payload in sent_payloads)
    assert not (config.runner_root / "tasks" / "task-77" / "vpn" / "task.ovpn").exists()
    assert not (config.runner_root / "tasks" / "task-77" / ".runtime-env.json").exists()


def test_cloud_client_maps_environment_query_with_runtime_job_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)
    inbound = parse_runner_envelope_json(
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.environment.metadata",
            message_id="msg-env-query",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-env-query-1",
            task_id=77,
            payload={
                "operation_id": "op-env-query",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.environment.metadata",
                "params": {"action": "query", "filters": {}},
            },
        )
    )

    operation, params = map_remote_runtime_operation(
        inbound=inbound,
        context=_RemoteRuntimeRequestContext(
            runtime_job_id="job-env-query-1",
            task_id=77,
            workspace_id="task-77",
        ),
    )

    assert operation == "query_runtime_environment_metadata"
    assert params["workspace_id"] == "task-77"
    assert params["runtime_job_id"] == "job-env-query-1"


def test_connected_session_rejects_duplicate_or_mismatched_task_start_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-existing-start-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    messages = [
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.start",
            message_id="msg-remote-runtime-start-duplicate",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-duplicate-start",
            task_id=77,
            payload={
                "operation_id": "op-start-duplicate",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "task.start",
                "params": {},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.start",
            message_id="msg-remote-runtime-start-workspace-mismatch",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-start-workspace-mismatch",
            task_id=78,
            payload={
                "operation_id": "op-start-workspace-mismatch",
                "workspace_id": "task-999",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "task.start",
                "params": {},
            },
        ),
    ]

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = list(messages)

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payloads = [payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack"]
    assert [ack["error_code"] for ack in ack_payloads] == [
        "RUNTIME_JOB_START_CONFLICT",
        "RUNTIME_WORKSPACE_MISMATCH",
    ]
    assert not any(payload["type"] == "runtime.started" for payload in sent_payloads)

    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    assert store.find_job("job-duplicate-start") is None
    assert store.find_job("job-start-workspace-mismatch") is None
    assert not (config.runner_root / "tasks" / "task-78").exists()
    assert not (config.runner_root / "tasks" / "task-999").exists()


def test_connected_session_allows_new_task_start_after_previous_job_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-old-stopped",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
        image="runtime:test",
    )
    store.mark_stopped("job-old-stopped", status="stopped")
    client = RunnerCloudClient(config=config)
    client._composition._job_store = store

    class _FakeStartOperationService:
        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            assert operation == "materialize_runtime"
            runtime_job_id = str(params["runtime_job_id"])
            task_id = str(params["task_id"])
            workspace_id = str(params["workspace_id"])
            image = str(params["image"])
            tenant_id = str(params["tenant_id"])
            store.start_job(
                runtime_job_id=runtime_job_id,
                tenant_id=tenant_id,
                task_id=task_id,
                workspace_id=workspace_id,
                image=image,
            )
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"runtime_job_id": runtime_job_id, "workspace_id": workspace_id},
            }

    client._composition._operation_service = _FakeStartOperationService()  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="task.start",
                    message_id="msg-remote-runtime-start-after-stop",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-new-after-stop",
                    task_id=77,
                    payload={
                        "operation_id": "op-start-after-stop",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "task.start",
                        "params": {},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"
    assert ack_payload["error_code"] is None
    assert any(payload["type"] == "runtime.started" for payload in sent_payloads)
    assert store.find_job("job-old-stopped") is None
    new_job = store.get_job("job-new-after-stop")
    assert new_job.task_id == "77"
    assert new_job.workspace_id == "task-77"


def test_connected_session_allows_new_task_start_after_previous_job_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-old-retired",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
        image="runtime:test",
    )
    store.mark_stopped("job-old-retired", status="stopped")
    store.mark_cleaned_up("job-old-retired")
    client = RunnerCloudClient(config=config)
    client._composition._job_store = store

    class _FakeStartOperationService:
        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            assert operation == "materialize_runtime"
            runtime_job_id = str(params["runtime_job_id"])
            task_id = str(params["task_id"])
            workspace_id = str(params["workspace_id"])
            image = str(params["image"])
            tenant_id = str(params["tenant_id"])
            store.start_job(
                runtime_job_id=runtime_job_id,
                tenant_id=tenant_id,
                task_id=task_id,
                workspace_id=workspace_id,
                image=image,
            )
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"runtime_job_id": runtime_job_id, "workspace_id": workspace_id},
            }

    client._composition._operation_service = _FakeStartOperationService()  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.recv_calls = 0

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            self.recv_calls += 1
            if self.recv_calls == 1:
                return _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="task.start",
                    message_id="msg-remote-runtime-start-after-retire",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-new-after-retire",
                    task_id=77,
                    payload={
                        "operation_id": "op-start-after-retire",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "task.start",
                        "params": {},
                    },
                )
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"
    assert ack_payload["error_code"] is None
    assert any(payload["type"] == "runtime.started" for payload in sent_payloads)
    assert store.find_job("job-old-retired") is None
    new_job = store.get_job("job-new-after-retire")
    assert new_job.task_id == "77"
    assert new_job.workspace_id == "task-77"


def test_connected_session_pause_persists_paused_state_and_status_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    workspace = RunnerWorkspaceManager(config.runner_root)
    workspace.initialize_runner_root()
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="job-paused-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
        image="runtime:test",
        container_id="cid-paused-1",
    )
    store.mark_running("job-paused-1", container_id="cid-paused-1")
    client = RunnerCloudClient(config=config)
    client._composition._job_store = store

    class _FakePauseDockerRuntime:
        def __init__(self) -> None:
            self._paused: set[str] = set()

        def pause_container(self, container_id: str) -> None:
            self._paused.add(container_id)

        def resume_container(self, container_id: str) -> None:
            self._paused.discard(container_id)

        def container_status(self, container_id: str) -> str:
            if container_id in self._paused:
                return "paused"
            return "running"

    class _UnsupportedTerminalProxy:
        def open_terminal_session(self, **_kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("terminal open should not be called")

        def send_terminal_input(self, **_kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("terminal input should not be called")

        def read_terminal_output(self, **_kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("terminal read should not be called")

        def resize_terminal_session(self, **_kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("terminal resize should not be called")

        def close_terminal_session(self, **_kwargs):  # noqa: ANN003, ANN201
            raise AssertionError("terminal close should not be called")

    docker_runtime = _FakePauseDockerRuntime()
    logs_metrics = RunnerLogsMetricsAdapter(
        job_store=store,
        docker_runtime=docker_runtime,  # type: ignore[arg-type]
        workspace_manager=workspace,
    )
    cleanup = RunnerCleanupService(
        workspace_manager=workspace,
        job_store=store,
        remove_container=lambda _container_id: None,
        cleanup_retention_hours=config.cleanup_retention_hours,
    )
    client._composition._operation_service = RunnerOperationService(
        config=config,
        workspace=workspace,
        job_store=store,
        docker_runtime=docker_runtime,  # type: ignore[arg-type]
        logs_metrics=logs_metrics,
        terminal_proxy=_UnsupportedTerminalProxy(),  # type: ignore[arg-type]
        cleanup=cleanup,
    )

    messages = [
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="task.pause",
            message_id="msg-remote-runtime-pause-state",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-paused-1",
            task_id=77,
            payload={
                "operation_id": "op-pause-state",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "task.pause",
                "params": {},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.status",
            message_id="msg-remote-runtime-status-after-pause",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-paused-1",
            task_id=77,
            payload={
                "operation_id": "op-status-after-pause",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.status",
                "params": {},
            },
        ),
        _control_message(
            tenant_id=3,
            runner_id="runner-3",
            message_type="runtime.startup_progress",
            message_id="msg-remote-runtime-startup-after-pause",
            schema_version="remote_runtime.v1",
            runtime_job_id="job-paused-1",
            task_id=77,
            payload={
                "operation_id": "op-startup-after-pause",
                "workspace_id": "task-77",
                "runtime_image": "drowai-runtime-local:latest",
                "operation": "runtime.startup_progress",
                "params": {},
            },
        ),
    ]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)] * 10

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = list(messages)

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    paused_job = store.get_job("job-paused-1")
    assert paused_job.status == "paused"

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    status_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.status")
    assert status_payload["result"]["job_status"] == "paused"

    startup_payload = next(
        payload["payload"] for payload in sent_payloads if payload["type"] == "runtime.startup_progress"
    )
    assert startup_payload["result"]["startup_phase"] == "paused"


def test_connected_session_executes_tooling_plane_tool_command_and_emits_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "completed",
                "error_code": None,
                "error_message": None,
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": str(params.get("command_id") or ""),
                    "exit_code": 0,
                    "stdout": "uid=0(root)",
                    "stderr": "",
                    "artifacts": ["artifacts/cmd-91/stdout.txt"],
                    "metadata": {
                        "semantic_schema_version": "generic.v1",
                        "capability_family": "network_discovery",
                        "hosts": [{"ip": "127.0.0.1"}],
                    },
                },
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-1",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    operation, params = fake_operation_service.calls[0]
    assert operation == "submit_tool_command"
    assert params["runtime_job_id"] == "task-runtime-91"
    assert params["tool_command_runtime_job_id"] == "tool-command-job-91"
    assert params["command_id"] == "cmd-91"
    assert params["tool"] == "shell.exec"
    assert params["timeout_policy"] == {"deadline_seconds": 30.0, "grace_seconds": 1.0}

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"
    assert ack_payload["error_code"] is None
    result_envelope = next(payload for payload in sent_payloads if payload["type"] == "tool.result")
    assert result_envelope["runtime_job_id"] == "tool-command-job-91"
    assert result_envelope["payload"]["status"] == "completed"
    assert result_envelope["payload"]["success"] is True
    assert result_envelope["payload"]["command_id"] == "cmd-91"
    assert result_envelope["payload"]["metadata"]["task_runtime_job_id"] == "task-runtime-91"
    assert result_envelope["payload"]["metadata"]["workspace_id"] == "task-91"
    assert result_envelope["payload"]["metadata"]["tool_metadata"]["hosts"] == [{"ip": "127.0.0.1"}]
    assert result_envelope["payload"]["metadata"]["semantic_schema_version"] == "generic.v1"
    assert result_envelope["payload"]["metadata"]["capability_family"] == "network_discovery"
    assert "metadata" not in result_envelope["payload"]["metadata"]

    result_wire_payload = next(payload for payload in fake_socket.sent if json.loads(payload)["type"] == "tool.result")
    parsed_result_envelope = parse_runner_envelope_json(result_wire_payload)
    assert parsed_result_envelope.runtime_job_id == "tool-command-job-91"
    assert parsed_result_envelope.payload.status == "completed"
    assert parsed_result_envelope.payload.success is True


def test_connected_session_executes_remote_runtime_workspace_write_and_emits_result_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="job-remote-runtime-write-1",
        tenant_id="3",
        task_id="77",
        workspace_id="task-77",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeWorkspaceWriteOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "metadata": {
                    "workspace_id": "task-77",
                    "path": "artifacts/out.txt",
                    "encoding": "base64",
                    "mode": "write",
                    "size": 3,
                },
            }

    fake_operation_service = _FakeWorkspaceWriteOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 0, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _control_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_type="runtime.workspace.write",
                    message_id="msg-remote-runtime-write-1",
                    schema_version="remote_runtime.v1",
                    runtime_job_id="job-remote-runtime-write-1",
                    task_id=77,
                    payload={
                        "operation_id": "op-write-1",
                        "workspace_id": "task-77",
                        "runtime_image": "drowai-runtime-local:latest",
                        "operation": "runtime.workspace.write",
                        "params": {
                            "artifact_path": "artifacts/out.txt",
                            "content_base64": "b2s=",
                        },
                    },
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="remote_runtime.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    operation, params = fake_operation_service.calls[0]
    assert operation == "write_runtime_artifact_file"
    assert params["workspace_id"] == "task-77"
    assert params["artifact_path"] == "artifacts/out.txt"
    assert params["content_base64"] == "b2s="

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"
    assert ack_payload["error_code"] is None

    write_result = next(payload for payload in sent_payloads if payload["type"] == "runtime.workspace.write")
    assert write_result["runtime_job_id"] == "job-remote-runtime-write-1"
    assert write_result["payload"]["status"] == "succeeded"
    assert write_result["payload"]["result"]["path"] == "artifacts/out.txt"
    assert write_result["payload"]["result"]["workspace_id"] == "task-77"

    parsed_result_envelope = parse_runner_envelope_json(
        next(payload for payload in fake_socket.sent if json.loads(payload)["type"] == "runtime.workspace.write")
    )
    assert parsed_result_envelope.message_type.value == "runtime.workspace.write"
    assert parsed_result_envelope.payload.status == "succeeded"


def test_connected_session_tooling_plane_tool_command_sanitizes_result_metadata_and_caps_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    raw_secret = "super-secret-command-flag"
    oversized_stdout = "x" * (RUNNER_TOOL_STDIO_MAX_BYTES + 64)
    oversized_stderr = "authorization: Bearer VERY_SECRET_TOKEN\n" + ("y" * (RUNNER_TOOL_STDIO_MAX_BYTES + 64))

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "completed",
                "error_code": None,
                "error_message": None,
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": str(params.get("command_id") or ""),
                    "exit_code": 0,
                    "stdout": oversized_stdout,
                    "stderr": oversized_stderr,
                    "artifacts": ["artifacts/cmd-91/stdout.txt"],
                    "api_key": "plain-secret-value",
                    "nested": {
                        "cookie": "session=abc123",
                        "safe": "value",
                    },
                },
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 2, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 2, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-sanitize-1",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                    command=f"echo {raw_secret}",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    result_envelope = next(payload for payload in sent_payloads if payload["type"] == "tool.result")
    result_payload = result_envelope["payload"]
    result_metadata = result_payload["metadata"]

    assert len(result_payload["stdout"].encode("utf-8")) <= RUNNER_TOOL_STDIO_MAX_BYTES
    assert len(result_payload["stderr"].encode("utf-8")) <= RUNNER_TOOL_STDIO_MAX_BYTES
    assert "VERY_SECRET_TOKEN" not in result_payload["stderr"]
    assert "<redacted>" in result_payload["stderr"]
    assert result_metadata["api_key"] == "<redacted>"
    assert result_metadata["nested"]["cookie"] == "<redacted>"
    assert result_metadata["nested"]["safe"] == "value"
    assert result_metadata["runner_transport_stdout_truncated"] is True
    assert result_metadata["runner_transport_stderr_truncated"] is True
    assert result_metadata["runner_transport_stdout_original_bytes"] > RUNNER_TOOL_STDIO_MAX_BYTES
    assert result_metadata["runner_transport_stderr_original_bytes"] > RUNNER_TOOL_STDIO_MAX_BYTES

    output = capsys.readouterr().out
    assert raw_secret not in output


def test_connected_session_tooling_plane_tool_command_rejects_mismatched_envelope_task_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"runtime_job_id": str(params.get("runtime_job_id") or "")},
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 5, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 5, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-task-mismatch",
                    runtime_job_id="tool-command-job-91",
                    task_id=92,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert fake_operation_service.calls == []
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "rejected"
    assert ack_payload["error_code"] == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE
    assert not any(payload["type"] == "tool.result" for payload in sent_payloads)


def test_connected_session_tooling_plane_tool_command_duplicate_runtime_binding_conflict_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": str(params.get("command_id") or ""),
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "artifacts": [],
                },
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 10, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 10, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-dup-1",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-dup-2",
                    runtime_job_id="tool-command-job-92",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent if json.loads(payload)["type"] == "runner.ack"]
    assert sent_payloads[0]["payload"]["status"] == "accepted"
    assert sent_payloads[1]["payload"]["status"] == "rejected"
    assert sent_payloads[1]["payload"]["error_code"] == "TOOL_COMMAND_RUNTIME_JOB_BINDING_CONFLICT"


def test_connected_session_tooling_plane_tool_command_rejects_unknown_task_runtime_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"runtime_job_id": str(params.get("runtime_job_id") or "")},
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 15, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 15, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-unknown-runtime",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-missing",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert fake_operation_service.calls == []
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "rejected"
    assert ack_payload["error_code"] == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE
    assert not any(payload["type"] == "tool.result" for payload in sent_payloads)


def test_connected_session_tooling_plane_tool_command_duplicate_delivery_replays_cached_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": str(params.get("command_id") or ""),
                    "exit_code": 0,
                    "stdout": "first-run",
                    "stderr": "",
                    "artifacts": ["artifacts/cmd-91/stdout.txt"],
                },
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 20, 0, tzinfo=UTC)] * 8

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 20, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-dup-replay-1",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-dup-replay-2",
                    runtime_job_id="tool-command-job-91",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-91",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    operation, params = fake_operation_service.calls[0]
    assert operation == "submit_tool_command"
    assert params["runtime_job_id"] == "task-runtime-91"

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payloads = [payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack"]
    assert len(ack_payloads) == 2
    assert ack_payloads[0]["status"] == "accepted"
    assert ack_payloads[1]["status"] == "accepted"

    result_envelopes = [payload for payload in sent_payloads if payload["type"] == "tool.result"]
    assert len(result_envelopes) == 2
    assert result_envelopes[0]["runtime_job_id"] == "tool-command-job-91"
    assert result_envelopes[1]["runtime_job_id"] == "tool-command-job-91"
    assert result_envelopes[0]["payload"]["metadata"]["task_runtime_job_id"] == "task-runtime-91"
    assert result_envelopes[1]["payload"]["metadata"]["task_runtime_job_id"] == "task-runtime-91"
    assert result_envelopes[0]["payload"] == result_envelopes[1]["payload"]


def test_connected_session_tooling_plane_tool_commands_overlap_and_respect_parallel_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_MAX_PARALLEL_COMMANDS_PER_TASK": "1",
        }
    )
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    client = RunnerCloudClient(config=config)
    client._composition._job_store = job_store

    entered_dispatch = 0
    peak_executing = 0
    executing = 0
    lock = threading.Lock()
    second_dispatch_entered = threading.Event()

    class _FakeToolOperationService:
        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal entered_dispatch, peak_executing, executing
            command_id = str(params.get("command_id") or "")
            if operation == "submit_tool_command":
                with lock:
                    entered_dispatch += 1
                    executing += 1
                    peak_executing = max(peak_executing, executing)
                    if entered_dispatch >= 2:
                        second_dispatch_entered.set()
                return {
                    "accepted": True,
                    "status": "running",
                    "metadata": {
                        "runtime_job_id": str(params.get("runtime_job_id") or ""),
                        "command_id": command_id,
                        "terminal": False,
                    },
                }
            assert operation == "get_tool_command_result"
            if command_id == "cmd-1" and not second_dispatch_entered.is_set():
                return {
                    "accepted": True,
                    "status": "running",
                    "metadata": {
                        "runtime_job_id": str(params.get("runtime_job_id") or ""),
                        "command_id": command_id,
                        "terminal": False,
                    },
                }
            with lock:
                executing = max(0, executing - 1)
            return {
                "accepted": True,
                "status": "completed",
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": command_id,
                    "exit_code": 0,
                    "stdout": command_id,
                    "stderr": "",
                    "artifacts": [],
                    "terminal": True,
                },
            }

    client._composition._operation_service = _FakeToolOperationService()  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 22, 0, tzinfo=UTC)] * 16

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 22, 2, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-overlap-1",
                    runtime_job_id="tool-command-job-overlap-1",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-1",
                ),
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-overlap-2",
                    runtime_job_id="tool-command-job-overlap-2",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-2",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            result_count = sum(
                1
                for payload in self.sent
                if json.loads(payload).get("type") == "tool.result"
            )
            if result_count >= 2:
                raise KeyboardInterrupt
            time.sleep(0.01)
            raise TimeoutError

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert entered_dispatch == 2
    assert peak_executing == 2
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payloads = [payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack"]
    assert len(ack_payloads) == 2
    assert all(payload["status"] == "accepted" for payload in ack_payloads)
    result_envelopes = [payload for payload in sent_payloads if payload["type"] == "tool.result"]
    assert len(result_envelopes) == 2


def test_connected_session_tooling_plane_tool_command_dispatch_timeout_emits_failed_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "TOOL_COMMAND_TIMEOUT",
                "error_message": "command timed out",
                "metadata": {
                    "runtime_job_id": str(params.get("runtime_job_id") or ""),
                    "command_id": str(params.get("command_id") or ""),
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": "deadline exceeded",
                    "artifacts": [],
                },
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 25, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 25, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id="msg-tooling-plane-tool-timeout",
                    runtime_job_id="tool-command-job-timeout",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id="cmd-timeout",
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert len(fake_operation_service.calls) == 1
    operation, params = fake_operation_service.calls[0]
    assert operation == "submit_tool_command"
    assert params["runtime_job_id"] == "task-runtime-91"

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"

    result_envelope = next(payload for payload in sent_payloads if payload["type"] == "tool.result")
    assert result_envelope["runtime_job_id"] == "tool-command-job-timeout"
    assert result_envelope["payload"]["status"] == "failed"
    assert result_envelope["payload"]["success"] is False
    assert result_envelope["payload"]["error_code"] == "TOOL_COMMAND_TIMEOUT"
    assert result_envelope["payload"]["error_message"] == "command timed out"
    assert result_envelope["payload"]["metadata"]["task_runtime_job_id"] == "task-runtime-91"


def test_data_plane_upload_failure_emits_tool_result_metadata_for_unpromoted_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeUploader:
        @staticmethod
        def upload(**kwargs):  # noqa: ANN003, ANN202
            del kwargs
            return ArtifactUploadBatchResult(
                completed=(),
                failures=(
                    ArtifactUploadFailure(
                        artifact_id="11111111-1111-1111-1111-111111111111",
                        artifact_client_id="artifact-client-1",
                        object_key="data-plane-prefix/tenants/3/tasks/91/artifacts/1/stdout.txt",
                        error_code="ARTIFACT_UPLOAD_FAILED",
                        message="Upload attempt 1 failed (URLError).",
                    ),
                ),
            )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

    client._artifact_uploader = _FakeUploader()  # type: ignore[assignment]
    command_key = ("task-runtime-91", "cmd-91")
    cached_tool_command_results = {
        command_key: _ToolCommandCacheEntry(  # type: ignore[attr-defined]
            task_runtime_job_id="task-runtime-91",
            command_id="cmd-91",
            tool_command_runtime_job_id="tool-command-job-91",
            task_id=91,
            result_payload=RunnerToolResultPayload(
                operation_id="tool-op-91",
                command_id="cmd-91",
                tool="shell.exec",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                artifacts=("artifacts/cmd-91/stdout.txt",),
                error_code=None,
                error_message=None,
                result={},
                metadata={"task_runtime_job_id": "task-runtime-91"},
            ),
            workspace_id="task-91",
            tool_call_id="tool-call-91",
            tool_batch_id="tool-batch-91",
            manifest_payload=RunnerArtifactManifestPayload(
                task_runtime_job_id="task-runtime-91",
                command_id="cmd-91",
                workspace_id="task-91",
                tool_call_id="tool-call-91",
                tool_batch_id="tool-batch-91",
                artifacts=(
                    RunnerArtifactManifestItem(
                        artifact_client_id="artifact-client-1",
                        relative_path="artifacts/cmd-91/stdout.txt",
                        artifact_kind="stdout",
                        size_bytes=3,
                        content_sha256="dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22",
                        content_type="text/plain",
                        is_text=True,
                        created_at=None,
                        metadata={},
                    ),
                ),
            ),
            files_by_client_id={},
            upload_completions_by_object_key={},
        )
    }
    pending_upload_contexts = {
        command_key: _PendingArtifactUploadContext(  # type: ignore[attr-defined]
            tool_command_runtime_job_id="tool-command-job-91",
            task_id=91,
            manifest_payload=cached_tool_command_results[command_key].manifest_payload,
            files_by_client_id={},
            upload_completions_by_object_key={},
        )
    }
    inbound = parse_runner_envelope_json(
        json.dumps(
            {
                "message_id": "msg-data-plane-upload-request-1",
                "type": "artifact.upload.request",
                "schema_version": "data_plane.v1",
                "tenant_id": "3",
                "runner_id": "runner-3",
                "correlation_id": "corr-data-plane-upload-request-1",
                "runtime_job_id": "tool-command-job-91",
                "task_id": 91,
                "created_at": "2026-05-25T12:00:00+00:00",
                "payload": {
                    "task_runtime_job_id": "task-runtime-91",
                    "command_id": "cmd-91",
                    "workspace_id": "task-91",
                    "tool_call_id": "tool-call-91",
                    "tool_batch_id": "tool-batch-91",
                    "uploads": [
                        {
                            "artifact_id": "11111111-1111-1111-1111-111111111111",
                            "artifact_client_id": "artifact-client-1",
                            "object_key": "data-plane-prefix/tenants/3/tasks/91/artifacts/1/stdout.txt",
                            "upload_url": "https://object.example.test/upload",
                            "upload_method": "PUT",
                            "upload_headers": {"x-test-signed": "1"},
                            "size_bytes": 3,
                            "content_sha256": "dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22",
                            "content_type": "text/plain",
                            "is_text": True,
                        }
                    ],
                },
            }
        )
    )

    session_state = ConnectionSessionState()
    session_state.cached_tool_command_results = cached_tool_command_results
    session_state.pending_upload_contexts = pending_upload_contexts
    websocket = _FakeWebSocket()
    upload_handler = ArtifactUploadHandler(artifact_uploader=client._artifact_uploader)  # type: ignore[arg-type]
    upload_handler.handle_upload(
        websocket=websocket,
        identity=CloudChannelIdentity(
            tenant_id=3,
            runner_id="runner-3",
            credential_secret="rsec_test",
            channel_endpoint="http://cloud.example.test/api/runner-control/channel",
            protocol_version="data_plane.v1",
            heartbeat_interval_seconds=30,
        ),
        inbound=inbound,
        session_state=session_state,
    )

    sent_payloads = [json.loads(payload) for payload in websocket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "accepted"

    follow_up_result = next(payload for payload in sent_payloads if payload["type"] == "tool.result")
    upload_metadata = follow_up_result["payload"]["metadata"]["artifact_upload"]
    assert upload_metadata["failed_count"] == 1
    assert upload_metadata["unpromoted_count"] == 1
    assert upload_metadata["failed_artifacts"][0]["artifact_client_id"] == "artifact-client-1"
    assert upload_metadata["unpromoted_artifacts"][0]["artifact_id"] == "11111111-1111-1111-1111-111111111111"

    cached_metadata = session_state.cached_tool_command_results[command_key].result_payload.metadata["artifact_upload"]
    assert cached_metadata["failed_count"] == 1
    assert cached_metadata["unpromoted_count"] == 1


def test_data_plane_upload_success_emits_promoted_artifact_metadata_for_follow_up_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    class _FakeUploader:
        @staticmethod
        def upload(**kwargs):  # noqa: ANN003, ANN202
            del kwargs
            return ArtifactUploadBatchResult(
                completed=(
                    RunnerArtifactUploadCompleteItem(
                        artifact_id="11111111-1111-1111-1111-111111111111",
                        artifact_client_id="artifact-client-1",
                        object_key="data-plane-prefix/tenants/3/tasks/91/artifacts/1/stdout.txt",
                        size_bytes=3,
                        content_sha256="dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22",
                        uploaded_at="2026-05-25T12:00:10+00:00",
                    ),
                ),
                failures=(),
            )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

    client._artifact_uploader = _FakeUploader()  # type: ignore[assignment]
    command_key = ("task-runtime-91", "cmd-91")
    cached_tool_command_results = {
        command_key: _ToolCommandCacheEntry(  # type: ignore[attr-defined]
            task_runtime_job_id="task-runtime-91",
            command_id="cmd-91",
            tool_command_runtime_job_id="tool-command-job-91",
            task_id=91,
            result_payload=RunnerToolResultPayload(
                operation_id="tool-op-91",
                command_id="cmd-91",
                tool="shell.exec",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                artifacts=("artifacts/cmd-91/stdout.txt",),
                error_code=None,
                error_message=None,
                result={},
                metadata={"task_runtime_job_id": "task-runtime-91"},
            ),
            workspace_id="task-91",
            tool_call_id="tool-call-91",
            tool_batch_id="tool-batch-91",
            manifest_payload=RunnerArtifactManifestPayload(
                task_runtime_job_id="task-runtime-91",
                command_id="cmd-91",
                workspace_id="task-91",
                tool_call_id="tool-call-91",
                tool_batch_id="tool-batch-91",
                artifacts=(
                    RunnerArtifactManifestItem(
                        artifact_client_id="artifact-client-1",
                        relative_path="artifacts/cmd-91/stdout.txt",
                        artifact_kind="stdout",
                        size_bytes=3,
                        content_sha256="dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22",
                        content_type="text/plain",
                        is_text=True,
                        created_at=None,
                        metadata={},
                    ),
                ),
            ),
            files_by_client_id={},
            upload_completions_by_object_key={},
        )
    }
    pending_upload_contexts = {
        command_key: _PendingArtifactUploadContext(  # type: ignore[attr-defined]
            tool_command_runtime_job_id="tool-command-job-91",
            task_id=91,
            manifest_payload=cached_tool_command_results[command_key].manifest_payload,
            files_by_client_id={},
            upload_completions_by_object_key={},
        )
    }
    inbound = parse_runner_envelope_json(
        json.dumps(
            {
                "message_id": "msg-data-plane-upload-request-success-1",
                "type": "artifact.upload.request",
                "schema_version": "data_plane.v1",
                "tenant_id": "3",
                "runner_id": "runner-3",
                "correlation_id": "corr-data-plane-upload-request-success-1",
                "runtime_job_id": "tool-command-job-91",
                "task_id": 91,
                "created_at": "2026-05-25T12:00:00+00:00",
                "payload": {
                    "task_runtime_job_id": "task-runtime-91",
                    "command_id": "cmd-91",
                    "workspace_id": "task-91",
                    "tool_call_id": "tool-call-91",
                    "tool_batch_id": "tool-batch-91",
                    "uploads": [
                        {
                            "artifact_id": "11111111-1111-1111-1111-111111111111",
                            "artifact_client_id": "artifact-client-1",
                            "object_key": "data-plane-prefix/tenants/3/tasks/91/artifacts/1/stdout.txt",
                            "upload_url": "https://object.example.test/upload",
                            "upload_method": "PUT",
                            "upload_headers": {"x-test-signed": "1"},
                            "size_bytes": 3,
                            "content_sha256": "dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22",
                            "content_type": "text/plain",
                            "is_text": True,
                        }
                    ],
                },
            }
        )
    )

    session_state = ConnectionSessionState()
    session_state.cached_tool_command_results = cached_tool_command_results
    session_state.pending_upload_contexts = pending_upload_contexts
    websocket = _FakeWebSocket()
    upload_handler = ArtifactUploadHandler(artifact_uploader=client._artifact_uploader)  # type: ignore[arg-type]
    upload_handler.handle_upload(
        websocket=websocket,
        identity=CloudChannelIdentity(
            tenant_id=3,
            runner_id="runner-3",
            credential_secret="rsec_test",
            channel_endpoint="http://cloud.example.test/api/runner-control/channel",
            protocol_version="data_plane.v1",
            heartbeat_interval_seconds=30,
        ),
        inbound=inbound,
        session_state=session_state,
    )

    sent_payloads = [json.loads(payload) for payload in websocket.sent]
    follow_up_result = next(payload for payload in sent_payloads if payload["type"] == "tool.result")
    metadata = follow_up_result["payload"]["metadata"]
    assert metadata["artifact_scope"] == "cloud_data_plane"
    assert metadata["artifact_promotion_status"] == "ready"
    assert metadata["promoted_artifact_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert metadata["artifact_refs"] == [
        {
            "artifact_id": "11111111-1111-1111-1111-111111111111",
            "artifact_client_id": "artifact-client-1",
            "relative_path": "artifacts/cmd-91/stdout.txt",
        }
    ]
    assert "object_key" not in metadata["artifact_refs"][0]
    assert "upload_url" not in metadata["artifact_refs"][0]

    upload_complete = next(payload for payload in sent_payloads if payload["type"] == "artifact.upload.complete")
    complete_upload = upload_complete["payload"]["uploads"][0]
    assert complete_upload["artifact_id"] == "11111111-1111-1111-1111-111111111111"
    assert complete_upload["object_key"] == "data-plane-prefix/tenants/3/tasks/91/artifacts/1/stdout.txt"


@pytest.mark.parametrize(
    ("secret_key", "secret_value"),
    [
        ("secret_refs", ["vault://runner/db-password"]),
        ("secret_resolution", {"provider": "vault", "path": "kv/task-91"}),
    ],
)
def test_connected_session_tooling_plane_tool_command_rejects_secret_reference_params_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret_key: str,
    secret_value: object,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    _seed_runner_job(
        config=config,
        runtime_job_id="task-runtime-91",
        tenant_id="3",
        task_id="91",
        workspace_id="task-91",
    )
    client = RunnerCloudClient(config=config)
    client._composition._job_store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")

    class _FakeToolOperationService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def dispatch_operation(self, *, operation: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((operation, dict(params)))
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {"runtime_job_id": str(params.get("runtime_job_id") or "")},
            }

    fake_operation_service = _FakeToolOperationService()
    client._composition._operation_service = fake_operation_service  # type: ignore[assignment]

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 28, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 28, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [
                _tooling_plane_tool_command_message(
                    tenant_id=3,
                    runner_id="runner-3",
                    message_id=f"msg-tooling-plane-tool-secret-{secret_key}",
                    runtime_job_id=f"tool-command-job-secret-{secret_key}",
                    task_id=91,
                    task_runtime_job_id="task-runtime-91",
                    workspace_id="task-91",
                    command_id=f"cmd-secret-{secret_key}",
                    params={"cwd": "/workspace", secret_key: secret_value},
                ),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    assert fake_operation_service.calls == []
    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "rejected"
    assert ack_payload["error_code"] == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE
    assert not any(payload["type"] == "tool.result" for payload in sent_payloads)


def test_connected_session_tooling_plane_tool_command_parse_error_sends_rejected_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_RUNNER_TENANT_ID", "3")
    config = _build_cloud_config(tmp_path)
    client = RunnerCloudClient(config=config)

    malformed_tool_command = json.dumps(
        {
            "message_id": "msg-tooling-plane-invalid",
            "type": "tool.command",
            "schema_version": "tooling_plane.v1",
            "tenant_id": "3",
            "runner_id": "runner-3",
            "correlation_id": "corr-invalid",
            "runtime_job_id": "tool-command-invalid",
            "task_id": 91,
            "created_at": "2026-05-24T12:30:00+00:00",
            "payload": {
                "operation_id": "op-invalid",
                "workspace_id": "task-91",
                "task_runtime_job_id": "task-runtime-91",
                "runtime_image": "drowai-runtime-local:latest",
                "command": "id",
                "cwd": "/workspace",
                "env": {},
                "command_id": "cmd-invalid",
                "timeout_seconds": 30.0,
                "timeout_policy": {"deadline_seconds": 30.0, "grace_seconds": 1.0},
                "route_policy": {
                    "selected_lane": "container_scoped",
                    "selected_authority": "container_runner_transport",
                },
                "delivery_policy": {"offline": "queue"},
                "tool_call_id": "tool-call-1",
                "tool_batch_id": "tool-batch-1",
                "execution_strategy": "per_call",
                "params": {"cwd": "/workspace"},
            },
        }
    )

    class _FakeDateTime:
        _ticks = [datetime(2026, 5, 24, 12, 30, 0, tzinfo=UTC)] * 4

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            value = cls._ticks.pop(0) if cls._ticks else datetime(2026, 5, 24, 12, 30, 1, tzinfo=UTC)
            if tz is not None:
                return value.astimezone(tz)
            return value

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._messages = [malformed_tool_command]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float):  # noqa: ANN201
            del timeout
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
            del exc_type, exc, tb
            return False

    fake_socket = _FakeWebSocket()
    monkeypatch.setattr(cloud_client_pump, "datetime", _FakeDateTime)
    monkeypatch.setattr(connection_module, "ws_connect", lambda *args, **kwargs: fake_socket)

    identity = CloudChannelIdentity(
        tenant_id=3,
        runner_id="runner-3",
        credential_secret="rsec_test",
        channel_endpoint="http://cloud.example.test/api/runner-control/channel",
        protocol_version="tooling_plane.v1",
        heartbeat_interval_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        client._session_pump.run(identity)

    sent_payloads = [json.loads(payload) for payload in fake_socket.sent]
    ack_payload = next(payload["payload"] for payload in sent_payloads if payload["type"] == "runner.ack")
    assert ack_payload["status"] == "rejected"
    assert ack_payload["error_code"] == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE
