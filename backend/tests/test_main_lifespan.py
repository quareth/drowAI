"""Tests startup wiring in backend.main lifespan hooks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect, text

import backend.database as database_module
import backend.main as main_module
import backend.services.platform.background_services as background_services_module
import backend.services.platform.installation_service as installation_service_module
import backend.services.websocket.connection_manager as websocket_manager_module
from backend.config.deployment_topology import DeploymentProfileValidationError


class _AsyncServiceStub:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.is_running = False

    async def start(self) -> None:
        self.start_calls += 1
        self.is_running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False


class _TerminalManagerStub:
    def __init__(self) -> None:
        self.start_calls = 0
        self.cleanup_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    async def cleanup_all_sessions(self) -> None:
        self.cleanup_calls += 1


class _WebsocketManagerStub:
    def __init__(self) -> None:
        self.start_cleanup_calls = 0
        self.stop_cleanup_calls = 0

    def start_cleanup_task(self) -> None:
        self.start_cleanup_calls += 1

    async def stop_cleanup_task(self) -> None:
        self.stop_cleanup_calls += 1


class _DbSessionStub:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _CompleteInstallationService:
    def __init__(self, _db) -> None:
        pass

    def repair_legacy_installation_if_needed(self) -> bool:
        return False

    def is_setup_required(self) -> bool:
        return False


class _PendingInstallationService:
    def __init__(self, _db) -> None:
        pass

    def repair_legacy_installation_if_needed(self) -> bool:
        return False

    def is_setup_required(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_health_reports_background_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "background_service_status",
        lambda: {
            "background_services_started": False,
            "report_scheduler_running": False,
        },
    )

    response = await main_module.health_check()

    assert response["status"] == "healthy"
    assert response["background_services_started"] is False
    assert response["report_scheduler_running"] is False


@pytest.fixture(autouse=True)
def _reset_background_services_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        installation_service_module,
        "PlatformInstallationService",
        _CompleteInstallationService,
    )
    background_services_module._state.started = False
    background_services_module._state.retention_task = None
    yield
    background_services_module._state.started = False
    background_services_module._state.retention_task = None


def _patch_background_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler: _AsyncServiceStub,
    metrics: _AsyncServiceStub,
    terminal_manager: _TerminalManagerStub,
    websocket_manager: _WebsocketManagerStub,
    report_scheduler: _AsyncServiceStub | None = None,
) -> None:
    monkeypatch.setattr(background_services_module, "cve_sync_scheduler", scheduler)
    monkeypatch.setattr(
        background_services_module,
        "report_scheduler",
        report_scheduler or _AsyncServiceStub(),
    )
    monkeypatch.setattr(background_services_module, "metrics", metrics)
    monkeypatch.setattr(
        background_services_module, "terminal_session_manager", terminal_manager
    )
    monkeypatch.setattr(
        websocket_manager_module, "websocket_manager", websocket_manager
    )


@pytest.mark.asyncio
async def test_background_service_start_repairs_dead_report_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    report_scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        report_scheduler=report_scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )
    background_services_module._state.started = True

    started = await background_services_module.start_background_services()

    assert started is True
    assert report_scheduler.start_calls == 1
    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0


@pytest.mark.asyncio
async def test_background_service_start_is_concurrently_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    report_scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        report_scheduler=report_scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    results = await asyncio.gather(
        background_services_module.start_background_services(),
        background_services_module.start_background_services(),
    )

    assert results == [True, True]
    assert report_scheduler.start_calls == 1
    assert scheduler.start_calls == 1
    assert metrics.start_calls == 1
    await background_services_module.stop_background_services()


def _prepare_pre_runner_control_schema(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(
            text(
                """
                CREATE TABLE tenants (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NULL,
                    tenant_id INTEGER NULL,
                    name TEXT NULL
                )
                """
            )
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_background_schedulers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    report_scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    bootstrap_calls = {"count": 0}
    monkeypatch.setattr(
        main_module,
        "bootstrap_default_tenant_state",
        lambda: bootstrap_calls.__setitem__("count", bootstrap_calls["count"] + 1),
    )
    initialize_checkpointer_schema = AsyncMock(return_value=True)
    monkeypatch.setattr(
        main_module,
        "initialize_checkpointer_schema",
        initialize_checkpointer_schema,
    )
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
        report_scheduler=report_scheduler,
    )

    async with main_module.lifespan(main_module.app):
        pass

    assert scheduler.start_calls == 1
    assert scheduler.stop_calls == 1
    assert report_scheduler.start_calls == 1
    assert report_scheduler.stop_calls == 1
    assert metrics.start_calls == 1
    assert metrics.stop_calls == 1
    assert terminal_manager.start_calls == 1
    assert terminal_manager.cleanup_calls == 1
    assert websocket_manager.start_cleanup_calls == 1
    assert websocket_manager.stop_cleanup_calls == 1
    assert bootstrap_calls["count"] == 1
    initialize_checkpointer_schema.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_lifespan_defers_background_services_while_setup_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    report_scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()

    monkeypatch.setattr(
        installation_service_module,
        "PlatformInstallationService",
        _PendingInstallationService,
    )
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "bootstrap_default_tenant_state", lambda: None)
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
        report_scheduler=report_scheduler,
    )

    async with main_module.lifespan(main_module.app):
        pass

    assert scheduler.start_calls == 0
    assert scheduler.stop_calls == 0
    assert report_scheduler.start_calls == 0
    assert report_scheduler.stop_calls == 0
    assert metrics.start_calls == 0
    assert metrics.stop_calls == 0
    assert terminal_manager.start_calls == 0
    assert terminal_manager.cleanup_calls == 0
    assert websocket_manager.start_cleanup_calls == 0
    assert websocket_manager.stop_cleanup_calls == 0


@pytest.mark.asyncio
async def test_lifespan_fails_fast_when_tenant_baseline_schema_readiness_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    monkeypatch.setattr(
        main_module,
        "ensure_tenant_baseline_schema_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("tenant-baseline missing")),
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    with pytest.raises(RuntimeError, match="tenant-baseline missing"):
        async with main_module.lifespan(main_module.app):
            pass

    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0
    assert terminal_manager.start_calls == 0
    assert websocket_manager.start_cleanup_calls == 0


@pytest.mark.asyncio
async def test_lifespan_fails_closed_for_invalid_product_deployment_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()

    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "local")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "true")
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    with pytest.raises(
        DeploymentProfileValidationError,
        match="TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
    ):
        async with main_module.lifespan(main_module.app):
            pass

    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0
    assert terminal_manager.start_calls == 0
    assert websocket_manager.start_cleanup_calls == 0


@pytest.mark.asyncio
async def test_lifespan_rejects_existing_local_placement_tasks_for_product_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    db_stub = _DbSessionStub()
    migration_calls: list[str] = []

    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "runner")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "true")
    monkeypatch.setattr(main_module, "SessionLocal", lambda: db_stub)
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "bootstrap_default_tenant_state", lambda: None)

    def _migration_stub(_db, *, deployment_profile: str):
        assert scheduler.start_calls == 0
        migration_calls.append(deployment_profile)
        return SimpleNamespace(
            changed_count=1, message="PRODUCT_LOCAL_RUNTIME_REJECTED task_ids=[1]"
        )

    monkeypatch.setattr(
        main_module,
        "fail_closed_active_local_placement_tasks",
        _migration_stub,
    )
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    async with main_module.lifespan(main_module.app):
        pass

    assert migration_calls == ["single_host"]
    assert db_stub.commit_calls == 2
    assert db_stub.rollback_calls == 0
    assert db_stub.close_calls == 2
    assert scheduler.start_calls == 1
    assert scheduler.stop_calls == 1


@pytest.mark.asyncio
async def test_lifespan_fails_fast_when_tenant_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "ensure_runner_control_schema_ready", lambda: None)
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    monkeypatch.setattr(
        main_module,
        "bootstrap_default_tenant_state",
        lambda: (_ for _ in ()).throw(RuntimeError("tenant bootstrap failed")),
    )
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    with pytest.raises(RuntimeError, match="tenant bootstrap failed"):
        async with main_module.lifespan(main_module.app):
            pass

    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0
    assert terminal_manager.start_calls == 0
    assert websocket_manager.start_cleanup_calls == 0


@pytest.mark.asyncio
async def test_lifespan_fails_fast_when_runner_control_schema_readiness_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(
        main_module,
        "ensure_runner_control_schema_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("runner-control missing")),
    )
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    with pytest.raises(RuntimeError, match="runner-control missing"):
        async with main_module.lifespan(main_module.app):
            pass

    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0
    assert terminal_manager.start_calls == 0
    assert websocket_manager.start_cleanup_calls == 0


@pytest.mark.asyncio
async def test_lifespan_checks_runner_control_schema_before_startup_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "lifespan_runner_control_readiness.sqlite"
    database_url = f"sqlite:///{db_path}"
    _prepare_pre_runner_control_schema(database_url)

    runtime_engine = create_engine(database_url, future=True)
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()

    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setattr(database_module, "engine", runtime_engine)
    monkeypatch.setattr(
        main_module,
        "ensure_runner_control_schema_ready",
        database_module.ensure_runner_control_schema_ready,
    )
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "bootstrap_default_tenant_state", lambda: None)
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    with pytest.raises(
        RuntimeError, match="Runner control-plane schema is not applied"
    ):
        async with main_module.lifespan(main_module.app):
            pass

    inspector = inspect(runtime_engine)
    assert not inspector.has_table("execution_sites")
    assert not inspector.has_table("runners")
    assert scheduler.start_calls == 0
    assert metrics.start_calls == 0
    assert terminal_manager.start_calls == 0
    assert websocket_manager.start_cleanup_calls == 0

    runtime_engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_local_mode_starts_without_runner_control_schema_readiness_requirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "lifespan_local_mode.sqlite"
    database_url = f"sqlite:///{db_path}"
    _prepare_pre_runner_control_schema(database_url)

    runtime_engine = create_engine(database_url, future=True)
    scheduler = _AsyncServiceStub()
    metrics = _AsyncServiceStub()
    terminal_manager = _TerminalManagerStub()
    websocket_manager = _WebsocketManagerStub()

    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "false")
    monkeypatch.setattr(database_module, "engine", runtime_engine)
    monkeypatch.setattr(
        main_module,
        "ensure_runner_control_schema_ready",
        database_module.ensure_runner_control_schema_ready,
    )
    monkeypatch.setattr(
        main_module, "ensure_tenant_baseline_schema_ready", lambda: None
    )
    monkeypatch.setattr(
        main_module, "ensure_reporting_lifecycle_schema_ready", lambda: None
    )
    monkeypatch.setattr(main_module, "bootstrap_default_tenant_state", lambda: None)
    _patch_background_services(
        monkeypatch,
        scheduler=scheduler,
        metrics=metrics,
        terminal_manager=terminal_manager,
        websocket_manager=websocket_manager,
    )

    async with main_module.lifespan(main_module.app):
        pass

    assert scheduler.start_calls == 1
    assert scheduler.stop_calls == 1
    assert metrics.start_calls == 1
    assert metrics.stop_calls == 1
    assert terminal_manager.start_calls == 1
    assert terminal_manager.cleanup_calls == 1
    assert websocket_manager.start_cleanup_calls == 1
    assert websocket_manager.stop_cleanup_calls == 1

    runtime_engine.dispose()
