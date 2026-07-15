"""Tests for task-local retention executor registration and ordering."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.schemas.retention import RetentionRunResponse
from backend.services.retention.contracts import (
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_TENANT,
    RetentionBatchCounts,
    RetentionExecutorResult,
    RetentionRunRequest,
)
from backend.services.retention import orchestrator as retention_orchestrator
from backend.services.retention.orchestrator import (
    REMAINING_RETENTION_CLASSES,
    REMAINING_RETENTION_EXECUTOR_ORDER,
    TASK_LOCAL_RETENTION_CLASSES,
    TASK_LOCAL_RETENTION_EXECUTOR_ORDER,
    RetentionOrchestrator,
    build_retention_executors,
    build_remaining_retention_executors,
    build_task_local_retention_executors,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    retention_batch_size_per_tenant: int = 5


class _FakeSession:
    def __init__(self) -> None:
        self.pending: list[str] = []
        self.commit_count = 0
        self.rollback_count = 0

    def add_marker(self, marker: str) -> None:
        self.pending.append(marker)

    def commit(self) -> None:
        self.commit_count += 1
        self.pending.clear()

    def rollback(self) -> None:
        self.rollback_count += 1
        self.pending.clear()


class _FakeExecutor:
    def __init__(
        self,
        *,
        name: str,
        retention_class: str,
        protected_count: int,
        calls: list[tuple[str, int]],
        db: _FakeSession,
    ) -> None:
        self.name = name
        self.retention_class = retention_class
        self._protected_count = protected_count
        self._calls = calls
        self._db = db

    def run(
        self,
        *,
        policy: object,
        tenant_id: int,
        mode: str,
        limit: int,
    ) -> RetentionExecutorResult:
        self._db.add_marker(self.name)
        self._calls.append((self.name, limit))
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=self.retention_class,
            mode=mode,
            tenant_id=tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=self._protected_count,
                protected_count=self._protected_count,
                batch_limit=limit,
            ),
            reason_counts={"task_local_retention_protected": self._protected_count},
        )


def test_task_local_executor_factory_registers_dependency_safe_order() -> None:
    executors = build_task_local_retention_executors(
        _FakeSession(),  # type: ignore[arg-type]
    )

    assert [executor.name for executor in executors] == [
        "checkpoint.retention",
        "chat.retention",
        "task.retention",
    ]
    assert [executor.retention_class for executor in executors] == [
        RETENTION_CLASS_RUNTIME_RESUME_STATE,
        RETENTION_CLASS_TASK_TRANSCRIPT,
        RETENTION_CLASS_TASK_RECORD,
    ]
    assert tuple(
        entry.executor_name for entry in TASK_LOCAL_RETENTION_EXECUTOR_ORDER
    ) == ("checkpoint.retention", "chat.retention", "task.retention")
    assert TASK_LOCAL_RETENTION_CLASSES == (
        RETENTION_CLASS_RUNTIME_RESUME_STATE,
        RETENTION_CLASS_TASK_TRANSCRIPT,
        RETENTION_CLASS_TASK_RECORD,
    )


def test_remaining_executor_factory_registers_default_order() -> None:
    executors = build_remaining_retention_executors(
        _FakeSession(),  # type: ignore[arg-type]
    )

    assert [executor.name for executor in executors] == [
        "runner_control.retention",
        "memory.retention",
        "usage.retention",
    ]
    assert tuple(
        entry.executor_name for entry in REMAINING_RETENTION_EXECUTOR_ORDER
    ) == ("runner_control.retention", "memory.retention", "usage.retention")
    assert REMAINING_RETENTION_CLASSES == (
        "operational_ephemeral",
        "semantic_memory",
        "usage_accounting",
    )


def test_default_factory_includes_implemented_executors_in_scheduled_order() -> None:
    executors = build_retention_executors(_FakeSession())  # type: ignore[arg-type]

    assert [executor.name for executor in executors] == [
        "runner_control.retention",
        "checkpoint.retention",
        "chat.retention",
        "task.retention",
        "artifact.retention",
        "artifact_provenance.retention",
        "knowledge.retention",
        "knowledge.evidence_retention",
        "reporting.retention",
        "memory.retention",
        "usage.retention",
    ]


def test_default_orchestrator_runs_task_local_cleanup_order_and_rolls_up_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSession()
    calls: list[tuple[str, int]] = []

    def fake_default_executors(_db):
        return (
            _FakeExecutor(
                name="task.retention",
                retention_class=RETENTION_CLASS_TASK_RECORD,
                protected_count=3,
                calls=calls,
                db=db,
            ),
            _FakeExecutor(
                name="checkpoint.retention",
                retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
                protected_count=1,
                calls=calls,
                db=db,
            ),
            _FakeExecutor(
                name="chat.retention",
                retention_class=RETENTION_CLASS_TASK_TRANSCRIPT,
                protected_count=2,
                calls=calls,
                db=db,
            ),
        )

    monkeypatch.setattr(
        retention_orchestrator,
        "build_retention_executors",
        fake_default_executors,
    )

    result = RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        policy_resolver=(
            lambda _db, _tenant_id: _Policy()  # type: ignore[arg-type, return-value]
        ),
    ).run(
        RetentionRunRequest(
            mode=RETENTION_RUN_MODE_DRY_RUN,
            scope=RETENTION_SCOPE_TENANT,
            tenant_id=1,
            retention_classes=(
                RETENTION_CLASS_TASK_RECORD,
                RETENTION_CLASS_TASK_TRANSCRIPT,
                RETENTION_CLASS_RUNTIME_RESUME_STATE,
            ),
            limit_per_tenant=2,
        )
    )

    assert [name for name, _limit in calls] == [
        "checkpoint.retention",
        "chat.retention",
        "task.retention",
    ]
    assert [limit for _name, limit in calls] == [2, 2, 2]
    assert db.pending == []
    assert db.commit_count == 0
    assert db.rollback_count == 3
    response = RetentionRunResponse.from_run_result(result)
    assert response.counts.protected_count == 6
