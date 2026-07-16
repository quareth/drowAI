"""Interactive PostgreSQL bootstrap for the local DrowAI launcher.

This module provisions only the local development login role, database, and
pgvector extension. It never stores PostgreSQL administrator credentials and
requires explicit confirmation before applying administrative changes.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

ADMIN_DATABASE_URL_ENV = "DROWAI_POSTGRES_ADMIN_URL"
LOCAL_DATABASE_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


class LocalPostgresBootstrapError(RuntimeError):
    """Raised when local PostgreSQL cannot be safely bootstrapped."""


@dataclass(frozen=True, slots=True)
class PostgresTarget:
    """Application PostgreSQL identity derived from ``DATABASE_URL``."""

    database: str
    user: str
    password: str
    host: str
    port: int

    def connection_kwargs(self, *, database: str | None = None) -> dict[str, object]:
        """Return psycopg2 connection parameters without exposing the password."""

        return {
            "dbname": database or self.database,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
        }


@dataclass(frozen=True, slots=True)
class ApplicationDatabaseState:
    """Application-user connectivity and extension readiness."""

    reachable: bool
    vector_extension_present: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AdminAccess:
    """Transient PostgreSQL administrator connection parameters."""

    connection_kwargs: Mapping[str, object]
    display_user: str

    def for_database(self, database: str) -> dict[str, object]:
        """Return the same transient admin identity for another database."""

        kwargs = dict(self.connection_kwargs)
        kwargs["dbname"] = database
        return kwargs


@dataclass(frozen=True, slots=True)
class AdminDatabaseState:
    """Existing PostgreSQL resources visible to the administrator."""

    role_exists: bool
    database_owner: str | None
    vector_extension_present: bool


def ensure_local_postgres_ready(
    env: Mapping[str, str],
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Ensure local PostgreSQL resources exist, prompting before provisioning."""

    target = _target_from_env(env)
    application_state = _read_application_state(target)
    if application_state.reachable and application_state.vector_extension_present:
        return

    admin_access = _open_admin_access(
        env,
        target=target,
        interactive=interactive,
        input_fn=input_fn,
        getpass_fn=getpass_fn,
    )
    admin_state = _read_admin_state(admin_access, target)

    if admin_state.database_owner not in {None, target.user}:
        raise LocalPostgresBootstrapError(
            f"Database '{target.database}' already exists but is owned by "
            f"'{admin_state.database_owner}', not '{target.user}'."
        )

    actions = _required_actions(target, admin_state)
    if not actions:
        detail = application_state.error or "application credentials were rejected"
        raise LocalPostgresBootstrapError(
            "PostgreSQL resources already exist, but DrowAI cannot connect with the "
            f"configured DATABASE_URL ({detail}). Set DATABASE_URL to the existing "
            "role credentials instead of changing the role automatically."
        )

    output_fn("[local-cloud] PostgreSQL bootstrap is required:")
    for action in actions:
        output_fn(f"  - {action}")
    output_fn(f"[local-cloud] administrator: {admin_access.display_user}")

    if not interactive:
        raise LocalPostgresBootstrapError(
            "Interactive confirmation is required. Run "
            "`python scripts/local_dev.py bootstrap-db` in a terminal."
        )

    confirmation = input_fn("Create these local PostgreSQL resources now? [y/N] ").strip().lower()
    if confirmation not in {"y", "yes"}:
        raise LocalPostgresBootstrapError("PostgreSQL bootstrap was cancelled.")

    _apply_bootstrap(admin_access, target, admin_state)
    verified = _read_application_state(target)
    if not verified.reachable or not verified.vector_extension_present:
        detail = verified.error or "pgvector is not available"
        raise LocalPostgresBootstrapError(
            f"PostgreSQL resources were created, but application verification failed: {detail}"
        )
    output_fn("[local-cloud] PostgreSQL role, database, and pgvector extension are ready")


def _target_from_env(env: Mapping[str, str]) -> PostgresTarget:
    raw_url = str(env.get("DATABASE_URL") or "").strip()
    if not raw_url:
        raise LocalPostgresBootstrapError("DATABASE_URL is required for local PostgreSQL bootstrap.")
    try:
        parsed = make_url(raw_url.replace("postgres://", "postgresql://", 1))
    except Exception as exc:
        raise LocalPostgresBootstrapError("DATABASE_URL is not a valid database URL.") from exc
    if parsed.get_backend_name() != "postgresql":
        raise LocalPostgresBootstrapError("Local bootstrap supports PostgreSQL DATABASE_URL values only.")

    database = str(parsed.database or "").strip()
    user = str(parsed.username or "").strip()
    password = str(parsed.password or "")
    host = str(parsed.host or "localhost").strip()
    port = int(parsed.port or 5432)
    if not database or not user or not password:
        raise LocalPostgresBootstrapError(
            "DATABASE_URL must include a database, login role, and password."
        )
    return PostgresTarget(
        database=database,
        user=user,
        password=password,
        host=host,
        port=port,
    )


def _read_application_state(target: PostgresTarget) -> ApplicationDatabaseState:
    try:
        connection = psycopg2.connect(**target.connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                extension_present = bool(cursor.fetchone()[0])
        finally:
            connection.close()
        return ApplicationDatabaseState(
            reachable=True,
            vector_extension_present=extension_present,
        )
    except psycopg2.Error as exc:
        return ApplicationDatabaseState(
            reachable=False,
            vector_extension_present=False,
            error=_safe_database_error(exc),
        )


def _open_admin_access(
    env: Mapping[str, str],
    *,
    target: PostgresTarget,
    interactive: bool,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
) -> AdminAccess:
    explicit_url = str(env.get(ADMIN_DATABASE_URL_ENV) or "").strip()
    if explicit_url:
        access = _admin_access_from_url(explicit_url)
        _verify_admin_connection(access)
        return access

    if target.host not in LOCAL_DATABASE_HOSTS:
        raise LocalPostgresBootstrapError(
            "Automatic bootstrap is limited to local PostgreSQL. Provision the remote "
            "database manually or set DROWAI_POSTGRES_ADMIN_URL for this one-time command."
        )

    socket_access = AdminAccess(
        connection_kwargs={"dbname": "postgres", "port": target.port},
        display_user="current local PostgreSQL administrator",
    )
    try:
        return _verified_admin_access(socket_access)
    except LocalPostgresBootstrapError:
        if not interactive:
            raise LocalPostgresBootstrapError(
                "Could not connect through the local PostgreSQL administrator socket. "
                "Run bootstrap-db interactively or set DROWAI_POSTGRES_ADMIN_URL."
            )

    admin_user = input_fn("PostgreSQL administrator username [postgres]: ").strip() or "postgres"
    admin_password = getpass_fn(f"Password for PostgreSQL administrator '{admin_user}': ")
    prompted_access = AdminAccess(
        connection_kwargs={
            "dbname": "postgres",
            "user": admin_user,
            "password": admin_password,
            "host": target.host,
            "port": target.port,
        },
        display_user=admin_user,
    )
    return _verified_admin_access(prompted_access)


def _admin_access_from_url(raw_url: str) -> AdminAccess:
    try:
        parsed = make_url(raw_url.replace("postgres://", "postgresql://", 1))
    except Exception as exc:
        raise LocalPostgresBootstrapError(
            f"{ADMIN_DATABASE_URL_ENV} is not a valid database URL."
        ) from exc
    if parsed.get_backend_name() != "postgresql":
        raise LocalPostgresBootstrapError(f"{ADMIN_DATABASE_URL_ENV} must use PostgreSQL.")
    user = str(parsed.username or "").strip()
    if not user:
        raise LocalPostgresBootstrapError(f"{ADMIN_DATABASE_URL_ENV} must include an admin user.")
    kwargs: dict[str, object] = {
        "dbname": str(parsed.database or "postgres"),
        "user": user,
        "host": str(parsed.host or "localhost"),
        "port": int(parsed.port or 5432),
    }
    if parsed.password is not None:
        kwargs["password"] = str(parsed.password)
    return AdminAccess(connection_kwargs=kwargs, display_user=user)


def _verified_admin_access(access: AdminAccess) -> AdminAccess:
    _verify_admin_connection(access)
    return access


def _verify_admin_connection(access: AdminAccess) -> None:
    try:
        connection = psycopg2.connect(**dict(access.connection_kwargs))
        connection.close()
    except psycopg2.Error as exc:
        raise LocalPostgresBootstrapError(
            f"Could not connect as PostgreSQL administrator: {_safe_database_error(exc)}"
        ) from exc


def _read_admin_state(access: AdminAccess, target: PostgresTarget) -> AdminDatabaseState:
    try:
        connection = psycopg2.connect(**dict(access.connection_kwargs))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    (target.user,),
                )
                role_exists = bool(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT pg_catalog.pg_get_userbyid(datdba) "
                    "FROM pg_database WHERE datname = %s",
                    (target.database,),
                )
                owner_row = cursor.fetchone()
        finally:
            connection.close()

        database_owner = str(owner_row[0]) if owner_row else None
        extension_present = False
        if database_owner is not None:
            target_connection = psycopg2.connect(**access.for_database(target.database))
            try:
                with target_connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                    )
                    extension_present = bool(cursor.fetchone()[0])
            finally:
                target_connection.close()
    except psycopg2.Error as exc:
        raise LocalPostgresBootstrapError(
            f"Could not inspect PostgreSQL bootstrap state: {_safe_database_error(exc)}"
        ) from exc
    return AdminDatabaseState(
        role_exists=role_exists,
        database_owner=database_owner,
        vector_extension_present=extension_present,
    )


def _required_actions(target: PostgresTarget, state: AdminDatabaseState) -> list[str]:
    actions: list[str] = []
    if not state.role_exists:
        actions.append(f"create login role '{target.user}'")
    if state.database_owner is None:
        actions.append(f"create database '{target.database}' owned by '{target.user}'")
    if not state.vector_extension_present:
        actions.append(f"enable pgvector in database '{target.database}'")
    return actions


def _apply_bootstrap(
    access: AdminAccess,
    target: PostgresTarget,
    state: AdminDatabaseState,
) -> None:
    connection = psycopg2.connect(**dict(access.connection_kwargs))
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            if not state.role_exists:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(target.user)),
                    (target.password,),
                )
            if state.database_owner is None:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(target.database),
                        sql.Identifier(target.user),
                    )
                )
    except psycopg2.Error as exc:
        raise LocalPostgresBootstrapError(
            f"Could not create PostgreSQL role or database: {_safe_database_error(exc)}"
        ) from exc
    finally:
        connection.close()

    target_connection = psycopg2.connect(**access.for_database(target.database))
    target_connection.autocommit = True
    try:
        with target_connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg2.Error as exc:
        raise LocalPostgresBootstrapError(
            "Could not enable pgvector. Install the PostgreSQL pgvector extension "
            f"and retry: {_safe_database_error(exc)}"
        ) from exc
    finally:
        target_connection.close()


def _safe_database_error(exc: BaseException) -> str:
    """Return one concise database error line without connection credentials."""

    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message.replace("\n", " ")


__all__ = [
    "ADMIN_DATABASE_URL_ENV",
    "LocalPostgresBootstrapError",
    "ensure_local_postgres_ready",
]
