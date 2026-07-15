"""Tests for retention entrypoint privileged maintenance context wiring.

These tests verify `cleanup_agent_logs` executes retention under the explicit
trusted maintenance RLS bypass scope, delegates to the orchestrator, and
preserves return contract behavior.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from backend.services import retention
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_REPORTING,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_SCOPE_ALL_TENANTS,
    RetentionBatchCounts,
    RetentionExecutorResult,
    RetentionRunResult,
)
from backend.services.retention.orchestrator import RETENTION_EXECUTOR_FAILURE_CODE


class _FakeDb:
    def __init__(self) -> None:
        self.rollback_count = 0

    def rollback(self) -> None:
        self.rollback_count += 1


def test_cleanup_agent_logs_uses_maintenance_privileged_scope(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    orchestrator_requests = []

    @contextmanager
    def _fake_privileged_bypass(db, *, scope: str, actor_type: str):
        calls.append((scope, actor_type))
        yield

    class FakeOrchestrator:
        def __init__(self, db, *, executors, executor_order) -> None:
            assert executors == ("registered-executors",)
            assert tuple(item.executor_name for item in executor_order) == (
                "artifact.retention",
                "artifact_provenance.retention",
                "knowledge.retention",
                "knowledge.evidence_retention",
                "reporting.retention",
            )

        def run(self, request):
            orchestrator_requests.append(request)
            return _run_result(
                _executor_result(
                    executor_name="knowledge.retention",
                    retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                    applied_count=7,
                )
            )

    monkeypatch.setattr(retention, "RetentionOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        retention,
        "build_existing_retention_executors",
        lambda _db: ("registered-executors",),
    )
    monkeypatch.setattr(retention, "privileged_rls_bypass", _fake_privileged_bypass)

    db = _FakeDb()
    deleted = retention.cleanup_agent_logs(db=db)

    assert deleted == 7
    assert calls == [("maintenance", "system")]
    assert db.rollback_count == 0
    request = orchestrator_requests[0]
    assert request.mode == RETENTION_RUN_MODE_APPLY
    assert request.scope == RETENTION_SCOPE_ALL_TENANTS
    assert request.tenant_id is None
    assert request.retention_classes == (
        RETENTION_CLASS_ARTIFACT_PAYLOAD,
        RETENTION_CLASS_EXECUTION_PROVENANCE,
        RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
        RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        RETENTION_CLASS_REPORTING,
    )


def test_cleanup_agent_logs_sums_safe_applied_counts(monkeypatch) -> None:
    class FakeOrchestrator:
        def __init__(self, db, *, executors, executor_order) -> None:
            pass

        def run(self, request):
            return _run_result(
                _executor_result(
                    executor_name="knowledge.retention",
                    retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                    applied_count=3,
                    candidate_count=20,
                ),
                _executor_result(
                    executor_name="artifact.retention",
                    retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
                    applied_count=2,
                    candidate_count=9,
                ),
                _executor_result(
                    executor_name="reporting.retention",
                    retention_class=RETENTION_CLASS_REPORTING,
                    applied_count=5,
                    candidate_count=11,
                ),
            )

    monkeypatch.setattr(retention, "RetentionOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(retention, "privileged_rls_bypass", _passthrough_bypass)

    assert retention.cleanup_agent_logs(db=_FakeDb()) == 10


def test_cleanup_agent_logs_returns_zero_for_safe_failure_result(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeOrchestrator:
        def __init__(self, db, *, executors, executor_order) -> None:
            pass

        def run(self, request):
            return _run_result(
                _executor_result(
                    executor_name="knowledge.retention",
                    retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                    applied_count=4,
                    succeeded=False,
                    error_code=RETENTION_EXECUTOR_FAILURE_CODE,
                ),
                succeeded=False,
            )

    db = _FakeDb()
    monkeypatch.setattr(retention, "RetentionOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(retention, "privileged_rls_bypass", _passthrough_bypass)

    deleted = retention.cleanup_agent_logs(db=db)

    assert deleted == 0
    assert db.rollback_count == 1
    assert "retention_executor_failed" in caplog.text
    assert "secret" not in caplog.text.lower()


def test_cleanup_agent_logs_returns_zero_for_orchestrator_exception_without_secret_log(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeOrchestrator:
        def __init__(self, db, *, executors, executor_order) -> None:
            pass

        def run(self, request):
            raise RuntimeError("secret token payload must not leak")

    db = _FakeDb()
    monkeypatch.setattr(retention, "RetentionOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(retention, "privileged_rls_bypass", _passthrough_bypass)

    deleted = retention.cleanup_agent_logs(db=db)

    assert deleted == 0
    assert db.rollback_count == 1
    assert "retention cleanup failed before safe result was produced" in caplog.text
    assert "secret token payload" not in caplog.text


@contextmanager
def _passthrough_bypass(db, *, scope: str, actor_type: str):
    yield


def _executor_result(
    *,
    executor_name: str,
    retention_class: str,
    applied_count: int,
    candidate_count: int | None = None,
    succeeded: bool = True,
    error_code: str | None = None,
) -> RetentionExecutorResult:
    return RetentionExecutorResult(
        executor_name=executor_name,
        retention_class=retention_class,
        mode=RETENTION_RUN_MODE_APPLY,
        tenant_id=1,
        counts=RetentionBatchCounts(
            candidate_count=(
                int(candidate_count) if candidate_count is not None else applied_count
            ),
            applied_count=applied_count,
        ),
        reason_counts={},
        succeeded=succeeded,
        error_code=error_code,
    )


def _run_result(
    *results: RetentionExecutorResult,
    succeeded: bool = True,
) -> RetentionRunResult:
    return RetentionRunResult(
        mode=RETENTION_RUN_MODE_APPLY,
        scope=RETENTION_SCOPE_ALL_TENANTS,
        tenant_id=None,
        results=tuple(results),
        succeeded=succeeded,
    )
