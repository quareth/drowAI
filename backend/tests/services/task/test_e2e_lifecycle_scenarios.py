"""Contract tests for process-gated deterministic task lifecycle scenarios."""

from __future__ import annotations

import pytest

from backend.services.task.lifecycle_service import (
    E2E_COMPLETION_SCOPE,
    E2E_FAILURE_RETRY_SCOPE,
    deterministic_e2e_bootstrap_statuses,
)
from backend.services.task.runtime_service import (
    deterministic_e2e_transition_targets,
    resolve_e2e_runtime_call_scope,
)
from backend.services.task.cleanup_service import resolve_task_delete_runtime_scope
from backend.services.runtime_provider.contracts import RuntimeCallScope


def test_bootstrap_scenarios_cover_running_failure_and_completion() -> None:
    """UI-created tasks can enter deterministic running, failed, and completed states."""
    assert deterministic_e2e_bootstrap_statuses("127.0.0.1") == (
        "queued",
        "starting",
        "running",
    )
    assert deterministic_e2e_bootstrap_statuses(E2E_FAILURE_RETRY_SCOPE) == (
        "queued",
        "starting",
        "failed",
    )
    assert deterministic_e2e_bootstrap_statuses(E2E_COMPLETION_SCOPE) == (
        "queued",
        "starting",
        "running",
        "completed",
    )


@pytest.mark.parametrize(
    ("action", "current_status", "expected"),
    [
        ("pause", "running", ("pausing", "paused")),
        ("resume", "paused", ("resuming", "running")),
        ("stop", "running", ("stopping", "stopped")),
        ("stop", "queued", ("stopped",)),
        ("start", "stopped", ("queued", "starting", "running")),
        ("start", "failed", ("queued", "starting", "running")),
    ],
)
def test_runtime_scenarios_follow_domain_valid_transitions(
    action: str,
    current_status: str,
    expected: tuple[str, ...],
) -> None:
    """Runtime actions simulate only domain-valid transitions without Docker."""
    assert deterministic_e2e_transition_targets(action, current_status) == expected


def test_suite_owned_delete_uses_test_runtime_scope_only_in_e2e_mode() -> None:
    """Deterministic cleanup may retire local fixtures without weakening production policy."""
    assert resolve_task_delete_runtime_scope(deterministic_mode=True) is RuntimeCallScope.TEST
    assert (
        resolve_task_delete_runtime_scope(deterministic_mode=False)
        is RuntimeCallScope.PRODUCT_TASK
    )


def test_runtime_canary_uses_test_scope_without_enabling_simulation() -> None:
    """The real-Docker canary may select local placement without product-policy drift."""
    assert (
        resolve_e2e_runtime_call_scope(
            RuntimeCallScope.PRODUCT_TASK,
            runtime_local_mode=True,
        )
        is RuntimeCallScope.TEST
    )
    assert (
        resolve_e2e_runtime_call_scope(
            RuntimeCallScope.PRODUCT_TASK,
            runtime_local_mode=False,
        )
        is RuntimeCallScope.PRODUCT_TASK
    )
