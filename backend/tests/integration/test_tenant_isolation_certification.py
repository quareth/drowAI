"""Cross-tenant certification matrix for final tenant-isolation checks.

Responsibilities:
- Certify cross-tenant API/object/runner/websocket reads fail closed.
- Certify same-tenant allowed role flows still succeed.
- Certify standalone default-tenant compatibility remains intact.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.websocket import gateway as ws_gateway
from backend.services.tenant.context import TenantRequestContext
from backend.tests.routers.test_artifact_provenance_tenant_authz import artifact_authz_client
from backend.tests.routers.test_engagement_knowledge_router import api_client as engagement_knowledge_client
from backend.tests.routers.test_reports_router_tenant_authz import reports_client
from backend.tests.routers.test_tasks_tenant_authz import tasks_authz_client
from backend.tests.routers.test_usage_router_tenant_authz import usage_client
from backend.tests.services.runner_control.test_message_idempotency import (
    _build_session,
    _envelope_json,
    _issue_credential_id,
    _seed_runner,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict[str, object]] = []
        self.close_events: list[tuple[int | None, str | None]] = []
        self.state = type("State", (), {})()

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


def test_cross_tenant_api_reads_fail_consistently(
    tasks_authz_client: TestClient,
    reports_client,
    usage_client,
    artifact_authz_client: TestClient,
    engagement_knowledge_client,
) -> None:
    reports_api_client, reports_seeded = reports_client
    usage_api_client, usage_seeded = usage_client
    engagement_api_client, engagement_seeded = engagement_knowledge_client

    responses = [
        tasks_authz_client.get("/api/tasks/22/scope", headers={"Authorization": "Bearer owner-token"}),
        reports_api_client.get(
            f"/api/reports/task/{reports_seeded['foreign_task_id']}",
            headers={"Authorization": "Bearer owner-token"},
        ),
        usage_api_client.get(
            f"/api/tasks/{usage_seeded['foreign_task_id']}/usage",
            headers={"Authorization": "Bearer owner-token"},
        ),
        artifact_authz_client.get(
            "/api/artifact-provenance/tasks/21/executions/cross-tenant",
            headers={"Authorization": "Bearer owner-token"},
        ),
    ]

    assert all(response.status_code == 404 for response in responses)
    assert all(response.json().get("detail") == "Task not found" for response in responses)

    evidence_response = engagement_api_client.post(
        (
            f"/api/engagements/{engagement_seeded['engagement_id']}"
            f"/evidence/{engagement_seeded['foreign_evidence_id']}/read"
        ),
        json={"mode": "head", "max_chars": 4},
        headers={"Authorization": "Bearer owner-token"},
    )

    assert evidence_response.status_code == 200, evidence_response.text
    evidence_payload = evidence_response.json()
    assert evidence_payload["status"] == "not_found"
    assert evidence_payload["source"] == "none"
    assert evidence_payload["content"] is None


def test_same_user_switching_between_two_tenants_stays_tenant_scoped(
    tasks_authz_client: TestClient,
) -> None:
    tenant_a_allowed = tasks_authz_client.get(
        "/api/tasks/31/scope",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    tenant_a_denied = tasks_authz_client.get(
        "/api/tasks/22/scope",
        headers={"Authorization": "Bearer default-owner-token"},
    )

    tenant_b_allowed = tasks_authz_client.get(
        "/api/tasks/22/scope",
        headers={"Authorization": "Bearer foreign-token"},
    )
    tenant_b_denied = tasks_authz_client.get(
        "/api/tasks/31/scope",
        headers={"Authorization": "Bearer foreign-token"},
    )

    assert tenant_a_allowed.status_code == 200, tenant_a_allowed.text
    assert tenant_b_allowed.status_code == 200, tenant_b_allowed.text
    assert tenant_a_denied.status_code == 404, tenant_a_denied.text
    assert tenant_b_denied.status_code == 404, tenant_b_denied.text


@pytest.mark.asyncio
async def test_cross_tenant_websocket_subscriptions_fail_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    websocket.state.tenant_context = TenantRequestContext(
        tenant_id=701,
        user_id=1,
        role="owner",
        membership_id=10,
        is_default_tenant=False,
        source="query",
    )

    monkeypatch.setattr(ws_gateway, "is_ws_task_in_tenant", lambda **_kwargs: False)

    allowed = await ws_gateway.enforce_ws_task_ownership(
        websocket,
        connection_type="agent",
        task_id=22,
        user_id=1,
        close_on_forbidden=True,
    )

    assert allowed is False
    assert websocket.sent_payloads == [{"type": "error", "message": "forbidden_task", "taskId": 22}]
    assert websocket.close_events == [(1008, "Forbidden")]


def test_cross_tenant_runner_messages_fail_consistently() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    mismatched_tenant_envelope = _envelope_json(
        message_id="cross-tenant-runner-message",
        tenant_id=tenant.id + 1,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "cert"}},
    )
    result = manager.handle_inbound_json(session, mismatched_tenant_envelope)

    assert result.should_close is True
    assert result.close_code == 1008
    assert result.response_envelopes
    assert result.response_envelopes[0].payload.error_code == "RUNNER_IDENTITY_MISMATCH"


def test_cross_tenant_object_reads_fail_consistently(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/21/executions/object-read-cross-tenant",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_same_tenant_owner_allowed_role_paths_continue_to_work(
    tasks_authz_client: TestClient,
    artifact_authz_client: TestClient,
) -> None:
    scope_response = tasks_authz_client.get(
        "/api/tasks/11/scope",
        headers={"Authorization": "Bearer owner-token"},
    )
    artifact_response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/11/executions/same-tenant",
        headers={"Authorization": "Bearer owner-token"},
    )

    assert scope_response.status_code == 200, scope_response.text
    assert artifact_response.status_code == 200, artifact_response.text


def test_same_tenant_non_owner_paths_fail_without_leakage(
    tasks_authz_client: TestClient,
    artifact_authz_client: TestClient,
) -> None:
    scope_response = tasks_authz_client.get(
        "/api/tasks/11/scope",
        headers={"Authorization": "Bearer viewer-token"},
    )
    artifact_response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/11/executions/same-tenant",
        headers={"Authorization": "Bearer viewer-token"},
    )

    assert scope_response.status_code == 404, scope_response.text
    assert artifact_response.status_code == 404, artifact_response.text


def test_standalone_default_tenant_certification_remains_green(
    tasks_authz_client: TestClient,
    artifact_authz_client: TestClient,
) -> None:
    task_response = tasks_authz_client.get(
        "/api/tasks/containers/list",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    artifact_response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/31/executions/default-tenant",
        headers={"Authorization": "Bearer default-owner-token"},
    )

    assert task_response.status_code == 200, task_response.text
    assert tuple(task_response.json().keys()) == ("containers", "total")

    assert artifact_response.status_code == 200, artifact_response.text
    assert tuple(artifact_response.json().keys()) == ("execution", "artifacts")
    assert artifact_response.json()["execution"]["tenant_id"] == 1
