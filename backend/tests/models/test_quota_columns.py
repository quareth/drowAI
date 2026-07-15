"""Validate ORM persistence for task concurrency quota and capacity columns.

This module verifies read/write round trips for the Phase 2 nullable quota
fields on tenant/user and capacity field on runner models.
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import User
from backend.models.runner_control import ExecutionSite, Runner
from backend.models.tenant import Tenant


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        bind=engine,
        tables=[Tenant.__table__, User.__table__, ExecutionSite.__table__, Runner.__table__],
    )
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_quota_columns_round_trip_explicit_values() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant = Tenant(
            slug="acme",
            name="Acme",
            max_concurrent_tasks=12,
            max_concurrent_tasks_per_user=4,
        )
        user = User(
            username="alice",
            password="hashed-password",
            email="alice@example.com",
            max_concurrent_tasks=3,
        )
        session.add_all([tenant, user])
        session.flush()

        site = ExecutionSite(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name="Site A",
            slug="site-a",
        )
        session.add(site)
        session.flush()

        runner = Runner(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            execution_site_id=site.id,
            name="Runner A",
            max_active_tasks=6,
        )
        session.add(runner)
        session.commit()

        session.expunge_all()
        loaded_tenant = session.get(Tenant, tenant.id)
        loaded_user = session.get(User, user.id)
        loaded_runner = session.get(Runner, runner.id)

        assert loaded_tenant is not None
        assert loaded_user is not None
        assert loaded_runner is not None
        assert loaded_tenant.max_concurrent_tasks == 12
        assert loaded_tenant.max_concurrent_tasks_per_user == 4
        assert loaded_user.max_concurrent_tasks == 3
        assert loaded_runner.max_active_tasks == 6

    engine.dispose()


def test_quota_columns_round_trip_nullable_values() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant = Tenant(
            slug="umbrella",
            name="Umbrella",
            max_concurrent_tasks=None,
            max_concurrent_tasks_per_user=None,
        )
        user = User(
            username="bob",
            password="hashed-password",
            email="bob@example.com",
            max_concurrent_tasks=None,
        )
        session.add_all([tenant, user])
        session.flush()

        site = ExecutionSite(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name="Site B",
            slug="site-b",
        )
        session.add(site)
        session.flush()

        runner = Runner(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            execution_site_id=site.id,
            name="Runner B",
            max_active_tasks=None,
        )
        session.add(runner)
        session.commit()

        session.expunge_all()
        loaded_tenant = session.get(Tenant, tenant.id)
        loaded_user = session.get(User, user.id)
        loaded_runner = session.get(Runner, runner.id)

        assert loaded_tenant is not None
        assert loaded_user is not None
        assert loaded_runner is not None
        assert loaded_tenant.max_concurrent_tasks is None
        assert loaded_tenant.max_concurrent_tasks_per_user is None
        assert loaded_user.max_concurrent_tasks is None
        assert loaded_runner.max_active_tasks is None

    engine.dispose()
