"""Tests for bounded retention scheduling and executor ordering helpers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.services.retention.contracts import (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_ALL_TENANTS,
    RETENTION_SCOPE_TENANT,
    RetentionRunRequest,
)
from backend.services.retention.scheduling import (
    DEFAULT_EXECUTOR_ORDER,
    RetentionExecutorOrderEntry,
    build_tenant_execution_plan,
    ordered_executor_entries,
    resolve_per_tenant_limit,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    retention_batch_size_per_tenant: int


def test_per_tenant_limit_uses_policy_and_caps_request_limit() -> None:
    policy = _Policy(retention_batch_size_per_tenant=50)

    assert resolve_per_tenant_limit(policy=policy, request_limit=None) == 50
    assert resolve_per_tenant_limit(policy=policy, request_limit=10) == 10
    assert resolve_per_tenant_limit(policy=policy, request_limit=75) == 50


def test_per_tenant_limit_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError, match="policy.retention_batch_size_per_tenant"):
        resolve_per_tenant_limit(policy=_Policy(retention_batch_size_per_tenant=0))

    with pytest.raises(ValueError, match="request.limit_per_tenant"):
        resolve_per_tenant_limit(
            policy=_Policy(retention_batch_size_per_tenant=10),
            request_limit=0,
        )


def test_build_tenant_execution_plan_applies_limits_to_each_tenant() -> None:
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_DRY_RUN,
        scope=RETENTION_SCOPE_ALL_TENANTS,
        tenant_id=None,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
        limit_per_tenant=10,
    )
    plan = build_tenant_execution_plan(
        request=request,
        tenant_ids=(1, 2),
        policies={
            1: _Policy(retention_batch_size_per_tenant=5),
            2: _Policy(retention_batch_size_per_tenant=50),
        },
    )

    assert [(item.tenant_id, item.executor_name, item.limit) for item in plan] == [
        (1, "runner_control.retention", 5),
        (1, "knowledge.retention", 5),
        (2, "runner_control.retention", 10),
        (2, "knowledge.retention", 10),
    ]
    assert all(item.mode == RETENTION_RUN_MODE_DRY_RUN for item in plan)


def test_build_tenant_execution_plan_requires_finite_tenant_batch() -> None:
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_ALL_TENANTS,
        tenant_id=None,
    )

    with pytest.raises(ValueError, match="tenant_ids exceeds max_tenants_per_run"):
        build_tenant_execution_plan(
            request=request,
            tenant_ids=(1, 2, 3),
            policies={
                1: _Policy(retention_batch_size_per_tenant=10),
                2: _Policy(retention_batch_size_per_tenant=10),
                3: _Policy(retention_batch_size_per_tenant=10),
            },
            max_tenants_per_run=2,
        )


def test_tenant_scoped_plan_uses_requested_tenant_without_unbounded_iteration() -> None:
    request = RetentionRunRequest(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=42,
        retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
    )
    plan = build_tenant_execution_plan(
        request=request,
        tenant_ids=(),
        policies={42: _Policy(retention_batch_size_per_tenant=25)},
    )

    assert [(item.executor_name, item.tenant_id, item.limit) for item in plan] == [
        ("runner_control.retention", 42, 25),
        ("knowledge.retention", 42, 25),
    ]


def test_executor_ordering_is_explicit_documented_and_dependency_safe() -> None:
    entries = ordered_executor_entries()
    by_class = {entry.retention_class: entry.order for entry in entries}

    assert entries == DEFAULT_EXECUTOR_ORDER
    assert all(entry.dependency_note.strip() for entry in entries)
    assert by_class[RETENTION_CLASS_RUNTIME_RESUME_STATE] < by_class[
        RETENTION_CLASS_TASK_RECORD
    ]
    assert by_class[RETENTION_CLASS_TASK_TRANSCRIPT] < by_class[
        RETENTION_CLASS_TASK_RECORD
    ]


def test_executor_ordering_rejects_duplicate_names_or_orders() -> None:
    duplicate_order = (
        RetentionExecutorOrderEntry(
            order=10,
            executor_name="first.retention",
            retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            dependency_note="first",
        ),
        RetentionExecutorOrderEntry(
            order=10,
            executor_name="second.retention",
            retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
            dependency_note="second",
        ),
    )
    duplicate_name = (
        RetentionExecutorOrderEntry(
            order=10,
            executor_name="same.retention",
            retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            dependency_note="first",
        ),
        RetentionExecutorOrderEntry(
            order=20,
            executor_name="same.retention",
            retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
            dependency_note="second",
        ),
    )

    with pytest.raises(ValueError, match="duplicate executor order"):
        ordered_executor_entries(executors=duplicate_order)

    with pytest.raises(ValueError, match="duplicate executor name"):
        ordered_executor_entries(executors=duplicate_name)
