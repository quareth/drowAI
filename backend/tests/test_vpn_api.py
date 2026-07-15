"""API regression tests for task VPN configure/status/retry endpoints."""

import json

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

from backend.main import app
from backend.database import SessionLocal
from backend.models.core import User, Task
from backend.models.tenant import Tenant, TenantMembership
from backend.routers.tasks.vpn import _runtime_is_ready_for_vpn_operations

pytestmark = pytest.mark.execution_plane_non_dind_regression


client = TestClient(app)


def test_vpn_runtime_operations_require_an_executable_runtime_state() -> None:
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="runner", status="starting")
    ) is False
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="runner", status="running")
    ) is True
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="runner", status="paused")
    ) is False
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="local", status="starting")
    ) is False
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="local", status="paused")
    ) is False
    assert _runtime_is_ready_for_vpn_operations(
        SimpleNamespace(runtime_placement_mode="local", status="running")
    ) is True


def _ensure_user_and_task(db):
    tenant = db.query(Tenant).filter(Tenant.slug == "vpn-test").first()
    if not tenant:
        tenant = Tenant(slug="vpn-test", name="VPN Test Tenant", status="active")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
    user = db.query(User).filter(User.username == "testuser").first()
    if not user:
        user = User(username="testuser", password="x", email="t@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)
    membership = (
        db.query(TenantMembership)
        .filter(TenantMembership.tenant_id == tenant.id, TenantMembership.user_id == user.id)
        .first()
    )
    if not membership:
        db.add(
            TenantMembership(
                tenant_id=tenant.id,
                user_id=user.id,
                role="owner",
                status="active",
            )
        )
        db.commit()
    task = Task(user_id=user.id, tenant_id=tenant.id, name="vpn-task")
    db.add(task)
    db.commit()
    db.refresh(task)
    return user, task


def _mark_runtime_running(task, *, mode: str = "runner") -> None:
    task.status = "running"
    task.runtime_placement_mode = mode


def test_configure_vpn_endpoint_defers_prestart_local_runtime_work(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        provider_called = False

        async def _forbidden_provider_call(self, _request):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("local provider must not materialize product VPN config")

        monkeypatch.setattr(
            "backend.services.runtime_provider.local_docker_provider.LocalDockerRuntimeProvider.materialize_vpn_config",
            _forbidden_provider_call,
        )

        payload = {"provider": "custom", "config_data": "client\nremote 1.2.3.4 1194\ndev tun\n" + ("a" * 60)}
        resp = client.post(
            f"/api/tasks/{task.id}/vpn/configure",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["runtime_materialized"] is False
        assert provider_called is False
        db.refresh(task)
        assert task.vpn_enabled is True
        assert task.vpn_connection_status == "configured"
    finally:
        db.close()


def test_configure_vpn_endpoint_preserves_provider_timeout_semantics(monkeypatch):
    """Runner VPN materialization wait failures should return deterministic timeout details."""

    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        _mark_runtime_running(task)
        db.commit()
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
            **_kwargs,
        ):
            _ = (self, task, user_id, operation, call, payload, metadata, runtime_call_scope)
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="vpn materialization timeout",
                metadata={},
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        payload = {"provider": "custom", "config_data": "client\nremote 1.2.3.4 1194\ndev tun\n" + ("a" * 60)}
        resp = client.post(
            f"/api/tasks/{task.id}/vpn/configure",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

        assert resp.status_code == 504, resp.text
        assert "RUNNER_OPERATION_RESULT_TIMEOUT" in resp.json()["detail"]
    finally:
        db.close()


def test_configure_vpn_endpoint_passes_json_serializable_runtime_metadata(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        _mark_runtime_running(task)
        db.commit()
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        captured = {}

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
            **_kwargs,
        ):
            _ = (self, task, user_id, operation, call, payload, runtime_call_scope)
            captured["metadata"] = metadata or {}
            json.dumps(captured["metadata"])
            return SimpleNamespace(ok=True, metadata={})

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        payload = {
            "provider": "custom",
            "config_data": "client\nremote 1.2.3.4 1194\ndev tun\n" + ("a" * 60),
        }
        resp = client.post(
            f"/api/tasks/{task.id}/vpn/configure",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

        assert resp.status_code in (200, 201), resp.text
        assert "db_session" not in captured["metadata"]
        json.dumps(captured["metadata"])
    finally:
        db.close()


def test_retry_vpn_endpoint_rejects_prestart_runtime_before_provider(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        provider_called = False

        async def _forbidden_provider_call(self, _request):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("local provider must not retry product VPN")

        monkeypatch.setattr(
            "backend.services.runtime_provider.local_docker_provider.LocalDockerRuntimeProvider.retry_vpn_connection",
            _forbidden_provider_call,
        )

        resp = client.post(
            f"/api/tasks/{task.id}/vpn/retry",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 409, resp.text
        assert "must be running" in resp.json()["detail"]
        assert provider_called is False
    finally:
        db.close()


def test_vpn_status_endpoint_returns_persisted_prestart_state_without_provider(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        task.vpn_connection_status = "configured"
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        provider_called = False

        async def _forbidden_provider_call(self, _request):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("local provider must not check product VPN status")

        monkeypatch.setattr(
            "backend.services.runtime_provider.local_docker_provider.LocalDockerRuntimeProvider.check_vpn_status",
            _forbidden_provider_call,
        )

        resp = client.get(
            f"/api/tasks/{task.id}/vpn/status",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["connection_status"] == "configured"
        assert provider_called is False
    finally:
        db.close()


def test_retry_vpn_endpoint_requires_vpn_configuration():
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        resp = client.post(
            f"/api/tasks/{task.id}/vpn/retry",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 400
        assert "VPN not configured" in resp.text
    finally:
        db.close()


def test_upload_vpn_endpoint_preserves_provider_failure_semantics(monkeypatch):
    """VPN upload should surface provider failure code/detail from materialization wait."""

    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        _mark_runtime_running(task)
        db.commit()
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
            **_kwargs,
        ):
            _ = (self, task, user_id, operation, call, payload, metadata, runtime_call_scope)
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="failed"),
                error_code="RUNNER_RUNTIME_OPERATION_FAILED",
                error_message="vpn materialization failed",
                metadata={},
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        resp = client.post(
            f"/api/tasks/{task.id}/vpn/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("task.ovpn", b"client\nremote 2.2.2.2 1194\ndev tun\n" + (b"a" * 60), "text/plain")},
        )

        assert resp.status_code == 503, resp.text
        assert "RUNNER_RUNTIME_OPERATION_FAILED" in resp.json()["detail"]
    finally:
        db.close()


def test_upload_vpn_endpoint_defers_prestart_local_runtime_work(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        provider_called = False

        async def _forbidden_provider_call(self, _request):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("pre-start local VPN upload must not reach the provider")

        monkeypatch.setattr(
            "backend.services.runtime_provider.local_docker_provider.LocalDockerRuntimeProvider.materialize_vpn_config",
            _forbidden_provider_call,
        )

        resp = client.post(
            f"/api/tasks/{task.id}/vpn/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={
                "file": (
                    "task.ovpn",
                    b"client\nremote 2.2.2.2 1194\ndev tun\n" + (b"a" * 60),
                    "text/plain",
                )
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["runtime_materialized"] is False
        assert provider_called is False
        db.refresh(task)
        assert task.vpn_enabled is True
        assert task.vpn_connection_status == "configured"
    finally:
        db.close()


def test_retry_vpn_endpoint_preserves_provider_timeout_semantics(monkeypatch):
    """VPN retry should not wrap deterministic provider timeout errors in generic 500s."""

    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        _mark_runtime_running(task)
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
            **_kwargs,
        ):
            _ = (self, task, user_id, operation, call, payload, metadata, runtime_call_scope)
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="vpn retry timed out",
                metadata={},
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        resp = client.post(
            f"/api/tasks/{task.id}/vpn/retry",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 504, resp.text
        assert "RUNNER_OPERATION_RESULT_TIMEOUT" in resp.json()["detail"]
    finally:
        db.close()


def test_vpn_status_endpoint_uses_provider_check_vpn_status_contract(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        task.vpn_connection_status = "configured"
        _mark_runtime_running(task)
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})
        captured = {"provider_called": False}

        class _ProviderStub:
            async def check_vpn_status(self, _request):
                captured["provider_called"] = True
                return SimpleNamespace()

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
        ):
            _ = (self, runtime_call_scope)
            captured["task_id"] = task.id
            captured["user_id"] = user_id
            captured["operation"] = operation
            captured["payload"] = payload
            captured["metadata"] = metadata
            await call(_ProviderStub(), SimpleNamespace())
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": [
                        {"message": "__DROWAI_VPN_STATUS__=connected|10.8.0.11"}
                    ]
                },
                error_message=None,
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        resp = client.get(
            f"/api/tasks/{task.id}/vpn/status",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["connection_status"] == "connected"
        assert body["ip_address"] == "10.8.0.11"
        assert captured["provider_called"] is True
        assert captured["operation"] == "check_vpn_status"
        assert captured["payload"]["command"] == "bash /opt/drowai/runtime/vpn/vpn-manager.sh status"
        assert captured["metadata"]["wait_for_result"] is True
    finally:
        db.close()


def test_vpn_status_endpoint_fails_closed_for_runner_tasks_when_provider_status_probe_fails(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        task.vpn_connection_status = "configured"
        _mark_runtime_running(task)
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
        ):
            _ = (self, task, user_id, operation, call, payload, metadata, runtime_call_scope)
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="runtime status unavailable",
                metadata={},
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        resp = client.get(
            f"/api/tasks/{task.id}/vpn/status",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 504, resp.text
        assert "RUNNER_OPERATION_RESULT_TIMEOUT" in resp.json()["detail"]
    finally:
        db.close()


def test_vpn_status_endpoint_reads_runner_stdout_sentinel_payload(monkeypatch):
    db = SessionLocal()
    try:
        user, task = _ensure_user_and_task(db)
        task.vpn_enabled = True
        task.vpn_connection_status = "configured"
        _mark_runtime_running(task)
        db.add(task)
        db.commit()

        from backend.auth import create_access_token

        token = create_access_token({"sub": user.username, "user_id": user.id})

        async def _fake_run_authorized_task_operation(
            self,
            *,
            task,
            user_id,
            operation,
            call,
            payload=None,
            metadata=None,
            runtime_call_scope=None,
        ):
            _ = (self, task, user_id, operation, call, payload, metadata, runtime_call_scope)
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "stdout": "__DROWAI_VPN_STATUS__=connected|10.8.0.44",
                        "stderr": "",
                        "exit_code": 0,
                    }
                },
                error_message=None,
            )

        monkeypatch.setattr(
            "backend.routers.tasks.vpn.RuntimeOperationService.run_authorized_task_operation",
            _fake_run_authorized_task_operation,
        )

        resp = client.get(
            f"/api/tasks/{task.id}/vpn/status",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["connection_status"] == "connected"
        assert body["ip_address"] == "10.8.0.44"
    finally:
        db.close()
