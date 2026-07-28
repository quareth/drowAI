"""Router tests for process-local subagent-run status/cancel endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.routers import tasks as composed_task_routes
from backend.routers.tasks import agent_runs as agent_run_routes
from backend.services.agent_runs.contracts import AgentAssignment, AgentRuntimeIdentity
from backend.services.agent_runs.control import AgentRunControlService
from backend.services.agent_runs.registry import LocalAgentRun, ProcessLocalAgentRunRegistry


def _runtime_identity(*, tenant_id: int = 7, task_id: int = 42) -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=f"task-{task_id}",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
    )


def _assignment(
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    agent_run_id: str = "pathfinder-run-1",
) -> AgentAssignment:
    return AgentAssignment(
        assignment_id=f"assignment-{agent_run_id}",
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=task_id,
        tenant_id=tenant_id,
        conversation_id=f"conversation-{task_id}",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map open services on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["port_scan"],
        scope_summary="Approved internal target only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )


class _RecordingLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.calls: list[tuple[int, int, str]] = []

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        self.calls.append((tenant_id, task_id, agent_run_id))
        return await self.registry.request_cancellation(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )


@pytest.fixture
def agent_run_client(monkeypatch: pytest.MonkeyPatch):
    registry = ProcessLocalAgentRunRegistry()
    launcher = _RecordingLauncher(registry)
    service = AgentRunControlService(registry=registry, launcher=launcher)
    app = FastAPI()
    app.include_router(agent_run_routes.router, prefix="/api/tasks")

    task_lookups: list[tuple[int, int]] = []

    def _fake_get_tenant_task_or_404(*, db, task_id, tenant_context):
        del db
        task_lookups.append((int(tenant_context.tenant_id), int(task_id)))
        return SimpleNamespace(
            id=task_id,
            tenant_id=tenant_context.tenant_id,
            user_id=tenant_context.user_id,
        )

    def _fake_get_db():
        yield object()

    monkeypatch.setattr(
        agent_run_routes,
        "get_tenant_task_or_404",
        _fake_get_tenant_task_or_404,
    )
    app.dependency_overrides[agent_run_routes.get_db] = _fake_get_db
    app.dependency_overrides[agent_run_routes.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=7, user_id=3, role="operator")
    )
    app.dependency_overrides[agent_run_routes.get_agent_run_control_service] = (
        lambda: service
    )

    client = TestClient(app)
    try:
        yield client, registry, launcher, task_lookups, app
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_composed_tasks_router_registers_agent_run_endpoints() -> None:
    assert (
        str(composed_task_routes.router.url_path_for("list_local_agent_runs", task_id=42))
        == "/42/agent-runs/local"
    )
    assert (
        str(
            composed_task_routes.router.url_path_for(
                "cancel_local_agent_run",
                task_id=42,
                agent_run_id="pathfinder-run-1",
            )
        )
        == "/42/agent-runs/pathfinder-run-1/cancel"
    )


@pytest.mark.asyncio
async def test_list_local_agent_runs_reports_only_authorized_task_scope(
    agent_run_client,
) -> None:
    client, registry, _launcher, task_lookups, _app = agent_run_client
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.register(
        _assignment(tenant_id=8, task_id=42, agent_run_id="other-tenant-run"),
        graph_thread_id="child-thread-2",
    )

    response = client.get("/api/tasks/42/agent-runs/local")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["process_local"] is True
    assert payload["task_id"] == 42
    assert task_lookups == [(7, 42)]
    assert [run["agent_run_id"] for run in payload["agent_runs"]] == ["pathfinder-run-1"]
    run = payload["agent_runs"][0]
    assert run["agent_display_name"] == "Pathfinder"
    assert run["status"] == "queued"
    assert run["cancel_requested"] is False
    assert run["assignment"]["objective"] == "Map open services on the approved target."
    assert "task_handle" not in run
    assert "tenant_id" not in run


@pytest.mark.asyncio
async def test_cancel_local_agent_run_uses_scoped_launcher_and_sets_flag(
    agent_run_client,
) -> None:
    client, registry, launcher, _task_lookups, _app = agent_run_client
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")

    response = client.post("/api/tasks/42/agent-runs/pathfinder-run-1/cancel")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["process_local"] is True
    assert payload["cancelled"] is True
    assert payload["agent_run"]["agent_run_id"] == "pathfinder-run-1"
    assert payload["agent_run"]["status"] == "running"
    assert payload["agent_run"]["cancel_requested"] is True
    assert launcher.calls == [(7, 42, "pathfinder-run-1")]
    stored = await registry.get(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")
    assert stored is not None
    assert stored.cancel_requested is True


@pytest.mark.asyncio
async def test_cancel_waiting_agent_run_returns_terminal_cancelled_projection(
    agent_run_client,
) -> None:
    client, registry, launcher, _task_lookups, _app = agent_run_client
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )

    response = client.post("/api/tasks/42/agent-runs/pathfinder-run-1/cancel")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cancelled"] is True
    assert payload["agent_run"]["status"] == "cancelled"
    assert payload["agent_run"]["cancel_requested"] is True
    assert launcher.calls == [(7, 42, "pathfinder-run-1")]
    stored = await registry.get(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.task_handle is None
    assert stored.cancel_requested is True


def test_cancel_missing_process_local_run_returns_explicit_pilot_404(
    agent_run_client,
) -> None:
    client, _registry, launcher, _task_lookups, _app = agent_run_client

    response = client.post("/api/tasks/42/agent-runs/missing-run/cancel")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["reason_code"] == "agent_run_not_active_in_process"
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_cancel_terminal_process_local_run_returns_not_active_conflict(
    agent_run_client,
) -> None:
    client, registry, launcher, _task_lookups, _app = agent_run_client
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_cancelled(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")

    response = client.post("/api/tasks/42/agent-runs/pathfinder-run-1/cancel")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason_code"] == "agent_run_not_active"
    assert launcher.calls == []
