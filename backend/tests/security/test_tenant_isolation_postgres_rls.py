"""PostgreSQL tenant_isolation RLS boundary integration tests.

This module validates database-enforced tenant isolation behavior under active
RLS rollout for tenant-owned tables and pre-active-tenant membership lookup.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.membership_service import TenantMembershipService, TenantMembershipServiceError
from backend.services.tenant.rls import (
    privileged_rls_bypass,
    set_tenant_rls_context,
    set_user_lookup_rls_context,
)


def _postgres_database_url() -> str | None:
    for key in ("BACKEND_TEST_DATABASE_URL", "TEST_DATABASE_URL", "DATABASE_URL"):
        raw = os.getenv(key)
        if raw:
            normalized = raw.replace("postgres://", "postgresql://", 1)
            if normalized.startswith("postgresql://") or normalized.startswith("postgresql+"):
                return normalized
    return None


@pytest.fixture
def postgres_db_session() -> Session:
    database_url = _postgres_database_url()
    if not database_url:
        pytest.skip("PostgreSQL DATABASE_URL/TEST_DATABASE_URL is not configured")

    engine = create_engine(database_url, future=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("tenant_isolation PostgreSQL RLS tests require a PostgreSQL database")

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, autocommit=False)

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


def _assert_tenant_isolation_policies_present(db: Session) -> None:
    tasks_policy_count = db.execute(
        text(
            """
            SELECT COUNT(*)
            FROM pg_policies
            WHERE schemaname = current_schema()
              AND tablename = 'tasks'
              AND policyname = 'tenant_isolation_tasks_scope'
            """
        )
    ).scalar_one()
    memberships_policy_count = db.execute(
        text(
            """
            SELECT COUNT(*)
            FROM pg_policies
            WHERE schemaname = current_schema()
              AND tablename = 'tenant_memberships'
              AND policyname = 'tenant_isolation_tenant_memberships_user_lookup_read'
            """
        )
    ).scalar_one()
    assert int(tasks_policy_count) > 0, "Missing `tenant_isolation_tasks_scope` policy on tasks"
    assert int(memberships_policy_count) > 0, (
        "Missing `tenant_isolation_tenant_memberships_user_lookup_read` policy on tenant_memberships"
    )


def _assert_tenant_isolation_forced_rls(db: Session) -> None:
    force_flags = db.execute(
        text(
            """
            SELECT c.relname, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname IN ('tasks', 'tenants', 'tenant_memberships')
            ORDER BY c.relname
            """
        )
    ).all()
    force_by_table = {str(row.relname): bool(row.relforcerowsecurity) for row in force_flags}
    assert force_by_table.get("tasks") is True
    assert force_by_table.get("tenants") is True
    assert force_by_table.get("tenant_memberships") is True


def _membership_id_for(*, db: Session, tenant_id: int, user_id: int) -> int:
    with privileged_rls_bypass(db, scope="maintenance", actor_type="system"):
        membership_id = db.execute(
            text(
                """
                SELECT id
                FROM tenant_memberships
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                """
            ),
            {"tenant_id": int(tenant_id), "user_id": int(user_id)},
        ).scalar_one()
    return int(membership_id)


def _seed_tenant_rls_fixture(db: Session) -> dict[str, int]:
    token = uuid4().hex[:8]
    with privileged_rls_bypass(db, scope="maintenance", actor_type="system"):
        user_a = db.execute(
            text(
                """
                INSERT INTO users (username, password, email, is_active)
                VALUES (:username, :password, :email, true)
                RETURNING id
                """
            ),
            {
                "username": f"tenant-isolation-rls-user-a-{token}",
                "password": "hashed",
                "email": f"tenant-isolation-rls-user-a-{token}@example.test",
            },
        ).scalar_one()
        user_b = db.execute(
            text(
                """
                INSERT INTO users (username, password, email, is_active)
                VALUES (:username, :password, :email, true)
                RETURNING id
                """
            ),
            {
                "username": f"tenant-isolation-rls-user-b-{token}",
                "password": "hashed",
                "email": f"tenant-isolation-rls-user-b-{token}@example.test",
            },
        ).scalar_one()
        user_c = db.execute(
            text(
                """
                INSERT INTO users (username, password, email, is_active)
                VALUES (:username, :password, :email, true)
                RETURNING id
                """
            ),
            {
                "username": f"tenant-isolation-rls-user-c-{token}",
                "password": "hashed",
                "email": f"tenant-isolation-rls-user-c-{token}@example.test",
            },
        ).scalar_one()

        tenant_a = db.execute(
            text(
                """
                INSERT INTO tenants (slug, name, status)
                VALUES (:slug, :name, 'active')
                RETURNING id
                """
            ),
            {"slug": f"tenant-isolation-rls-tenant-a-{token}", "name": "Tenant Isolation RLS Tenant A"},
        ).scalar_one()
        tenant_b = db.execute(
            text(
                """
                INSERT INTO tenants (slug, name, status)
                VALUES (:slug, :name, 'active')
                RETURNING id
                """
            ),
            {"slug": f"tenant-isolation-rls-tenant-b-{token}", "name": "Tenant Isolation RLS Tenant B"},
        ).scalar_one()
        tenant_c = db.execute(
            text(
                """
                INSERT INTO tenants (slug, name, status)
                VALUES (:slug, :name, 'active')
                RETURNING id
                """
            ),
            {"slug": f"tenant-isolation-rls-tenant-c-{token}", "name": "Tenant Isolation RLS Tenant C"},
        ).scalar_one()

        db.execute(
            text(
                """
                INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                VALUES
                    (:tenant_a, :user_a, 'owner', 'active'),
                    (:tenant_b, :user_a, 'admin', 'active'),
                    (:tenant_a, :user_b, 'viewer', 'active'),
                    (:tenant_b, :user_c, 'viewer', 'active'),
                    (:tenant_c, :user_b, 'owner', 'active')
                """
            ),
            {
                "tenant_a": int(tenant_a),
                "tenant_b": int(tenant_b),
                "tenant_c": int(tenant_c),
                "user_a": int(user_a),
                "user_b": int(user_b),
                "user_c": int(user_c),
            },
        )

        task_a = db.execute(
            text(
                """
                INSERT INTO tasks (tenant_id, user_id, name, status)
                VALUES (:tenant_id, :user_id, :name, 'created')
                RETURNING id
                """
            ),
            {
                "tenant_id": int(tenant_a),
                "user_id": int(user_a),
                "name": f"tenant-isolation-tenant-a-task-{token}",
            },
        ).scalar_one()
        task_b = db.execute(
            text(
                """
                INSERT INTO tasks (tenant_id, user_id, name, status)
                VALUES (:tenant_id, :user_id, :name, 'created')
                RETURNING id
                """
            ),
            {
                "tenant_id": int(tenant_b),
                "user_id": int(user_a),
                "name": f"tenant-isolation-tenant-b-task-{token}",
            },
        ).scalar_one()

    return {
        "user_a": int(user_a),
        "user_b": int(user_b),
        "user_c": int(user_c),
        "tenant_a": int(tenant_a),
        "tenant_b": int(tenant_b),
        "tenant_c": int(tenant_c),
        "task_a": int(task_a),
        "task_b": int(task_b),
    }


def test_tenant_isolation_rls_blocks_cross_tenant_task_reads(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    _assert_tenant_isolation_forced_rls(db)
    fixture = _seed_tenant_rls_fixture(db)

    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_a"],
        actor_type="user",
    )
    visible_task_a = db.execute(
        text("SELECT id FROM tasks WHERE id = :task_id"),
        {"task_id": fixture["task_a"]},
    ).scalar_one_or_none()
    hidden_task_b = db.execute(
        text("SELECT id FROM tasks WHERE id = :task_id"),
        {"task_id": fixture["task_b"]},
    ).scalar_one_or_none()

    assert visible_task_a == fixture["task_a"]
    assert hidden_task_b is None

    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_b"],
        user_id=fixture["user_a"],
        actor_type="user",
    )
    visible_task_b = db.execute(
        text("SELECT id FROM tasks WHERE id = :task_id"),
        {"task_id": fixture["task_b"]},
    ).scalar_one_or_none()
    hidden_task_a = db.execute(
        text("SELECT id FROM tasks WHERE id = :task_id"),
        {"task_id": fixture["task_a"]},
    ).scalar_one_or_none()

    assert visible_task_b == fixture["task_b"]
    assert hidden_task_a is None


def test_tenant_isolation_pre_tenant_membership_lookup_isolated_to_current_user(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    fixture = _seed_tenant_rls_fixture(db)

    set_user_lookup_rls_context(db, user_id=fixture["user_a"], actor_type="user")

    visible_memberships = db.execute(
        text("SELECT tenant_id, user_id FROM tenant_memberships ORDER BY tenant_id"),
    ).all()
    visible_tenants = db.execute(text("SELECT id FROM tenants ORDER BY id")).scalars().all()

    assert {(int(row.tenant_id), int(row.user_id)) for row in visible_memberships} == {
        (fixture["tenant_a"], fixture["user_a"]),
        (fixture["tenant_b"], fixture["user_a"]),
    }
    assert {int(tenant_id) for tenant_id in visible_tenants} == {
        fixture["tenant_a"],
        fixture["tenant_b"],
    }

    other_user_membership = db.execute(
        text(
            """
            SELECT id
            FROM tenant_memberships
            WHERE user_id = :other_user_id
            """
        ),
        {"other_user_id": fixture["user_b"]},
    ).scalars().all()
    assert other_user_membership == []


def test_tenant_isolation_owner_admin_membership_management_allowed_under_rls(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    fixture = _seed_tenant_rls_fixture(db)
    service = TenantMembershipService(db)

    owner_context = TenantRequestContext(
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_a"],
        role="owner",
        membership_id=_membership_id_for(db=db, tenant_id=fixture["tenant_a"], user_id=fixture["user_a"]),
        is_default_tenant=False,
        source="test",
    )
    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_a"],
        actor_type="tenant_owner",
    )
    tenant_a_memberships = service.list_tenant_memberships(
        actor_context=owner_context,
        tenant_id=fixture["tenant_a"],
    )
    assert {(item.tenant_id, item.user_id) for item in tenant_a_memberships} == {
        (fixture["tenant_a"], fixture["user_a"]),
        (fixture["tenant_a"], fixture["user_b"]),
    }

    tenant_a_viewer_membership_id = _membership_id_for(
        db=db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_b"],
    )
    updated_owner_target = service.change_membership_role(
        actor_context=owner_context,
        tenant_id=fixture["tenant_a"],
        membership_id=tenant_a_viewer_membership_id,
        new_role="operator",
    )
    assert updated_owner_target.role == "operator"

    admin_context = TenantRequestContext(
        tenant_id=fixture["tenant_b"],
        user_id=fixture["user_a"],
        role="admin",
        membership_id=_membership_id_for(db=db, tenant_id=fixture["tenant_b"], user_id=fixture["user_a"]),
        is_default_tenant=False,
        source="test",
    )
    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_b"],
        user_id=fixture["user_a"],
        actor_type="tenant_admin",
    )
    tenant_b_viewer_membership_id = _membership_id_for(
        db=db,
        tenant_id=fixture["tenant_b"],
        user_id=fixture["user_c"],
    )
    updated_admin_target = service.change_membership_role(
        actor_context=admin_context,
        tenant_id=fixture["tenant_b"],
        membership_id=tenant_b_viewer_membership_id,
        new_role="operator",
    )
    assert updated_admin_target.role == "operator"


def test_tenant_isolation_rls_denies_viewer_membership_management(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    fixture = _seed_tenant_rls_fixture(db)
    service = TenantMembershipService(db)

    viewer_context = TenantRequestContext(
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_b"],
        role="viewer",
        membership_id=_membership_id_for(db=db, tenant_id=fixture["tenant_a"], user_id=fixture["user_b"]),
        is_default_tenant=False,
        source="test",
    )
    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_b"],
        actor_type="tenant_viewer",
    )
    visible_count = db.execute(
        text("SELECT COUNT(*) FROM tenant_memberships WHERE tenant_id = :tenant_id"),
        {"tenant_id": fixture["tenant_a"]},
    ).scalar_one()
    assert int(visible_count) == 1

    owner_membership_id = _membership_id_for(
        db=db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_a"],
    )
    with pytest.raises(TenantMembershipServiceError) as exc_info:
        service.change_membership_role(
            actor_context=viewer_context,
            tenant_id=fixture["tenant_a"],
            membership_id=owner_membership_id,
            new_role="operator",
        )
    assert exc_info.value.error_code == "TENANT_MEMBERSHIP_FORBIDDEN"

    with db.begin_nested():
        with pytest.raises(Exception):
            db.execute(
                text(
                    """
                    UPDATE tenant_memberships
                    SET role = 'operator'
                    WHERE tenant_id = :tenant_id
                      AND user_id = :owner_user_id
                    """
                ),
                {
                    "tenant_id": fixture["tenant_a"],
                    "owner_user_id": fixture["user_a"],
                },
            )


def test_tenant_isolation_rls_denies_spoofed_actor_type_membership_management(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    fixture = _seed_tenant_rls_fixture(db)

    # User B is a viewer in tenant A; spoofed actor_type must not grant admin access.
    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_b"],
        actor_type="tenant_admin",
    )

    visible_memberships = db.execute(
        text(
            """
            SELECT user_id, role
            FROM tenant_memberships
            WHERE tenant_id = :tenant_id
            ORDER BY user_id
            """
        ),
        {"tenant_id": fixture["tenant_a"]},
    ).all()
    assert [(int(row.user_id), str(row.role)) for row in visible_memberships] == [
        (fixture["user_b"], "viewer")
    ]

    with db.begin_nested():
        with pytest.raises(Exception):
            db.execute(
                text(
                    """
                    UPDATE tenant_memberships
                    SET role = 'operator'
                    WHERE tenant_id = :tenant_id
                      AND user_id = :owner_user_id
                    """
                ),
                {
                    "tenant_id": fixture["tenant_a"],
                    "owner_user_id": fixture["user_a"],
                },
            )

    with db.begin_nested():
        with pytest.raises(Exception):
            db.execute(
                text(
                    """
                    INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                    VALUES (:tenant_id, :user_id, 'viewer', 'active')
                    """
                ),
                {
                    "tenant_id": fixture["tenant_a"],
                    "user_id": fixture["user_c"],
                },
            )


def test_tenant_isolation_rls_rejects_mismatched_tenant_inserts(
    postgres_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")
    db = postgres_db_session
    _assert_tenant_isolation_policies_present(db)
    fixture = _seed_tenant_rls_fixture(db)

    set_tenant_rls_context(
        db,
        tenant_id=fixture["tenant_a"],
        user_id=fixture["user_a"],
        actor_type="user",
    )

    with db.begin_nested():
        with pytest.raises(Exception):
            db.execute(
                text(
                    """
                    INSERT INTO tasks (tenant_id, user_id, name, status)
                    VALUES (:tenant_id, :user_id, :name, 'created')
                    """
                ),
                {
                    "tenant_id": fixture["tenant_b"],
                    "user_id": fixture["user_a"],
                    "name": f"tenant-isolation-mismatch-{uuid4().hex[:8]}",
                },
            )

    inserted = db.execute(
        text(
            """
            INSERT INTO tasks (tenant_id, user_id, name, status)
            VALUES (:tenant_id, :user_id, :name, 'created')
            RETURNING tenant_id
            """
        ),
        {
            "tenant_id": fixture["tenant_a"],
            "user_id": fixture["user_a"],
            "name": f"tenant-isolation-match-{uuid4().hex[:8]}",
        },
    ).scalar_one()

    assert int(inserted) == fixture["tenant_a"]
