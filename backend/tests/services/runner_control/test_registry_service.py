"""Tests for Runner Control runner registry management service behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.chat import ChatMessage
from backend.models.core import Engagement, Task, User
from backend.models.provenance import ToolExecution
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.runner_control.credentials import RunnerCredentialAuthError, RunnerCredentialService
from backend.services.runner_control.registry_service import RunnerRegistryError, RunnerRegistryService


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            ChatMessage.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
            RunnerInstallToken.__table__,
            RunnerCredential.__table__,
            RuntimeJob.__table__,
            ToolExecution.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_context(db: Session) -> tuple[int, int, ExecutionSite, ExecutionSite, Runner]:
    tenant_one = Tenant(slug="tenant-one", name="Tenant One")
    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    user = User(username="owner", password="hashed")
    db.add_all([tenant_one, tenant_two, user])
    db.flush()

    site_one = ExecutionSite(
        tenant_id=tenant_one.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    site_two = ExecutionSite(
        tenant_id=tenant_two.id,
        name="Secondary Site",
        slug="secondary-site",
        status="active",
    )
    db.add_all([site_one, site_two])
    db.flush()

    runner = Runner(
        tenant_id=tenant_one.id,
        execution_site_id=site_one.id,
        name="runner-alpha",
        status="registered",
    )
    db.add(runner)
    db.commit()
    return tenant_one.id, user.id, site_one, site_two, runner


def _seed_ready_replacement(
    db: Session,
    *,
    tenant_id: int,
    now: datetime,
) -> tuple[ExecutionSite, Runner]:
    site = ExecutionSite(
        tenant_id=tenant_id,
        name="Replacement Site",
        slug="replacement-site",
        status="active",
    )
    db.add(site)
    db.flush()
    runner = Runner(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        name="runner-replacement",
        status="active",
        last_seen_at=now,
        capacity_json={"active_tasks": 99, "available_tasks": 0},
    )
    db.add(runner)
    db.flush()
    RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-replacement",
            connection_id="conn-replacement",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    db.commit()
    return site, runner


def test_runner_site_connectivity_ignores_active_runner_row_without_lease() -> None:
    db = _build_session()
    tenant_id, _user_id, site, _site_two, runner = _seed_context(db)
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    runner.status = "active"
    db.commit()

    service = RunnerRegistryService(db)
    summaries = service.list_runner_site_connectivity(tenant_id=tenant_id, now=observed_at)

    assert summaries[site.id].connectivity_status == "offline"
    assert summaries[site.id].connected_runner_count == 0
    assert service.has_connected_runner_site(now=observed_at) is False


def test_runner_site_connectivity_ignores_expired_active_connection() -> None:
    db = _build_session()
    tenant_id, _user_id, site, _site_two, runner = _seed_context(db)
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=observed_at - timedelta(seconds=1),
            last_seen_at=observed_at - timedelta(minutes=5),
        )
    )
    db.commit()

    service = RunnerRegistryService(db)
    summaries = service.list_runner_site_connectivity(tenant_id=tenant_id, now=observed_at)

    assert summaries[site.id].connectivity_status == "offline"
    assert summaries[site.id].connected_runner_count == 0
    assert service.has_connected_runner_site(now=observed_at) is False


def test_runner_site_connectivity_counts_fresh_active_connection() -> None:
    db = _build_session()
    tenant_id, _user_id, site, _site_two, runner = _seed_context(db)
    observed_at = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=observed_at + timedelta(minutes=10),
            last_seen_at=observed_at - timedelta(seconds=30),
        )
    )
    db.commit()

    service = RunnerRegistryService(db)
    summaries = service.list_runner_site_connectivity(tenant_id=tenant_id, now=observed_at)

    assert summaries[site.id].connectivity_status == "connected"
    assert summaries[site.id].connected_runner_count == 1
    assert service.has_connected_runner_site(now=observed_at) is True


def test_issue_install_token_validates_tenant_execution_site_and_hashes_secret() -> None:
    db = _build_session()
    tenant_id, user_id, site_one, site_two, _runner = _seed_context(db)
    audit_events: list[dict[str, object]] = []
    service = RunnerRegistryService(db, audit_emitter=audit_events.append)

    issued = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site_one.id,
        created_by_user_id=user_id,
    )
    db.commit()

    stored = db.get(RunnerInstallToken, issued.install_token_id)
    assert stored is not None
    assert stored.token_hash != issued.plaintext_token
    assert len(audit_events) == 1
    assert audit_events[0]["event_type"] == "runner.install_token_created"
    metadata = audit_events[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["install_token_id"] == str(issued.install_token_id)
    assert issued.plaintext_token not in str(metadata)

    with pytest.raises(RunnerRegistryError) as wrong_tenant_error:
        service.issue_install_token(
            tenant_id=tenant_id,
            execution_site_id=site_two.id,
            created_by_user_id=user_id,
        )
    assert wrong_tenant_error.value.error_code == "EXECUTION_SITE_NOT_FOUND"


def test_revoke_runner_credentials_invalidates_authentication() -> None:
    db = _build_session()
    tenant_id, user_id, _site_one, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    credential_service = RunnerCredentialService(db, now_provider=lambda: now)
    issued = credential_service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    db.commit()

    matched = credential_service.authenticate_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner.id,
        plaintext_secret=issued.plaintext_secret,
    )
    assert matched.id == issued.credential_id

    audit_events: list[dict[str, object]] = []
    registry = RunnerRegistryService(db, credential_service=credential_service, audit_emitter=audit_events.append)
    revoked_count = registry.revoke_runner_credentials(
        tenant_id=tenant_id,
        runner_id=runner.id,
        actor_user_id=user_id,
    )
    db.commit()

    assert revoked_count == 1
    refreshed_runner = db.get(Runner, runner.id)
    assert refreshed_runner is not None
    assert refreshed_runner.status == "revoked"
    assert [event["event_type"] for event in audit_events] == ["runner.credential_revoked"]
    assert audit_events[0]["runner_id"] == str(runner.id)
    assert audit_events[0]["actor_user_id"] == user_id

    with pytest.raises(RunnerCredentialAuthError) as revoked_error:
        credential_service.authenticate_runner_credential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            plaintext_secret=issued.plaintext_secret,
        )
    assert revoked_error.value.error_code == "RUNNER_AUTH_REVOKED"


def test_delete_runner_site_removes_registry_and_operational_rows() -> None:
    db = _build_session()
    tenant_id, user_id, site, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    _seed_ready_replacement(db, tenant_id=tenant_id, now=now)
    credential_service = RunnerCredentialService(db)
    issued_credential = credential_service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    install_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=now + timedelta(minutes=10),
            last_seen_at=now,
        )
    )
    runtime_job = RuntimeJob(
        tenant_id=tenant_id,
        runner_id=runner.id,
        execution_site_id=site.id,
        job_type="runtime_stop",
        status="succeeded",
        idempotency_key="delete-site-terminal-job",
    )
    db.add(runtime_job)
    db.flush()
    control_message = RunnerControlMessage(
        tenant_id=tenant_id,
        runner_id=runner.id,
        runtime_job_id=runtime_job.id,
        message_id="delete-site-message",
        direction="outbound",
        type="runtime_stop",
        status="acked",
    )
    db.add(control_message)
    db.commit()
    runtime_job_id = runtime_job.id
    control_message_id = control_message.id

    audit_events: list[dict[str, object]] = []
    service = RunnerRegistryService(
        db,
        credential_service=credential_service,
        audit_emitter=audit_events.append,
    )
    service.delete_runner_site(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        actor_user_id=user_id,
        now=now,
    )
    db.commit()

    assert db.get(ExecutionSite, site.id) is None
    assert db.get(Runner, runner.id) is None
    assert db.get(RunnerInstallToken, install_token.install_token_id) is None
    assert db.get(RunnerCredential, issued_credential.credential_id) is None
    assert db.get(RuntimeJob, runtime_job_id) is None
    assert db.get(RunnerControlMessage, control_message_id) is None
    assert db.execute(
        select(RunnerConnection).where(RunnerConnection.runner_id == runner.id)
    ).scalar_one_or_none() is None
    assert audit_events[0]["event_type"] == "runner_site.deleted"


@pytest.mark.parametrize(
    ("active_source", "capacity"),
    [
        ("runtime_job", {"active_tasks": 0, "active_runtime_jobs": []}),
        ("heartbeat_tasks", {"active_tasks": 1, "active_runtime_jobs": []}),
        (
            "heartbeat_jobs",
            {
                "active_tasks": 0,
                "active_runtime_jobs": [{"runtime_job_id": "reported-job"}],
            },
        ),
    ],
)
def test_delete_runner_site_rejects_connected_active_executions_without_mutation(
    active_source: str,
    capacity: dict[str, object],
) -> None:
    db = _build_session()
    tenant_id, user_id, site, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    _seed_ready_replacement(db, tenant_id=tenant_id, now=now)
    runner.status = "active"
    runner.last_seen_at = now
    runner.capacity_json = capacity
    credential = RunnerCredentialService(db).issue_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner.id,
    )
    token = RunnerCredentialService(db).issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    if active_source == "runtime_job":
        db.add(
            RuntimeJob(
                tenant_id=tenant_id,
                runner_id=runner.id,
                execution_site_id=site.id,
                job_type="runtime_start",
                status="running",
                idempotency_key="active-delete-guard",
            )
        )
    db.commit()

    with pytest.raises(RunnerRegistryError) as exc_info:
        RunnerRegistryService(db).delete_runner_site(
            tenant_id=tenant_id,
            execution_site_id=site.id,
            actor_user_id=user_id,
            now=now,
        )

    assert exc_info.value.error_code == "RUNNER_SITE_ACTIVE_EXECUTIONS"
    assert exc_info.value.details == {"execution_count": 1}
    assert db.get(ExecutionSite, site.id) is not None
    assert db.get(Runner, runner.id) is not None
    assert db.get(RunnerCredential, credential.credential_id) is not None
    assert db.get(RunnerInstallToken, token.install_token_id) is not None
    assert db.execute(
        select(RunnerConnection).where(RunnerConnection.runner_id == runner.id)
    ).scalar_one_or_none() is not None


def test_delete_runner_site_rejects_when_no_connected_replacement_exists() -> None:
    db = _build_session()
    tenant_id, user_id, site, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    runner.status = "active"
    runner.last_seen_at = now
    credential_id = RunnerCredentialService(db).issue_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner.id,
    ).credential_id
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-one",
            connection_id="conn-one",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    db.commit()

    with pytest.raises(RunnerRegistryError) as exc_info:
        RunnerRegistryService(db).delete_runner_site(
            tenant_id=tenant_id,
            execution_site_id=site.id,
            actor_user_id=user_id,
            now=now,
        )

    assert exc_info.value.error_code == "RUNNER_SITE_LAST_CONNECTED"
    assert db.get(ExecutionSite, site.id) is not None
    assert db.get(RunnerCredential, credential_id) is not None


def test_delete_runner_site_allows_offline_target_with_stale_active_heartbeat() -> None:
    db = _build_session()
    tenant_id, user_id, site, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    _seed_ready_replacement(db, tenant_id=tenant_id, now=now)
    runner.capacity_json = {
        "active_tasks": 4,
        "active_runtime_jobs": [{"runtime_job_id": "stale-job"}],
    }
    db.commit()

    RunnerRegistryService(db).delete_runner_site(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        actor_user_id=user_id,
        now=now,
    )
    db.commit()

    assert db.get(ExecutionSite, site.id) is None


def test_delete_runner_site_missing_or_cross_tenant_is_not_found() -> None:
    db = _build_session()
    tenant_id, user_id, _site, foreign_site, _runner = _seed_context(db)

    with pytest.raises(RunnerRegistryError) as exc_info:
        RunnerRegistryService(db).delete_runner_site(
            tenant_id=tenant_id,
            execution_site_id=foreign_site.id,
            actor_user_id=user_id,
        )

    assert exc_info.value.error_code == "RUNNER_SITE_NOT_FOUND"


def test_delete_runner_site_preserves_task_and_durable_provenance() -> None:
    db = _build_session()
    tenant_id, user_id, site, _site_two, runner = _seed_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    _seed_ready_replacement(db, tenant_id=tenant_id, now=now)
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        name="Preserved Task",
        status="running",
        runner_id=str(runner.id),
        execution_site_id=str(site.id),
    )
    db.add(task)
    db.flush()
    runtime_job = RuntimeJob(
        tenant_id=tenant_id,
        task_id=task.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        job_type="tool_execute",
        status="succeeded",
        idempotency_key="preserve-durable-provenance",
    )
    db.add(runtime_job)
    db.flush()
    execution = ToolExecution(
        tenant_id=tenant_id,
        task_id=task.id,
        runtime_job_id=runtime_job.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        tool_name="shell.exec",
        tool_arguments={"command": "true"},
        agent_path="langgraph",
        status="completed",
        started_at=now,
    )
    db.add(execution)
    db.commit()
    execution_id = execution.id
    task_id = task.id
    runner_id = runner.id
    site_id = site.id

    RunnerRegistryService(db).delete_runner_site(
        tenant_id=tenant_id,
        execution_site_id=site_id,
        actor_user_id=user_id,
        now=now,
    )
    db.commit()

    preserved_task = db.get(Task, task_id)
    preserved_execution = db.get(ToolExecution, execution_id)
    assert preserved_task is not None
    assert preserved_task.status == "running"
    assert preserved_task.runner_id == str(runner_id)
    assert preserved_task.execution_site_id == str(site_id)
    assert preserved_execution is not None
    assert preserved_execution.runtime_job_id is None
    assert preserved_execution.runner_id is None
    assert preserved_execution.execution_site_id is None
