"""Unit tests for Tenant Isolation tenant RLS session context helpers.

These tests verify PostgreSQL-only session variable behavior and request-session
cleanup boundaries used by `backend.database.get_db`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.database as database
from backend.services.tenant import rls


class _FakeSession:
    def __init__(self, *, dialect_name: str = "sqlite") -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.events: list[object] = []

    def get_bind(self):
        return self._bind

    def execute(self, statement, params=None):
        self.events.append((str(statement), params))
        return SimpleNamespace(one_or_none=lambda: None)

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


class _TaskLookupResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


def test_rls_helpers_are_noop_for_sqlite() -> None:
    db = _FakeSession(dialect_name="sqlite")

    rls.set_rls_session_context(db, tenant_id=10, user_id=20, actor_type="user")
    rls.clear_rls_session_context(db)

    assert db.events == []


def test_set_rls_session_context_sets_and_resets_postgres_variables() -> None:
    db = _FakeSession(dialect_name="postgresql")

    rls.set_rls_session_context(db, tenant_id=10, user_id=20, actor_type="user")
    rls.set_rls_session_context(db, tenant_id=None, user_id=20, actor_type=None)

    statements = [event[0] for event in db.events if isinstance(event, tuple)]
    assert statements.count("SELECT set_config(:setting_name, :setting_value, false)") >= 4
    assert "RESET app.current_tenant_id" in statements
    assert "RESET app.current_actor_type" in statements


def test_set_rls_session_context_sets_rls_enabled_feature_flag_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSession(dialect_name="postgresql")
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")

    rls.set_rls_session_context(db, tenant_id=11, user_id=22, actor_type="user")

    set_calls = [event for event in db.events if isinstance(event, tuple)]
    flag_calls = [
        params
        for statement, params in set_calls
        if statement == "SELECT set_config(:setting_name, :setting_value, false)"
        and isinstance(params, dict)
        and params.get("setting_name") == rls.RLS_ENABLED_SETTING
    ]
    assert flag_calls
    assert flag_calls[-1]["setting_value"] == "on"


def test_set_rls_session_context_resets_rls_enabled_feature_flag_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSession(dialect_name="postgresql")
    monkeypatch.delenv("TENANT_ISOLATION_RLS_ENABLED", raising=False)

    rls.set_rls_session_context(db, tenant_id=11, user_id=22, actor_type="user")

    statements = [event[0] for event in db.events if isinstance(event, tuple)]
    assert f"RESET {rls.RLS_ENABLED_SETTING}" in statements


def test_set_task_worker_rls_context_uses_server_owned_task_row(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeSession(dialect_name="postgresql")

    execute_calls = []

    def _execute(statement, params=None):
        execute_calls.append((str(statement), params))
        return _TaskLookupResult((44, 7))

    db.execute = _execute  # type: ignore[method-assign]

    captured = {}

    def _capture_setter(session, *, tenant_id, user_id, actor_type):
        captured["session"] = session
        captured["tenant_id"] = tenant_id
        captured["user_id"] = user_id
        captured["actor_type"] = actor_type

    monkeypatch.setattr(rls, "set_rls_session_context", _capture_setter)

    rls.set_task_worker_rls_context(db, task_id=900, user_id=77)

    assert execute_calls, "task ownership lookup should query server-owned task row"
    assert captured == {
        "session": db,
        "tenant_id": 44,
        "user_id": 77,
        "actor_type": "system",
    }


def test_set_task_worker_rls_context_bootstraps_lookup_with_privileged_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSession(dialect_name="postgresql")
    bypass_events: list[str] = []

    class _BypassContext:
        def __enter__(self):
            bypass_events.append("enter")

        def __exit__(self, exc_type, exc, tb):
            bypass_events.append("exit")
            return False

    monkeypatch.setattr(rls, "privileged_rls_bypass", lambda *_args, **_kwargs: _BypassContext())
    monkeypatch.setattr(rls, "set_rls_session_context", lambda *_args, **_kwargs: None)

    def _execute(_statement, _params=None):
        return _TaskLookupResult((99, 101))

    db.execute = _execute  # type: ignore[method-assign]

    rls.set_task_worker_rls_context(db, task_id=55, user_id=101)
    assert bypass_events == ["enter", "exit"]


def test_privileged_rls_bypass_sets_and_resets_postgres_bypass_flag() -> None:
    db = _FakeSession(dialect_name="postgresql")

    with rls.privileged_rls_bypass(db, scope="maintenance", actor_type="system"):
        pass

    set_calls = [event for event in db.events if isinstance(event, tuple)]
    bypass_set = [
        params
        for statement, params in set_calls
        if statement == "SELECT set_config(:setting_name, :setting_value, false)"
        and isinstance(params, dict)
        and params.get("setting_name") == rls.RLS_BYPASS_SETTING
    ]
    assert bypass_set
    assert bypass_set[-1]["setting_value"] == "on"

    statements = [event[0] for event in set_calls]
    assert f"RESET {rls.RLS_BYPASS_SETTING}" in statements


def test_privileged_rls_bypass_rejects_unknown_scope() -> None:
    db = _FakeSession(dialect_name="postgresql")

    with pytest.raises(ValueError, match="Unsupported privileged RLS scope"):
        with rls.privileged_rls_bypass(db, scope="request", actor_type="user"):
            pass


def test_get_db_clears_rls_context_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeSession(dialect_name="postgresql")
    clear_calls: list[object] = []

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(rls, "clear_rls_session_context", lambda session: clear_calls.append(session))

    dependency = database.get_db()
    yielded = next(dependency)
    assert yielded is db

    with pytest.raises(StopIteration):
        next(dependency)

    assert clear_calls == [db]
    assert db.events[-1] == "close"


def test_get_db_clears_rls_context_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeSession(dialect_name="postgresql")
    clear_calls: list[object] = []

    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(rls, "clear_rls_session_context", lambda session: clear_calls.append(session))

    dependency = database.get_db()
    _ = next(dependency)

    with pytest.raises(RuntimeError, match="boom"):
        dependency.throw(RuntimeError("boom"))

    assert "rollback" in db.events
    assert clear_calls == [db]
    assert db.events[-1] == "close"


def test_get_db_cleanup_runs_for_each_session(monkeypatch: pytest.MonkeyPatch) -> None:
    db_a = _FakeSession(dialect_name="postgresql")
    db_b = _FakeSession(dialect_name="postgresql")
    sessions = iter([db_a, db_b])
    clear_calls: list[object] = []

    monkeypatch.setattr(database, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(rls, "clear_rls_session_context", lambda session: clear_calls.append(session))

    dep_a = database.get_db()
    _ = next(dep_a)
    with pytest.raises(StopIteration):
        next(dep_a)

    dep_b = database.get_db()
    _ = next(dep_b)
    with pytest.raises(StopIteration):
        next(dep_b)

    assert clear_calls == [db_a, db_b]


def test_privileged_rls_bypass_not_used_in_http_or_websocket_auth_paths() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    request_entrypoints = (
        repo_root / "backend" / "auth.py",
        repo_root / "backend" / "services" / "tenant" / "dependencies.py",
        repo_root / "backend" / "services" / "websocket" / "gateway.py",
    )
    for module_path in request_entrypoints:
        source = module_path.read_text(encoding="utf-8")
        assert "privileged_rls_bypass" not in source
