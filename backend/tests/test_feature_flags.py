"""Tests for typed backend feature flag helpers."""

from __future__ import annotations

import pytest

from backend.config.feature_flags import (
    get_deployment_profile,
    get_default_task_runtime_placement_mode,
    get_local_max_active_tasks_default,
    get_object_store_backend,
    get_task_max_concurrent_per_tenant_default,
    get_task_max_concurrent_per_user_default,
    get_knowledge_vulnerability_min_confidence,
    is_cloud_runner_control_enabled,
    is_knowledge_cve_lookup_enabled,
    is_knowledge_vulnerability_candidates_enabled,
    resolve_task_concurrency_limit,
)


def test_vulnerability_candidates_toggle_defaults_enabled(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_KNOWLEDGE_VULNERABILITY_CANDIDATES", raising=False)
    assert is_knowledge_vulnerability_candidates_enabled() is True


def test_cve_lookup_feature_flag_defaults_enabled(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", raising=False)
    assert is_knowledge_cve_lookup_enabled() is True


def test_cve_lookup_feature_flag_parses_boolean_env(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "false")
    assert is_knowledge_cve_lookup_enabled() is False

    monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "true")
    assert is_knowledge_cve_lookup_enabled() is True


def test_vulnerability_candidates_toggle_parses_boolean_env(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_VULNERABILITY_CANDIDATES", "false")
    assert is_knowledge_vulnerability_candidates_enabled() is False

    monkeypatch.setenv("ENABLE_KNOWLEDGE_VULNERABILITY_CANDIDATES", "true")
    assert is_knowledge_vulnerability_candidates_enabled() is True


def test_vulnerability_min_confidence_defaults_to_point_eight(monkeypatch) -> None:
    monkeypatch.delenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", raising=False)
    assert get_knowledge_vulnerability_min_confidence() == 0.80


def test_vulnerability_min_confidence_reads_valid_env(monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "0.93")
    assert get_knowledge_vulnerability_min_confidence() == 0.93


def test_vulnerability_min_confidence_invalid_env_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "not-a-number")
    assert get_knowledge_vulnerability_min_confidence() == 0.80

    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "1.2")
    assert get_knowledge_vulnerability_min_confidence() == 0.80

    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "-0.2")
    assert get_knowledge_vulnerability_min_confidence() == 0.80


def test_default_runtime_placement_mode_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", raising=False)
    assert get_default_task_runtime_placement_mode() == "local"


def test_default_runtime_placement_mode_allows_explicit_runner(monkeypatch) -> None:
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "runner")
    assert get_default_task_runtime_placement_mode() == "runner"


def test_default_runtime_placement_mode_invalid_env_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "invalid-mode")

    with pytest.raises(ValueError) as error:
        get_default_task_runtime_placement_mode()

    message = str(error.value)
    assert "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT" in message
    assert "local" in message
    assert "runner" in message


def test_deployment_profile_defaults_to_dev_local(monkeypatch) -> None:
    monkeypatch.delenv("DROWAI_DEPLOYMENT_PROFILE", raising=False)
    assert get_deployment_profile() == "dev_local"


def test_deployment_profile_reads_explicit_profile_env(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    assert get_deployment_profile() == "single_host"


def test_deployment_profile_invalid_env_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "invalid-profile")
    with pytest.raises(ValueError) as error:
        get_deployment_profile()
    assert "DROWAI_DEPLOYMENT_PROFILE" in str(error.value)


def test_cloud_runner_control_flag_defaults_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_CLOUD_RUNNER_CONTROL", raising=False)
    assert is_cloud_runner_control_enabled() is False


def test_cloud_runner_control_flag_parses_enabled_value(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    assert is_cloud_runner_control_enabled() is True


def test_data_plane_object_store_backend_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("DATA_PLANE_OBJECT_STORE_BACKEND", raising=False)
    assert get_object_store_backend() == "local"


def test_task_concurrency_global_defaults_read_positive_env_values(monkeypatch) -> None:
    monkeypatch.setenv("TASK_MAX_CONCURRENT_PER_TENANT", "8")
    monkeypatch.setenv("TASK_MAX_CONCURRENT_PER_USER", "3")
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "5")

    assert get_task_max_concurrent_per_tenant_default() == 8
    assert get_task_max_concurrent_per_user_default() == 3
    assert get_local_max_active_tasks_default() == 5


def test_task_concurrency_global_defaults_honor_zero_value(monkeypatch) -> None:
    monkeypatch.setenv("TASK_MAX_CONCURRENT_PER_TENANT", "0")
    monkeypatch.setenv("TASK_MAX_CONCURRENT_PER_USER", "0")
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "0")

    assert get_task_max_concurrent_per_tenant_default() == 0
    assert get_task_max_concurrent_per_user_default() == 0
    assert get_local_max_active_tasks_default() == 0


@pytest.mark.parametrize("name", [
    "TASK_MAX_CONCURRENT_PER_TENANT",
    "TASK_MAX_CONCURRENT_PER_USER",
    "LOCAL_MAX_ACTIVE_TASKS",
])
def test_task_concurrency_global_defaults_reject_negative_values(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "-1")
    with pytest.raises(ValueError):
        if name == "TASK_MAX_CONCURRENT_PER_TENANT":
            get_task_max_concurrent_per_tenant_default()
        elif name == "TASK_MAX_CONCURRENT_PER_USER":
            get_task_max_concurrent_per_user_default()
        else:
            get_local_max_active_tasks_default()


@pytest.mark.parametrize("name", [
    "TASK_MAX_CONCURRENT_PER_TENANT",
    "TASK_MAX_CONCURRENT_PER_USER",
    "LOCAL_MAX_ACTIVE_TASKS",
])
def test_task_concurrency_global_defaults_reject_non_integer_values(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "bad-value")
    with pytest.raises(ValueError):
        if name == "TASK_MAX_CONCURRENT_PER_TENANT":
            get_task_max_concurrent_per_tenant_default()
        elif name == "TASK_MAX_CONCURRENT_PER_USER":
            get_task_max_concurrent_per_user_default()
        else:
            get_local_max_active_tasks_default()


def test_resolve_task_concurrency_limit_honors_explicit_zero_row_limit() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=0,
        tenant_default_limit=4,
        global_default_limit=2,
    )
    assert resolved == 0


def test_resolve_task_concurrency_limit_honors_explicit_zero_tenant_limit() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=None,
        tenant_default_limit=0,
        global_default_limit=2,
    )
    assert resolved == 0


def test_resolve_task_concurrency_limit_honors_explicit_zero_global_limit() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=None,
        tenant_default_limit=None,
        global_default_limit=0,
    )
    assert resolved == 0


@pytest.mark.parametrize(
    ("row_limit", "tenant_default_limit", "global_default_limit"),
    [(-1, None, None), (None, -1, None), (None, None, -1)],
)
def test_resolve_task_concurrency_limit_rejects_negative_values(
    row_limit: int | None,
    tenant_default_limit: int | None,
    global_default_limit: int | None,
) -> None:
    with pytest.raises(ValueError):
        resolve_task_concurrency_limit(
            row_limit=row_limit,
            tenant_default_limit=tenant_default_limit,
            global_default_limit=global_default_limit,
        )


def test_resolve_task_concurrency_limit_uses_row_before_tenant_and_global() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=6,
        tenant_default_limit=4,
        global_default_limit=2,
    )
    assert resolved == 6


def test_resolve_task_concurrency_limit_uses_tenant_before_global() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=None,
        tenant_default_limit=4,
        global_default_limit=2,
    )
    assert resolved == 4


def test_resolve_task_concurrency_limit_uses_global_before_unlimited() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=None,
        tenant_default_limit=None,
        global_default_limit=2,
    )
    assert resolved == 2


def test_resolve_task_concurrency_limit_returns_unlimited_when_no_limits() -> None:
    resolved = resolve_task_concurrency_limit(
        row_limit=None,
        tenant_default_limit=None,
        global_default_limit=None,
    )
    assert resolved is None
