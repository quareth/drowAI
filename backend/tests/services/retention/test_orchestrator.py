"""Tests for tenant-scoped retention orchestration behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend.models.data_management import TenantDataManagementSettings
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_REPORTING,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_ALL_TENANTS,
    RETENTION_SCOPE_TENANT,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunRequest,
)
from backend.services.metrics import retention as retention_metrics
from backend.services.retention.orchestrator import (
    RETENTION_EXECUTOR_FAILURE_CODE,
    RETENTION_TRANSACTION_BOUNDARY_TENANT,
    RetentionOrchestrator,
)
from backend.services.retention.scheduling import (
    DEFAULT_EXECUTOR_ORDER,
    RetentionExecutorOrderEntry,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    tenant_id: int
    retention_batch_size_per_tenant: int


class _FakeSession:
    def __init__(self) -> None:
        self.pending: list[str] = []
        self.committed: list[str] = []
        self.queried_models: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0

    def add_marker(self, marker: str) -> None:
        self.pending.append(marker)

    def commit(self) -> None:
        self.commit_count += 1
        self.committed.extend(self.pending)
        self.pending.clear()

    def rollback(self) -> None:
        self.rollback_count += 1
        self.pending.clear()

    def query(self, model: object) -> "_NoSettingsQuery":
        self.queried_models.append(model)
        return _NoSettingsQuery()


class _NoSettingsQuery:
    def filter(self, *_conditions: object) -> "_NoSettingsQuery":
        return self

    def one_or_none(self) -> None:
        return None


class _Executor:
    def __init__(
        self,
        *,
        name: str,
        retention_class: str,
        db: _FakeSession,
        calls: list[tuple[str, int, str, int]] | None = None,
        marker: str | None = None,
        result_class: str | None = None,
        decisions: tuple[Any, ...] = (),
        fail: bool = False,
    ) -> None:
        self.name = name
        self.retention_class = retention_class
        self._db = db
        self._calls = calls
        self._marker = marker or name
        self._result_class = result_class or retention_class
        self._decisions = decisions
        self._fail = fail

    def run(
        self,
        *,
        policy: object,
        tenant_id: int,
        mode: str,
        limit: int,
    ) -> object:
        self._db.add_marker(f"{tenant_id}:{self._marker}")
        if self._calls is not None:
            self._calls.append((self.name, tenant_id, mode, limit))
        if self._fail:
            raise RuntimeError("secret token payload must not leak")
        return _LegacyResult(
            executor_name=self.name,
            retention_class=self._result_class,
            mode=mode,
            tenant_id=tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=1,
                candidate_count=1,
                applied_count=1,
                batch_count=1,
                batch_limit=limit,
            ),
            reason_counts={"retention_candidate": 1},
            decisions=self._decisions,
            succeeded=True,
            error_code=None,
        )


@dataclass(frozen=True, slots=True)
class _LegacyResult:
    executor_name: str
    retention_class: str
    mode: str
    tenant_id: int
    counts: RetentionBatchCounts
    reason_counts: dict[str, int]
    decisions: tuple[Any, ...]
    succeeded: bool
    error_code: str | None


def test_dry_run_rolls_back_executor_mutation_attempts() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
            ),
        ),
    )

    result = orchestrator.run(request)

    assert result.succeeded is True
    assert db.committed == []
    assert db.pending == []
    assert db.commit_count == 0
    assert db.rollback_count == 1


def test_apply_is_tenant_scoped_and_bounded_by_policy_batch_size() -> None:
    db = _FakeSession()
    calls: list[tuple[str, int, str, int]] = []
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=42,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
        limit_per_tenant=20,
    )
    orchestrator = _orchestrator(
        db,
        policies={42: _Policy(tenant_id=42, retention_batch_size_per_tenant=7)},
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
                calls=calls,
            ),
        ),
    )

    result = orchestrator.run(request)

    assert result.succeeded is True
    assert calls == [("runner_control.retention", 42, RETENTION_RUN_MODE_APPLY, 7)]
    assert db.committed == ["42:runner_control.retention"]
    assert db.commit_count == 1
    assert db.rollback_count == 0


def test_all_tenant_run_rejects_unbounded_batch_before_resolving_policies() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_ALL_TENANTS,
        tenant_id=None,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    resolved_policy_ids: list[int] = []
    orchestrator = RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        tenant_loader=lambda _db: (1, 2, 3),
        max_tenants_per_run=2,
        policy_resolver=lambda _db, tenant_id: resolved_policy_ids.append(
            tenant_id
        ),  # type: ignore[arg-type, return-value]
    )

    with pytest.raises(ValueError, match="tenant_ids exceeds max_tenants_per_run"):
        orchestrator.run(request)

    assert resolved_policy_ids == []


def test_registered_executors_run_in_scheduled_order() -> None:
    db = _FakeSession()
    calls: list[tuple[str, int, str, int]] = []
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(
            RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            RETENTION_CLASS_TASK_RECORD,
        ),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="task.retention",
                retention_class=RETENTION_CLASS_TASK_RECORD,
                db=db,
                calls=calls,
            ),
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
                calls=calls,
            ),
        ),
    )

    orchestrator.run(request)

    assert [item[0] for item in calls] == [
        "runner_control.retention",
        "task.retention",
    ]


def test_executor_failure_returns_safe_result_without_exception_payload() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
                fail=True,
            ),
        ),
    )

    result = orchestrator.run(request)
    failed = result.results[0]

    assert result.succeeded is False
    assert failed.succeeded is False
    assert failed.error_code == RETENTION_EXECUTOR_FAILURE_CODE
    assert failed.reason_counts == {RETENTION_EXECUTOR_FAILURE_CODE: 1}
    assert "secret token payload" not in str(failed.to_safe_dict())
    assert db.commit_count == 0
    assert db.rollback_count == 1


def test_orchestrator_emits_safe_audit_events_without_exception_payload() -> None:
    db = _FakeSession()
    audit_events: list[dict[str, Any]] = []
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
                fail=True,
            ),
        ),
        audit_emitter=audit_events.append,
    )

    orchestrator.run(request)

    assert [event["event_type"] for event in audit_events] == [
        "retention.executor_completed",
        "retention.run_completed",
    ]
    executor_event = audit_events[0]
    assert executor_event["tenant_id"] == 1
    assert executor_event["executor_name"] == "runner_control.retention"
    assert executor_event["error_code"] == RETENTION_EXECUTOR_FAILURE_CODE
    assert executor_event["counts"]["failed_count"] == 1
    assert "secret token payload" not in str(audit_events)
    assert "payload" not in str(audit_events)


def test_retention_metrics_use_safe_names_without_tenant_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, str, int | float]] = []
    result = RetentionExecutorResult(
        executor_name="runner_control.retention",
        retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        mode=RETENTION_RUN_MODE_APPLY,
        tenant_id=42,
        counts=RetentionBatchCounts(
            candidate_count=3,
            applied_count=2,
            protected_count=1,
            failed_count=1,
            batch_limit=10,
        ),
        reason_counts={"operational_log_retention_expired": 3},
        succeeded=False,
        error_code="storage_timeout",
    )

    monkeypatch.setattr(
        retention_metrics,
        "safe_inc",
        lambda name, value=1: emitted.append(("inc", name, value)),
    )
    monkeypatch.setattr(
        retention_metrics,
        "safe_gauge",
        lambda name, value: emitted.append(("gauge", name, value)),
    )

    retention_metrics.emit_retention_executor_metrics(
        result,
        duration_seconds=0.25,
    )

    metric_names = [item[1] for item in emitted]
    assert "retention.executor.runner_control_retention.candidates" in metric_names
    assert "retention.executor.runner_control_retention.applied" in metric_names
    assert "retention.executor.runner_control_retention.protected" in metric_names
    assert "retention.executor.runner_control_retention.failures" in metric_names
    assert (
        "retention.executor.runner_control_retention.failure.storage_timeout"
        in metric_names
    )
    assert "42" not in str(emitted)
    assert "tenant_id" not in str(emitted)
    assert "payload" not in str(emitted)
    assert "object_key" not in str(emitted)
    assert "prompt" not in str(emitted)
    assert "secret" not in str(emitted)


def test_legacy_class_labels_are_normalized_before_returning_results() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                result_class="operational_logs",
                decisions=(
                    {
                        "retention_class": "operational_logs",
                        "outcome": RETENTION_DECISION_CANDIDATE,
                        "reason_code": "operational_log_retention_expired",
                        "resource_id": "agent_log:1",
                    },
                ),
                db=db,
            ),
        ),
    )

    result = orchestrator.run(request)

    assert result.results[0].retention_class == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
    assert (
        result.results[0].decisions[0].retention_class
        == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
    )


def test_runtime_ephemeral_legacy_label_uses_executor_context() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_ARTIFACT_PAYLOAD,),
    )
    orchestrator = _orchestrator(
        db,
        executors=(
            _Executor(
                name="artifact.retention",
                retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
                result_class="runtime_ephemeral",
                decisions=(
                    RetentionDecision(
                        retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
                        outcome=RETENTION_DECISION_APPLIED,
                        reason_code="artifact_payload_retention_expired",
                    ),
                ),
                db=db,
            ),
        ),
    )

    result = orchestrator.run(request)

    assert result.results[0].retention_class == RETENTION_CLASS_ARTIFACT_PAYLOAD


def test_default_registry_runs_noop_executors_for_unimplemented_modules() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(RETENTION_CLASS_REPORTING,),
    )

    result = _orchestrator(db).run(request)

    assert result.succeeded is True
    assert len(result.results) == 1
    assert isinstance(result.results[0], RetentionExecutorResult)
    assert result.results[0].executor_name == "reporting.retention"
    assert result.results[0].retention_class == RETENTION_CLASS_REPORTING
    assert result.results[0].counts.batch_limit == 10
    assert result.results[0].counts.batch_count == 0
    assert result.results[0].reason_counts == {}


def test_default_policy_resolver_path_uses_keyword_tenant_id() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=3,
        retention_classes=(RETENTION_CLASS_REPORTING,),
    )

    result = RetentionOrchestrator(db, executors=()).run(request)  # type: ignore[arg-type]

    assert result.succeeded is True
    assert result.results[0].tenant_id == 3
    assert result.results[0].executor_name == "reporting.retention"
    assert result.results[0].counts.batch_limit == 100
    assert db.queried_models == [TenantDataManagementSettings]
    assert db.rollback_count == 1


def test_per_tenant_apply_rolls_back_tenant_when_one_executor_fails() -> None:
    db = _FakeSession()
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=1,
        retention_classes=(
            RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            RETENTION_CLASS_TASK_RECORD,
        ),
    )
    orchestrator = _orchestrator(
        db,
        transaction_boundary=RETENTION_TRANSACTION_BOUNDARY_TENANT,
        executors=(
            _Executor(
                name="runner_control.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                db=db,
                marker="first",
            ),
            _Executor(
                name="task.retention",
                retention_class=RETENTION_CLASS_TASK_RECORD,
                db=db,
                marker="second",
                fail=True,
            ),
        ),
    )

    result = orchestrator.run(request)

    assert result.succeeded is False
    assert db.committed == []
    assert db.pending == []
    assert db.commit_count == 0
    assert db.rollback_count >= 1


def _orchestrator(
    db: _FakeSession,
    *,
    policies: dict[int, _Policy] | None = None,
    executors: tuple[_Executor, ...] = (),
    transaction_boundary: str = "executor",
    audit_emitter: Any | None = None,
) -> RetentionOrchestrator:
    resolved_policies = policies or {
        1: _Policy(tenant_id=1, retention_batch_size_per_tenant=10),
    }
    return RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        executors=executors,
        executor_order=(
            _executor_order_for(executors) if executors else DEFAULT_EXECUTOR_ORDER
        ),
        transaction_boundary=transaction_boundary,
        policy_resolver=lambda _db, tenant_id: resolved_policies[tenant_id],  # type: ignore[arg-type, return-value]
        audit_emitter=audit_emitter,
    )


def _executor_order_for(
    executors: tuple[_Executor, ...],
) -> tuple[RetentionExecutorOrderEntry, ...]:
    executor_names = {executor.name for executor in executors}
    return tuple(
        entry for entry in DEFAULT_EXECUTOR_ORDER if entry.executor_name in executor_names
    )
