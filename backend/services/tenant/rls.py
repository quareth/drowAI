"""Tenant-scoped PostgreSQL RLS session context helpers.

Responsibilities:
- Set and clear request/worker-scoped PostgreSQL session variables for RLS.
- Provide no-op behavior on non-PostgreSQL dialects for SQLite/local parity.
- Derive worker tenant context from server-owned task rows when needed.
- Provide explicit privileged bypass context for trusted migration/maintenance
  jobs outside user HTTP/WS request paths.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

CURRENT_TENANT_ID_SETTING = "app.current_tenant_id"
CURRENT_USER_ID_SETTING = "app.current_user_id"
CURRENT_ACTOR_TYPE_SETTING = "app.current_actor_type"
RLS_BYPASS_SETTING = "app.rls_bypass"
RLS_ENABLED_SETTING = "app.rls_enabled"

_PRIVILEGED_BYPASS_SCOPES = frozenset({"migration", "repair", "maintenance"})

_RLS_SETTINGS = (
    CURRENT_TENANT_ID_SETTING,
    CURRENT_USER_ID_SETTING,
    CURRENT_ACTOR_TYPE_SETTING,
    RLS_BYPASS_SETTING,
    RLS_ENABLED_SETTING,
)


def _resolve_bind(db_or_connection: Any) -> Any:
    if hasattr(db_or_connection, "get_bind"):
        return db_or_connection.get_bind()
    return db_or_connection


def _is_postgresql_session(db_or_connection: Any) -> bool:
    bind = _resolve_bind(db_or_connection)
    dialect = getattr(bind, "dialect", None)
    return str(getattr(dialect, "name", "")).lower() == "postgresql"


def _reset_setting(db: Any, setting_name: str) -> None:
    db.execute(text(f"RESET {setting_name}"))


def _set_setting(db: Any, setting_name: str, setting_value: str) -> None:
    db.execute(
        text("SELECT set_config(:setting_name, :setting_value, false)"),
        {
            "setting_name": setting_name,
            "setting_value": setting_value,
        },
    )


def _rollback_if_supported(db: Any) -> None:
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        rollback()


def _reset_privileged_bypass_settings(db: Any) -> None:
    _reset_setting(db, RLS_BYPASS_SETTING)
    _reset_setting(db, CURRENT_ACTOR_TYPE_SETTING)


def _is_tenant_isolation_rls_enabled() -> bool:
    return str(os.getenv("TENANT_ISOLATION_RLS_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _set_rls_rollout_setting(db: Any) -> None:
    if _is_tenant_isolation_rls_enabled():
        _set_setting(db, RLS_ENABLED_SETTING, "on")
        return
    _reset_setting(db, RLS_ENABLED_SETTING)


def clear_rls_session_context(db: Session) -> None:
    """Clear all tenant RLS settings on the current DB session."""

    if not _is_postgresql_session(db):
        return

    for setting_name in _RLS_SETTINGS:
        _reset_setting(db, setting_name)


def set_rls_session_context(
    db: Session,
    *,
    tenant_id: int | None,
    user_id: int | None,
    actor_type: str | None,
) -> None:
    """Set tenant RLS settings on the current DB session when PostgreSQL is active."""

    if not _is_postgresql_session(db):
        return

    _set_rls_rollout_setting(db)

    if tenant_id is None:
        _reset_setting(db, CURRENT_TENANT_ID_SETTING)
    else:
        _set_setting(db, CURRENT_TENANT_ID_SETTING, str(int(tenant_id)))

    if user_id is None:
        _reset_setting(db, CURRENT_USER_ID_SETTING)
    else:
        _set_setting(db, CURRENT_USER_ID_SETTING, str(int(user_id)))

    normalized_actor_type = str(actor_type).strip() if actor_type is not None else ""
    if not normalized_actor_type:
        _reset_setting(db, CURRENT_ACTOR_TYPE_SETTING)
    else:
        _set_setting(db, CURRENT_ACTOR_TYPE_SETTING, normalized_actor_type)


def set_user_lookup_rls_context(
    db: Session,
    *,
    user_id: int,
    actor_type: str = "user",
) -> None:
    """Set user-only context for pre-active-tenant membership discovery paths."""

    set_rls_session_context(
        db,
        tenant_id=None,
        user_id=int(user_id),
        actor_type=actor_type,
    )


def set_tenant_rls_context(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    actor_type: str = "user",
) -> None:
    """Set tenant + user context for tenant-owned query paths."""

    set_rls_session_context(
        db,
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        actor_type=actor_type,
    )


def set_task_worker_rls_context(
    db: Session,
    *,
    task_id: int,
    actor_type: str = "system",
    user_id: int | None = None,
) -> None:
    """Set worker RLS context by resolving tenant ownership from a task row."""

    if not _is_postgresql_session(db):
        return

    from backend.models import Task

    with privileged_rls_bypass(db, scope="maintenance", actor_type="system"):
        row: Any = db.execute(
            select(Task.tenant_id, Task.user_id).where(Task.id == int(task_id))
        ).one_or_none()
    if row is None:
        raise ValueError(f"Task worker RLS context requires an existing task (task_id={task_id}).")

    tenant_id = row[0]
    task_user_id = row[1]
    if tenant_id is None:
        raise ValueError(
            f"Task worker RLS context requires task.tenant_id (task_id={task_id})."
        )

    resolved_user_id = int(user_id) if user_id is not None else (
        int(task_user_id) if task_user_id is not None else None
    )
    set_rls_session_context(
        db,
        tenant_id=int(tenant_id),
        user_id=resolved_user_id,
        actor_type=actor_type,
    )


@contextmanager
def privileged_rls_bypass(
    db: Any,
    *,
    scope: str,
    actor_type: str = "system",
):
    """Enable temporary RLS bypass for trusted migration/maintenance contexts.

    This helper is intentionally scoped to non-request paths such as Alembic
    migrations, startup bootstrap/repair flows, and background maintenance jobs.
    """

    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in _PRIVILEGED_BYPASS_SCOPES:
        allowed = ", ".join(sorted(_PRIVILEGED_BYPASS_SCOPES))
        raise ValueError(
            f"Unsupported privileged RLS scope `{scope}`. Allowed scopes: {allowed}."
        )

    if not _is_postgresql_session(db):
        yield
        return

    normalized_actor_type = str(actor_type or "").strip() or "system"

    set_rls_session_context(
        db,
        tenant_id=None,
        user_id=None,
        actor_type=normalized_actor_type,
    )
    _set_setting(db, RLS_BYPASS_SETTING, "on")
    try:
        yield
    except BaseException:
        try:
            _reset_privileged_bypass_settings(db)
        except Exception:
            _rollback_if_supported(db)
            try:
                _reset_privileged_bypass_settings(db)
            except Exception:
                pass
        raise
    else:
        _reset_privileged_bypass_settings(db)


__all__ = [
    "CURRENT_ACTOR_TYPE_SETTING",
    "CURRENT_TENANT_ID_SETTING",
    "CURRENT_USER_ID_SETTING",
    "RLS_ENABLED_SETTING",
    "RLS_BYPASS_SETTING",
    "clear_rls_session_context",
    "privileged_rls_bypass",
    "set_rls_session_context",
    "set_task_worker_rls_context",
    "set_tenant_rls_context",
    "set_user_lookup_rls_context",
]
