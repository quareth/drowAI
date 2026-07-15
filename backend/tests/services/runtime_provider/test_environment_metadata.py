"""Tests for runtime-provider environment metadata compatibility helper."""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.runtime_provider import environment_metadata


def test_load_runtime_environment_metadata_queries_provider_and_returns_items_map(
    monkeypatch,
) -> None:
    """Compatibility helper should read full environment map from query items."""

    fake_session = SimpleNamespace(closed=False)

    def _close() -> None:
        fake_session.closed = True

    fake_session.close = _close

    class _RuntimeOperationsStub:
        def __init__(self, db) -> None:
            assert db is fake_session

        def context_for_internal_task(self, **kwargs):
            assert kwargs["task_id"] == 34
            return SimpleNamespace(task_id=34)

        async def run_for_context(self, *, context, operation, call, payload=None, metadata=None):
            assert context.task_id == 34
            assert operation == "query_runtime_environment_metadata"
            assert payload is None
            assert metadata["wait_for_result"] is True
            assert metadata["wait_timeout_seconds"] == 5.0
            call(SimpleNamespace(query_runtime_environment_metadata=lambda _request: None), object())
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "workspace_id": "task-34",
                        "items": {"agent.version": "4.0.0"},
                    }
                },
            )

    monkeypatch.setattr(environment_metadata, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(environment_metadata, "RuntimeOperationService", _RuntimeOperationsStub)

    loaded = environment_metadata.load_runtime_environment_metadata(
        task_id=34,
        actor_id="intent_classifier",
    )

    assert loaded == {"agent.version": "4.0.0"}
    assert fake_session.closed is True


def test_load_runtime_environment_metadata_prefers_structured_environment(
    monkeypatch,
) -> None:
    """Structured runner-owned environment should win over compatibility items."""

    fake_session = SimpleNamespace(closed=False)

    def _close() -> None:
        fake_session.closed = True

    fake_session.close = _close
    structured_environment = {
        "hostname": "runner-task",
        "network": {
            "interfaces": [{"name": "eth0", "ipv4": "172.17.0.2/16", "state": "UP"}],
            "default_gateway": "172.17.0.1",
            "dns": ["192.168.65.7"],
            "routes": [],
        },
    }

    class _RuntimeOperationsStub:
        def __init__(self, db) -> None:
            assert db is fake_session

        def context_for_internal_task(self, **kwargs):
            assert kwargs["task_id"] == 35
            return SimpleNamespace(task_id=35)

        async def run_for_context(self, *, context, operation, call, payload=None, metadata=None):
            assert context.task_id == 35
            assert operation == "query_runtime_environment_metadata"
            assert payload is None
            assert metadata["wait_for_result"] is True
            call(SimpleNamespace(query_runtime_environment_metadata=lambda _request: None), object())
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "workspace_id": "task-35",
                        "environment": structured_environment,
                        "items": {"agent.version": "4.0.0"},
                    }
                },
            )

    monkeypatch.setattr(environment_metadata, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(environment_metadata, "RuntimeOperationService", _RuntimeOperationsStub)

    loaded = environment_metadata.load_runtime_environment_metadata(
        task_id=35,
        actor_id="planner",
    )

    assert loaded == structured_environment
    assert fake_session.closed is True


_STRUCTURED_ENV = {
    "hostname": "kali-task-52",
    "os": {"name": "Kali GNU/Linux Rolling", "version": "2026.2"},
    "network": {
        "interfaces": [{"name": "eth0", "ipv4": "172.17.0.2/16", "state": "UP"}],
        "default_gateway": "172.17.0.1",
        "dns_servers": ["192.168.65.7"],
    },
    "routes": [],
}


def test_extract_environment_from_result_json_reads_started_payload() -> None:
    """Environment persisted on a runtime.started result_json is extracted."""

    result_json = {
        "source": "runner_event",
        "message_type": "runtime.started",
        "status": "succeeded",
        "result": {
            "runtime_job_id": "job-1",
            "workspace_id": "task-52",
            "environment_info": _STRUCTURED_ENV,
        },
    }

    assert environment_metadata._extract_environment_from_result_json(result_json) == _STRUCTURED_ENV


def test_extract_environment_from_result_json_ignores_non_canonical() -> None:
    """Flat or missing environment payloads do not count as canonical env info."""

    assert environment_metadata._extract_environment_from_result_json(None) is None
    assert environment_metadata._extract_environment_from_result_json({"result": {}}) is None
    assert (
        environment_metadata._extract_environment_from_result_json(
            {"result": {"environment_info": {"agent.version": "4.0.0"}}}
        )
        is None
    )


def test_resolve_local_prefers_task_start_runtime_job(monkeypatch) -> None:
    """TASK_START-persisted env wins and the workspace fallback is not consulted."""

    monkeypatch.setattr(
        environment_metadata,
        "_load_task_start_environment_metadata",
        lambda *, task_id: dict(_STRUCTURED_ENV),
    )

    def _fail_workspace(*, task_id):  # pragma: no cover - must not be called
        raise AssertionError("workspace fallback should not run when start job has env")

    monkeypatch.setattr(environment_metadata, "_load_workspace_environment_info", _fail_workspace)

    assert environment_metadata.resolve_local_runtime_environment_info(task_id=52) == _STRUCTURED_ENV


def test_resolve_local_falls_back_to_workspace(monkeypatch) -> None:
    """When no start-job env exists, the local workspace file is used."""

    monkeypatch.setattr(
        environment_metadata,
        "_load_task_start_environment_metadata",
        lambda *, task_id: None,
    )
    monkeypatch.setattr(
        environment_metadata,
        "_load_workspace_environment_info",
        lambda *, task_id: dict(_STRUCTURED_ENV),
    )

    assert environment_metadata.resolve_local_runtime_environment_info(task_id=52) == _STRUCTURED_ENV


def test_resolve_local_falls_back_when_start_read_raises(monkeypatch) -> None:
    """A failure reading the start job degrades to the workspace fallback."""

    def _raise(*, task_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(environment_metadata, "_load_task_start_environment_metadata", _raise)
    monkeypatch.setattr(
        environment_metadata,
        "_load_workspace_environment_info",
        lambda *, task_id: dict(_STRUCTURED_ENV),
    )

    assert environment_metadata.resolve_local_runtime_environment_info(task_id=52) == _STRUCTURED_ENV


def test_resolve_local_returns_none_when_unavailable(monkeypatch) -> None:
    """No env anywhere resolves to None."""

    monkeypatch.setattr(
        environment_metadata,
        "_load_task_start_environment_metadata",
        lambda *, task_id: None,
    )
    monkeypatch.setattr(
        environment_metadata,
        "_load_workspace_environment_info",
        lambda *, task_id: None,
    )

    assert environment_metadata.resolve_local_runtime_environment_info(task_id=52) is None
