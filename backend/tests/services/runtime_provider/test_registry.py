"""Tests for runtime provider registry defaulting and fail-closed behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.runtime_provider.registry import (
    RuntimeProviderRegistry,
    UnsupportedRuntimePlacementError,
    resolve_task_runtime_placement_mode,
)
from backend.services.runtime_provider.runner_provider_selection import (
    ManagedRunnerProviderUnavailableError,
)


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name


def test_registry_defaults_to_local_provider_and_caches_instance():
    calls = 0

    def _factory():
        nonlocal calls
        calls += 1
        return _FakeProvider("local-fake")

    registry = RuntimeProviderRegistry(local_provider_factory=_factory)
    first_provider = registry.get_provider()
    second_provider = registry.get_provider()

    assert first_provider is second_provider
    assert first_provider.provider_name == "local-fake"
    assert calls == 1


def test_registry_selects_explicit_local_provider_and_caches_instance():
    local_calls = 0
    runner_calls = 0

    def _local_factory():
        nonlocal local_calls
        local_calls += 1
        return _FakeProvider("local-fake")

    def _runner_factory():
        nonlocal runner_calls
        runner_calls += 1
        return _FakeProvider("runner-fake")

    registry = RuntimeProviderRegistry(
        default_mode="runner",
        local_provider_factory=_local_factory,
        runner_provider_factory=_runner_factory,
    )
    first_provider = registry.get_provider(runtime_placement_mode="local")
    second_provider = registry.get_provider(runtime_placement_mode="local")

    assert first_provider is second_provider
    assert first_provider.provider_name == "local-fake"
    assert local_calls == 1
    assert runner_calls == 0


def test_registry_fails_closed_for_unknown_runtime_placement_mode():
    registry = RuntimeProviderRegistry(local_provider_factory=lambda: _FakeProvider("local"))

    with pytest.raises(UnsupportedRuntimePlacementError) as error:
        registry.get_provider(runtime_placement_mode="unexpected")

    assert "Unsupported task runtime placement mode" in str(error.value)


def test_registry_selects_runner_provider_when_mode_is_enabled():
    local_calls = 0
    runner_calls = 0

    def _local_factory():
        nonlocal local_calls
        local_calls += 1
        return _FakeProvider("local")

    def _runner_factory():
        nonlocal runner_calls
        runner_calls += 1
        return _FakeProvider("runner")

    registry = RuntimeProviderRegistry(
        default_mode="runner",
        local_provider_factory=_local_factory,
        runner_provider_factory=_runner_factory,
    )

    first_provider = registry.get_provider()
    second_provider = registry.get_provider()

    assert first_provider is second_provider
    assert first_provider.provider_name == "runner"
    assert runner_calls == 1
    assert local_calls == 0


def test_registry_selects_cloud_runner_provider_when_backend_is_cloud(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")

    registry = RuntimeProviderRegistry(default_mode="runner")

    selected = registry.get_provider()

    assert selected.provider_name == "cloud_runner"


def test_registry_fails_closed_when_cloud_backend_selected_without_feature_gate(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "false")

    registry = RuntimeProviderRegistry(default_mode="runner")

    with pytest.raises(ManagedRunnerProviderUnavailableError) as error:
        registry.get_provider()

    assert "ENABLE_CLOUD_RUNNER_CONTROL" in str(error.value)


def test_registry_allows_runner_provider_injection_for_tests():
    fake_provider = _FakeProvider("runner-test-double")
    registry = RuntimeProviderRegistry(
        default_mode="runner",
        provider_overrides={"runner": fake_provider},
        runner_provider_factory=lambda: _FakeProvider("should-not-be-used"),
    )

    selected_provider = registry.get_provider()

    assert selected_provider is fake_provider


def test_registry_fails_closed_for_unsupported_runtime_placement_mode():
    registry = RuntimeProviderRegistry(local_provider_factory=lambda: _FakeProvider("local"))

    with pytest.raises(UnsupportedRuntimePlacementError) as error:
        registry.get_provider(runtime_placement_mode="unsupported-mode")

    assert "`unsupported-mode`" in str(error.value)


def test_registry_rejects_chat_execution_mode_labels_as_runtime_placement():
    registry = RuntimeProviderRegistry(local_provider_factory=lambda: _FakeProvider("local"))

    with pytest.raises(UnsupportedRuntimePlacementError) as error:
        registry.get_provider(runtime_placement_mode="deep_reasoning")

    assert "`deep_reasoning`" in str(error.value)


def test_registry_allows_provider_override_for_tests():
    fake_provider = _FakeProvider("test-double")
    registry = RuntimeProviderRegistry(
        provider_overrides={"local": fake_provider},
        local_provider_factory=lambda: _FakeProvider("should-not-be-used"),
    )

    selected_provider = registry.get_provider(runtime_placement_mode="local")

    assert selected_provider is fake_provider


def test_resolve_task_runtime_placement_mode_defaults_to_local():
    task = SimpleNamespace(id=11)

    mode = resolve_task_runtime_placement_mode(task)

    assert mode.value == "local"


def test_resolve_task_runtime_placement_mode_reads_task_attribute():
    task = SimpleNamespace(id=11, runtime_placement_mode="local")

    mode = resolve_task_runtime_placement_mode(task, default_mode="runner")

    assert mode.value == "local"
