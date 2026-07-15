"""
API tests for artifact provenance router endpoints.

These tests validate endpoint routing, ownership checks, task-scoped tool call
lookups, and query parameter behavior for execution/artifact retrieval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.routers import artifact_provenance as artifact_routes
from backend.services.artifact.memory_service import (
    ArtifactCatalogEntry,
    ArtifactCatalogPage,
    ArtifactReadResult,
)


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: (tmp_path / f"task-{int(task_id)}").resolve()),
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="artifact-owner", password="secret")
        other = User(username="artifact-other", password="secret")
        db.add(owner)
        db.add(other)
        db.flush()

        owner_task = Task(user_id=owner.id, tenant_id=701, name="owner-task")
        other_task = Task(user_id=other.id, tenant_id=702, name="other-task")
        db.add(owner_task)
        db.add(other_task)
        db.flush()

        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        t0 = datetime.now(timezone.utc)

        owner_exec_one = execution_repo.create(
            task_id=owner_task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo owner"},
            agent_path="langgraph",
            status="success",
            started_at=t0,
            finished_at=t0 + timedelta(seconds=1),
            duration_ms=1000,
            tool_call_id="tc-shared",
            conversation_id="conv-1",
            turn_id="turn-1",
            turn_sequence=1,
        )
        owner_exec_two = execution_repo.create(
            task_id=owner_task.id,
            tool_name="filesystem.read_file",
            tool_arguments={"path": "README.md"},
            agent_path="langgraph",
            status="error",
            started_at=t0 + timedelta(seconds=2),
            finished_at=t0 + timedelta(seconds=3),
            duration_ms=1000,
            tool_call_id="tc-owner-2",
            conversation_id="conv-1",
            turn_id="turn-2",
            turn_sequence=2,
            workspace_path=str(tmp_path / f"task-{owner_task.id}"),
        )
        other_exec = execution_repo.create(
            task_id=other_task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo other"},
            agent_path="langgraph",
            status="success",
            started_at=t0 + timedelta(seconds=4),
            tool_call_id="tc-shared",
            conversation_id="conv-9",
            turn_id="turn-9",
            turn_sequence=9,
        )

        created_artifacts = artifact_repo.create_batch(
            [
                {
                    "execution_id": owner_exec_one.id,
                    "task_id": owner_task.id,
                    "artifact_kind": "stdout",
                    "content_text": "owner output",
                    "content_sha256": artifact_repo.compute_content_hash("owner output"),
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": owner_exec_two.id,
                    "task_id": owner_task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/result.txt",
                    "content_sha256": artifact_repo.compute_content_hash("result"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": other_exec.id,
                    "task_id": other_task.id,
                    "artifact_kind": "stdout",
                    "content_text": "other output",
                    "content_sha256": artifact_repo.compute_content_hash("other output"),
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()
        owner_workspace = tmp_path / f"task-{owner_task.id}" / "artifacts"
        owner_workspace.mkdir(parents=True, exist_ok=True)
        (owner_workspace / "result.txt").write_text("router-file-content", encoding="utf-8")

        seeded = {
            "owner_id": owner.id,
            "other_id": other.id,
            "owner_task_id": owner_task.id,
            "other_task_id": other_task.id,
            "owner_exec_one_id": str(owner_exec_one.id),
            "owner_exec_two_id": str(owner_exec_two.id),
            "other_exec_id": str(other_exec.id),
            "owner_artifact_id": str(created_artifacts[0].id),
            "owner_file_artifact_id": str(created_artifacts[1].id),
            "other_artifact_id": str(created_artifacts[2].id),
        }

    app = FastAPI()
    app.include_router(artifact_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )
        token = auth_header.split(" ", 1)[1].strip()
        if token == "owner-token":
            return SimpleNamespace(id=seeded["owner_id"], username="owner", is_active=True)
        if token == "other-token":
            return SimpleNamespace(id=seeded["other_id"], username="other", is_active=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    app.dependency_overrides[artifact_routes.get_db] = fake_get_db
    app.dependency_overrides[artifact_routes.get_current_user] = fake_get_current_user
    def fake_get_tenant_request_context(request: Request):
        if request.headers.get("Authorization") == "Bearer owner-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["owner_id"], role="owner")
        return SimpleNamespace(tenant_id=702, user_id=seeded["other_id"], role="owner")

    app.dependency_overrides[artifact_routes.get_tenant_request_context] = fake_get_tenant_request_context

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_get_execution_by_id_returns_execution_for_owner(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions/{seeded['owner_exec_one_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["execution"]["execution_id"] == seeded["owner_exec_one_id"]
    assert len(data["artifacts"]) == 1
    assert "workspace_path" not in data["execution"]
    assert "container_path" not in data["execution"]
    assert "source_path" not in data["artifacts"][0]
    assert "fallback_path" not in data["artifacts"][0]


def test_get_execution_by_id_enforces_ownership(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions/{seeded['owner_exec_one_id']}",
        headers={"Authorization": "Bearer other-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_execution_by_id_is_task_scoped(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions/{seeded['other_exec_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Execution not found"


def test_get_execution_by_tool_call_is_task_scoped(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions/by-tool-call/tc-shared",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["execution"]["execution_id"] == seeded["owner_exec_one_id"]
    assert data["execution"]["raw_output"]["availability"] == "available"
    assert data["execution"]["raw_output"]["reason"] == "artifacts_present"
    assert isinstance(data["execution"]["raw_output"]["stdout_artifact_id"], str)
    assert data["execution"]["raw_output"]["stdout_artifact_id"].strip()
    assert isinstance(data["artifacts"], list)
    assert len(data["artifacts"]) > 0

    stdout_artifact = next(
        (artifact for artifact in data["artifacts"] if artifact.get("artifact_kind") == "stdout"),
        None,
    )
    assert stdout_artifact is not None
    assert isinstance(stdout_artifact.get("artifact_id"), str)
    assert stdout_artifact["artifact_id"].strip()
    assert stdout_artifact.get("artifact_kind") == "stdout"


def test_get_execution_by_tool_call_returns_not_found_contract(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions/by-tool-call/not-found-id",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Execution not found"


def test_get_task_executions_supports_filters_and_pagination(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/executions",
        params={"tool_name": "shell.exec", "status": "success", "limit": 1, "offset": 0},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert len(data["executions"]) == 1
    assert data["executions"][0]["tool_name"] == "shell.exec"


def test_get_task_timeline_returns_chronological_rows(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/timeline",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert len(data["timeline"]) == 2
    assert data["timeline"][0]["execution_id"] == seeded["owner_exec_one_id"]
    assert data["timeline"][0]["artifact_count"] == 1


def test_get_conversation_executions_filters_turn(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/conversations/conv-1/executions",
        params={"turn_id": "turn-2"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["executions"][0]["execution_id"] == seeded["owner_exec_two_id"]


def test_get_artifact_by_id_returns_metadata_only_and_enforces_ownership(api_client) -> None:
    client, seeded = api_client
    owner_response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert owner_response.status_code == 200, owner_response.text
    owner_data = owner_response.json()
    assert owner_data["artifact_id"] == seeded["owner_artifact_id"]
    assert owner_data["content_text"] is None
    assert owner_data["content_availability"] == "available_inline"
    assert "source_path" not in owner_data
    assert "fallback_path" not in owner_data

    unauthorized_response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}",
        headers={"Authorization": "Bearer other-token"},
    )
    assert unauthorized_response.status_code == 404, unauthorized_response.text
    assert unauthorized_response.json()["detail"] == "Task not found"


def test_get_artifact_by_id_is_task_scoped(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['other_artifact_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Artifact not found"


def test_get_artifact_by_id_cannot_bypass_bounded_read_contract(api_client) -> None:
    client, seeded = api_client

    detail_response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert detail_response.status_code == 200, detail_response.text
    detail_payload = detail_response.json()
    assert detail_payload["content_text"] is None
    assert detail_payload["content_availability"] == "available_inline"

    read_response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "auto", "max_chars": 64},
    )
    assert read_response.status_code == 200, read_response.text
    read_payload = read_response.json()
    assert read_payload["status"] == "ready"
    assert read_payload["source"] == "inline_db"
    assert read_payload["content"] == "owner output"


def test_search_task_artifacts_filters_kind(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts",
        params={"artifact_kind": "stdout"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert len(data["artifacts"]) == 1
    assert data["artifacts"][0]["artifact_kind"] == "stdout"
    assert data["artifacts"][0]["content_text"] is None


def test_get_task_artifact_catalog_returns_joined_rows(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifact-catalog",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert len(data["artifacts"]) == 2

    first = data["artifacts"][0]
    expected_keys = {
        "artifact_id",
        "execution_id",
        "tool_call_id",
        "tool_name",
        "artifact_kind",
        "relative_path",
        "turn_id",
        "turn_sequence",
        "byte_size",
        "mime_type",
        "content_availability",
        "label",
        "task_id",
        "created_at",
    }
    assert expected_keys.issubset(set(first.keys()))
    assert "content_text" not in first


def test_get_task_artifact_catalog_applies_filters_and_ownership(api_client) -> None:
    client, seeded = api_client
    owner_response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifact-catalog",
        params={
            "tool_name": "shell.exec",
            "turn_id": "turn-1",
            "query": "stdout from shell.exec (turn 1)",
        },
        headers={"Authorization": "Bearer owner-token"},
    )
    assert owner_response.status_code == 200, owner_response.text
    owner_data = owner_response.json()
    assert owner_data["total"] == 1
    assert owner_data["artifacts"][0]["tool_name"] == "shell.exec"
    assert owner_data["artifacts"][0]["turn_id"] == "turn-1"

    unauthorized_response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifact-catalog",
        headers={"Authorization": "Bearer other-token"},
    )
    assert unauthorized_response.status_code == 404, unauthorized_response.text
    assert unauthorized_response.json()["detail"] == "Task not found"


def test_read_task_artifact_endpoint_supports_inline_and_file_modes(api_client) -> None:
    client, seeded = api_client

    inline_response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "auto", "max_chars": 5},
    )
    assert inline_response.status_code == 200, inline_response.text
    inline_payload = inline_response.json()
    assert inline_payload["status"] == "ready"
    assert inline_payload["source"] == "inline_db"
    assert inline_payload["mode_used"] == "head"
    assert inline_payload["content"] == "owner"
    assert inline_payload["truncated"] is True

    file_response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_file_artifact_id']}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "match", "query": "file", "max_chars": 12},
    )
    assert file_response.status_code == 200, file_response.text
    file_payload = file_response.json()
    assert file_payload["status"] == "ready"
    assert file_payload["source"] == "workspace_file"
    assert file_payload["mode_used"] == "match"
    assert "file" in file_payload["content"].lower()


def test_read_task_artifact_endpoint_preserves_ownership_and_not_found_contract(api_client) -> None:
    client, seeded = api_client

    unauthorized_response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['owner_artifact_id']}/read",
        headers={"Authorization": "Bearer other-token"},
        json={"mode": "auto"},
    )
    assert unauthorized_response.status_code == 404, unauthorized_response.text
    assert unauthorized_response.json()["detail"] == "Task not found"

    not_found_response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{seeded['other_artifact_id']}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "auto"},
    )
    assert not_found_response.status_code == 200, not_found_response.text
    payload = not_found_response.json()
    assert payload["status"] == "not_found"
    assert payload["content"] is None
    assert payload["artifact"] is None


def test_get_task_artifact_catalog_includes_availability_states(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, seeded = api_client
    catalog_rows = (
        ArtifactCatalogEntry(
            artifact_id="a-ready",
            execution_id=seeded["owner_exec_one_id"],
            tool_call_id="tc-1",
            tool_name="shell.exec",
            task_id=seeded["owner_task_id"],
            artifact_kind="stdout",
            relative_path="artifacts/ready.txt",
            turn_id="turn-1",
            turn_sequence=1,
            byte_size=5,
            mime_type="text/plain",
            content_availability="available_object",
            label="stdout from shell.exec (turn 1)",
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
        ArtifactCatalogEntry(
            artifact_id="a-pending",
            execution_id=seeded["owner_exec_one_id"],
            tool_call_id="tc-2",
            tool_name="shell.exec",
            task_id=seeded["owner_task_id"],
            artifact_kind="tool_file",
            relative_path="artifacts/pending.txt",
            turn_id="turn-1",
            turn_sequence=1,
            byte_size=7,
            mime_type="text/plain",
            content_availability="upload_pending",
            label="tool_file from shell.exec (turn 1)",
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
        ArtifactCatalogEntry(
            artifact_id="a-failed",
            execution_id=seeded["owner_exec_one_id"],
            tool_call_id="tc-3",
            tool_name="shell.exec",
            task_id=seeded["owner_task_id"],
            artifact_kind="tool_file",
            relative_path="artifacts/failed.txt",
            turn_id="turn-1",
            turn_sequence=1,
            byte_size=6,
            mime_type="text/plain",
            content_availability="upload_failed",
            label="tool_file from shell.exec (turn 1)",
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    class _StubMemoryService:
        def search_task_artifacts(self, *, task_id: int, tenant_id: int | None = None, filters):
            del tenant_id
            return ArtifactCatalogPage(
                artifacts=catalog_rows,
                total=len(catalog_rows),
                limit=20,
                offset=0,
            )

    monkeypatch.setattr(artifact_routes, "_artifact_memory_service", lambda db: _StubMemoryService())

    response = client.get(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifact-catalog",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    states = {row["artifact_id"]: row["availability_state"] for row in payload["artifacts"]}
    assert states["a-ready"] == "ready"
    assert states["a-pending"] == "pending"
    assert states["a-failed"] == "failed"


def test_read_task_artifact_supports_object_store_source(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, seeded = api_client
    artifact_row = ArtifactCatalogEntry(
        artifact_id="a-object",
        execution_id=seeded["owner_exec_one_id"],
        tool_call_id="tc-object",
        tool_name="shell.exec",
        task_id=seeded["owner_task_id"],
        artifact_kind="tool_file",
        relative_path="artifacts/object.txt",
        turn_id="turn-1",
        turn_sequence=1,
        byte_size=16,
        mime_type="text/plain",
        content_availability="available_object",
        label="tool_file from shell.exec (turn 1)",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    class _StubMemoryService:
        def read_task_artifact(
            self,
            *,
            task_id: int,
            tenant_id: int | None = None,
            artifact_id: str,
            request,
            user_id: int | None = None,
        ):
            del tenant_id
            return ArtifactReadResult(
                status="ready",
                artifact_id=artifact_id,
                content="object-backed-text",
                content_availability="available_object",
                mode_used="head",
                truncated=False,
                source="object_store",
                artifact=artifact_row,
            )

    monkeypatch.setattr(artifact_routes, "_artifact_memory_service", lambda db: _StubMemoryService())

    response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{artifact_row.artifact_id}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "auto", "max_chars": 64},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["source"] == "object_store"
    assert payload["availability_state"] == "ready"
    assert payload["artifact"]["availability_state"] == "ready"
    assert "object_key" not in payload["artifact"]
    assert "url" not in payload["artifact"]


@pytest.mark.parametrize(
    "failure_reason",
    ["object_unavailable", "object_read_failed", "decode_failed"],
)
def test_read_task_artifact_reports_not_available_state_for_object_failures(
    api_client,
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
) -> None:
    client, seeded = api_client
    artifact_row = ArtifactCatalogEntry(
        artifact_id=f"a-object-{failure_reason}",
        execution_id=seeded["owner_exec_one_id"],
        tool_call_id="tc-object-failure",
        tool_name="shell.exec",
        task_id=seeded["owner_task_id"],
        artifact_kind="tool_file",
        relative_path="artifacts/object-failure.txt",
        turn_id="turn-1",
        turn_sequence=1,
        byte_size=32,
        mime_type="text/plain",
        content_availability="not_available",
        label="tool_file from shell.exec (turn 1)",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    class _StubMemoryService:
        def read_task_artifact(
            self,
            *,
            task_id: int,
            tenant_id: int | None = None,
            artifact_id: str,
            request,
            user_id: int | None = None,
        ):
            del task_id, request, user_id
            del tenant_id
            return ArtifactReadResult(
                status="not_available",
                artifact_id=artifact_id,
                content=None,
                content_availability="not_available",
                mode_used="auto",
                truncated=False,
                source="none",
                artifact=artifact_row,
            )

    monkeypatch.setattr(artifact_routes, "_artifact_memory_service", lambda db: _StubMemoryService())

    response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/artifacts/{artifact_row.artifact_id}/read",
        headers={"Authorization": "Bearer owner-token"},
        json={"mode": "auto", "query": failure_reason, "max_chars": 64},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "not_available"
    assert payload["availability_state"] == "not_available"
    assert payload["content_availability"] == "not_available"
    assert payload["artifact"]["availability_state"] == "not_available"


def test_raw_output_batch_resolves_multiple_tool_calls(api_client) -> None:
    client, seeded = api_client
    response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/raw-output/batch",
        headers={"Authorization": "Bearer owner-token"},
        json={
            "tool_call_ids": ["tc-shared", "tc-owner-2", "missing-id"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data["results"].keys()) == {"tc-shared", "tc-owner-2", "missing-id"}
    assert data["results"]["tc-shared"]["status"] == "ready"
    assert "owner output" in data["results"]["tc-shared"]["output_text"]
    assert data["results"]["tc-owner-2"]["status"] == "not_available"
    assert data["results"]["tc-owner-2"]["reason"] == "missing_output_artifacts"
    assert data["results"]["missing-id"]["status"] == "not_available"
    assert data["results"]["missing-id"]["reason"] == "execution_not_found"
    assert "missing-id" in data["missing"]


def test_raw_output_batch_enforces_task_ownership(api_client) -> None:
    client, seeded = api_client
    response = client.post(
        f"/api/artifact-provenance/tasks/{seeded['owner_task_id']}/raw-output/batch",
        headers={"Authorization": "Bearer other-token"},
        json={"tool_call_ids": ["tc-shared"]},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_main_app_registers_artifact_provenance_router() -> None:
    from pathlib import Path

    source = Path("backend/main.py").read_text(encoding="utf-8")
    assert "artifact_provenance" in source
    assert "app.include_router(artifact_provenance.router)" in source
