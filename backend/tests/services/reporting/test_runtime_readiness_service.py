"""Tests for durable reporting runtime readiness computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import AgentLog, ChatMessage, ChatTurnEvent
from backend.models.core import Task, TaskHistory, User
from backend.models.provenance import ToolExecution
from backend.models.runner_control import RunnerControlMessage, RuntimeJob
from backend.models.streaming import StreamEvent, SystemLog
from backend.models.tenant import Tenant
from backend.services.reporting.contracts import (
    REASON_NO_USEFUL_RUNTIME_EXECUTION,
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    REASON_TASK_NOT_STOPPED,
)
from backend.services.reporting.runtime_readiness_service import RuntimeReadinessService


def _build_session() -> Session:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenant(db: Session) -> Tenant:
    tenant = Tenant(slug=f"tenant-{uuid_lib.uuid4().hex[:8]}", name="Tenant")
    db.add(tenant)
    db.flush()
    return tenant


def _seed_user(db: Session) -> User:
    user = User(username=f"user-{uuid_lib.uuid4().hex[:8]}", password="hashed")
    db.add(user)
    db.flush()
    return user


def _seed_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    status: str,
    runtime_placement_mode: str = "local",
) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        user_id=user_id,
        tenant_id=tenant_id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=status,
        runtime_placement_mode=runtime_placement_mode,
    )
    db.add(task)
    db.flush()
    return task


def _seed_context(
    *,
    status: str = TaskStatus.STOPPED.value,
    runtime_placement_mode: str = "local",
) -> tuple[Session, Tenant, User, Task]:
    db = _build_session()
    tenant = _seed_tenant(db)
    user = _seed_user(db)
    task = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        status=status,
        runtime_placement_mode=runtime_placement_mode,
    )
    return db, tenant, user, task


def _add_history(
    db: Session,
    *,
    task: Task,
    new_status: str,
    metadata: dict[str, object] | None = None,
    timestamp: datetime | None = None,
) -> TaskHistory:
    row = TaskHistory(
        task_id=task.id,
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        old_status=TaskStatus.STARTING.value,
        new_status=new_status,
        change_source="system",
        change_metadata=metadata,
        timestamp=timestamp,
    )
    db.add(row)
    db.flush()
    return row


def test_runner_retired_history_metadata_makes_stopped_runner_task_runtime_retired() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value, timestamp=started_at)
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.STOPPED.value,
        metadata={
            "runtime_event_type": "runtime.retired",
            "runtime_event_lifecycle_outcome": "retired",
        },
        timestamp=started_at + timedelta(minutes=1),
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason is None


def test_runner_stopped_without_retirement_outcome_fails_closed() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value)
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.STOPPED.value,
        metadata={"runtime_event_type": "runtime.stopped"},
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is False
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason == REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED


def test_runner_stopped_outcome_confirms_runtime_ready_for_reporting() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value, timestamp=started_at)
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.STOPPED.value,
        metadata={
            "runtime_event_type": "runtime.stopped",
            "runtime_event_lifecycle_outcome": "stopped",
        },
        timestamp=started_at + timedelta(minutes=1),
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason is None


def test_runner_retirement_signal_before_later_run_does_not_confirm_current_stop() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.RUNNING.value,
        timestamp=started_at,
    )
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.STOPPED.value,
        metadata={
            "runtime_event_type": "runtime.retired",
            "runtime_event_lifecycle_outcome": "retired",
        },
        timestamp=started_at + timedelta(minutes=1),
    )
    db.add_all(
        [
            RuntimeJob(
                tenant_id=tenant.id,
                task_id=task.id,
                job_type="task.retire",
                status="succeeded",
                idempotency_key=f"old-retire-{uuid_lib.uuid4().hex}",
                result_json={"lifecycle_outcome": "retired"},
                created_at=started_at + timedelta(minutes=1),
                updated_at=started_at + timedelta(minutes=1),
            ),
            RunnerControlMessage(
                tenant_id=tenant.id,
                runner_id=uuid_lib.uuid4(),
                task_id=task.id,
                message_id=f"old-retire-message-{uuid_lib.uuid4().hex}",
                direction="inbound",
                type="runtime.retired",
                status="accepted",
                payload_json={"runtime_event_lifecycle_outcome": "retired"},
                created_at=started_at + timedelta(minutes=1),
            ),
        ]
    )
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.QUEUED.value,
        timestamp=started_at + timedelta(minutes=2),
    )
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.RUNNING.value,
        timestamp=started_at + timedelta(minutes=3),
    )
    _add_history(
        db,
        task=task,
        new_status=TaskStatus.STOPPED.value,
        metadata={"runtime_event_type": "runtime.stopped"},
        timestamp=started_at + timedelta(minutes=4),
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is False
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason == REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED


def test_runner_runtime_job_retirement_metadata_is_durable_retirement_signal() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value, timestamp=started_at)
    db.add(
        RuntimeJob(
            tenant_id=tenant.id,
            task_id=task.id,
            job_type="task.retire",
            status="succeeded",
            idempotency_key=f"retire-{uuid_lib.uuid4().hex}",
            result_json={
                "source": "runner_event",
                "message_type": "runtime.retired",
                "status": "succeeded",
                "result": {"task_id": task.id},
                "lifecycle_outcome": "retired",
            },
            created_at=started_at + timedelta(minutes=1),
            updated_at=started_at + timedelta(minutes=1),
        )
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason is None


def test_runner_runtime_job_requires_successful_retirement_completion() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value)
    db.add_all(
        [
            RuntimeJob(
                tenant_id=tenant.id,
                task_id=task.id,
                job_type="task.retire",
                status="failed",
                idempotency_key=f"retire-failed-{uuid_lib.uuid4().hex}",
                result_json={"lifecycle_outcome": "retired"},
            ),
            RuntimeJob(
                tenant_id=tenant.id,
                task_id=task.id,
                job_type="terminal.open",
                status="succeeded",
                idempotency_key=f"terminal-{uuid_lib.uuid4().hex}",
                result_json={"status": "succeeded"},
            ),
            RunnerControlMessage(
                tenant_id=tenant.id,
                runner_id=uuid_lib.uuid4(),
                task_id=task.id,
                message_id=f"retire-outbound-{uuid_lib.uuid4().hex}",
                direction="outbound",
                type="task.retire",
                status="acked",
                payload_json={"operation": "task.retire"},
            ),
        ]
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is False
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason == REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED


def test_local_stopped_task_is_runtime_retired_without_runner_signal() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="local")
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is False
    assert readiness.not_preparable_reason == REASON_NO_USEFUL_RUNTIME_EXECUTION


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.CREATED.value,
        TaskStatus.QUEUED.value,
        TaskStatus.FAILED.value,
    ],
)
def test_created_queued_or_failed_task_without_useful_rows_is_not_preparable(status: str) -> None:
    db, tenant, _user, task = _seed_context(status=status, runtime_placement_mode="runner")
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is False
    assert readiness.useful_runtime_execution is False
    assert readiness.not_preparable_reason == REASON_TASK_NOT_STOPPED


def test_tool_execution_or_running_history_makes_useful_execution_true() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="local")
    db.add(
        ToolExecution(
            tenant_id=tenant.id,
            task_id=task.id,
            tool_name="nmap",
            tool_arguments={"target": "127.0.0.1"},
            agent_path="langgraph",
            status="completed",
            started_at=datetime.now(UTC),
        )
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is True
    assert readiness.not_preparable_reason is None


def test_pre_running_events_do_not_make_runtime_execution_useful() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="local")
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    message = ChatMessage(
        tenant_id=tenant.id,
        task_id=task.id,
        conversation_id=f"conv-{uuid_lib.uuid4().hex}",
        message_type="assistant",
        message="Provisioning started",
        created_at=event_at,
    )
    db.add(message)
    db.flush()
    db.add_all(
        [
            ChatTurnEvent(
                tenant_id=tenant.id,
                task_id=task.id,
                conversation_id=message.conversation_id,
                chat_message_id=message.id,
                turn_number=1,
                phase_sequence=1,
                kind="observation",
                content="Provisioning",
                event_metadata={"task_status": "starting"},
                created_at=event_at,
            ),
            AgentLog(
                tenant_id=tenant.id,
                task_id=task.id,
                type="reasoning",
                content="Provisioning",
                log_metadata={"task_status": "starting"},
                turn_id=f"turn-{uuid_lib.uuid4().hex}",
                turn_number=1,
                timestamp=event_at,
            ),
            SystemLog(
                tenant_id=tenant.id,
                task_id=task.id,
                sequence=1,
                type="runtime.output",
                content="Provisioning",
                log_metadata={"task_status": "starting"},
                timestamp=event_at,
            ),
            StreamEvent(
                tenant_id=tenant.id,
                task_id=task.id,
                sequence=1,
                event_type="runtime.output",
                payload={"obj": {"type": "runtime.output"}, "task_status": "starting"},
                created_at=event_at,
            ),
        ]
    )
    db.commit()

    readiness = RuntimeReadinessService(db).compute_for_task(
        tenant_id=tenant.id,
        task_id=task.id,
    )

    assert readiness.runtime_retired is True
    assert readiness.useful_runtime_execution is False
    assert readiness.not_preparable_reason == REASON_NO_USEFUL_RUNTIME_EXECUTION


def test_runtime_readiness_does_not_mutate_task_or_history() -> None:
    db, tenant, _user, task = _seed_context(runtime_placement_mode="runner")
    _add_history(db, task=task, new_status=TaskStatus.RUNNING.value)
    db.commit()
    original_status = task.status
    history_count = db.query(TaskHistory).filter(TaskHistory.task_id == task.id).count()

    RuntimeReadinessService(db).compute_for_task(tenant_id=tenant.id, task_id=task.id)

    db.refresh(task)
    assert task.status == original_status
    assert db.query(TaskHistory).filter(TaskHistory.task_id == task.id).count() == history_count
