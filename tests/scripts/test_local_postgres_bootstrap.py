"""Tests for the explicit local PostgreSQL bootstrap workflow."""

from __future__ import annotations

import pytest

from scripts import local_postgres_bootstrap as bootstrap


def test_target_from_env_reads_generated_postgres_identity() -> None:
    target = bootstrap._target_from_env(
        {
            "DATABASE_URL": (
                "postgresql+psycopg2://drowai_user:encoded%2Fpassword@localhost:5433/drowai"
            )
        }
    )

    assert target.database == "drowai"
    assert target.user == "drowai_user"
    assert target.password == "encoded/password"
    assert target.host == "localhost"
    assert target.port == 5433


def test_ready_database_does_not_request_admin_access(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_read_application_state",
        lambda _target: bootstrap.ApplicationDatabaseState(True, True),
    )
    monkeypatch.setattr(
        bootstrap,
        "_open_admin_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ready database must not request administrator access")
        ),
    )

    bootstrap.ensure_local_postgres_ready(
        {"DATABASE_URL": "postgresql://drowai_user:secret@localhost:5432/drowai"},
        interactive=False,
    )


def test_interactive_bootstrap_applies_only_after_confirmation(monkeypatch) -> None:
    application_states = iter(
        [
            bootstrap.ApplicationDatabaseState(False, False, "role does not exist"),
            bootstrap.ApplicationDatabaseState(True, True),
        ]
    )
    admin_access = bootstrap.AdminAccess(
        connection_kwargs={"dbname": "postgres"},
        display_user="local-admin",
    )
    admin_state = bootstrap.AdminDatabaseState(False, None, False)
    applied: list[tuple[bootstrap.PostgresTarget, bootstrap.AdminDatabaseState]] = []
    output: list[str] = []

    monkeypatch.setattr(bootstrap, "_read_application_state", lambda _target: next(application_states))
    monkeypatch.setattr(bootstrap, "_open_admin_access", lambda *_args, **_kwargs: admin_access)
    monkeypatch.setattr(bootstrap, "_read_admin_state", lambda _access, _target: admin_state)
    monkeypatch.setattr(
        bootstrap,
        "_apply_bootstrap",
        lambda _access, target, state: applied.append((target, state)),
    )

    bootstrap.ensure_local_postgres_ready(
        {"DATABASE_URL": "postgresql://drowai_user:secret@localhost:5432/drowai"},
        interactive=True,
        input_fn=lambda _prompt: "yes",
        output_fn=output.append,
    )

    assert len(applied) == 1
    assert applied[0][0].user == "drowai_user"
    assert any("create login role 'drowai_user'" in line for line in output)
    assert any("pgvector extension are ready" in line for line in output)


def test_noninteractive_bootstrap_refuses_administrative_changes(monkeypatch) -> None:
    admin_access = bootstrap.AdminAccess(
        connection_kwargs={"dbname": "postgres"},
        display_user="local-admin",
    )
    monkeypatch.setattr(
        bootstrap,
        "_read_application_state",
        lambda _target: bootstrap.ApplicationDatabaseState(False, False, "role does not exist"),
    )
    monkeypatch.setattr(bootstrap, "_open_admin_access", lambda *_args, **_kwargs: admin_access)
    monkeypatch.setattr(
        bootstrap,
        "_read_admin_state",
        lambda _access, _target: bootstrap.AdminDatabaseState(False, None, False),
    )
    monkeypatch.setattr(
        bootstrap,
        "_apply_bootstrap",
        lambda *_args: (_ for _ in ()).throw(AssertionError("bootstrap must not run")),
    )

    with pytest.raises(bootstrap.LocalPostgresBootstrapError, match="Interactive confirmation"):
        bootstrap.ensure_local_postgres_ready(
            {"DATABASE_URL": "postgresql://drowai_user:secret@localhost:5432/drowai"},
            interactive=False,
            output_fn=lambda _message: None,
        )


def test_existing_resources_with_wrong_credentials_are_not_modified(monkeypatch) -> None:
    admin_access = bootstrap.AdminAccess(
        connection_kwargs={"dbname": "postgres"},
        display_user="local-admin",
    )
    monkeypatch.setattr(
        bootstrap,
        "_read_application_state",
        lambda _target: bootstrap.ApplicationDatabaseState(False, False, "password rejected"),
    )
    monkeypatch.setattr(bootstrap, "_open_admin_access", lambda *_args, **_kwargs: admin_access)
    monkeypatch.setattr(
        bootstrap,
        "_read_admin_state",
        lambda _access, _target: bootstrap.AdminDatabaseState(True, "drowai_user", True),
    )

    with pytest.raises(bootstrap.LocalPostgresBootstrapError, match="Set DATABASE_URL"):
        bootstrap.ensure_local_postgres_ready(
            {"DATABASE_URL": "postgresql://drowai_user:wrong@localhost:5432/drowai"},
            interactive=True,
            input_fn=lambda _prompt: "yes",
            output_fn=lambda _message: None,
        )
