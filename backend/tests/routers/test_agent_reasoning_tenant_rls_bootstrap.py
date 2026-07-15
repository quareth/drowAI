"""Router-level RLS bootstrap tests for reasoning entry points.

These tests guard that manual reasoning authorization paths set user-lookup and
active-tenant RLS context deterministically before tenant-owned queries.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.routers import agent_reasoning
from backend.services.tenant.context import TenantRequestContext


def test_authorize_task_action_sets_user_lookup_and_tenant_rls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(headers={}, cookies={})
    db = SimpleNamespace()
    user = SimpleNamespace(id=17, username="owner")
    tenant_context = TenantRequestContext(
        tenant_id=301,
        user_id=17,
        role="owner",
        membership_id=12,
        is_default_tenant=False,
        source="header",
    )
    events: list[tuple[str, int, int]] = []

    monkeypatch.setattr(
        agent_reasoning,
        "_get_user_from_request",
        lambda _request, _db: (user, {"sub": "owner", "active_tenant_id": 301}),
    )
    monkeypatch.setattr(
        agent_reasoning,
        "_resolve_tenant_context_for_request",
        lambda **_kwargs: tenant_context,
    )
    monkeypatch.setattr(agent_reasoning, "_enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(agent_reasoning, "get_task_in_tenant_or_404", lambda **_kwargs: object())
    monkeypatch.setattr(
        agent_reasoning,
        "set_user_lookup_rls_context",
        lambda _db, *, user_id, actor_type: events.append(("lookup", int(user_id), 0)),
    )
    monkeypatch.setattr(
        agent_reasoning,
        "set_tenant_rls_context",
        lambda _db, *, tenant_id, user_id, actor_type: events.append(
            ("tenant", int(user_id), int(tenant_id))
        ),
    )

    resolved_user, resolved_context = agent_reasoning._authorize_task_action(
        task_id=91,
        request=request,
        db=db,
        action="stream.subscribe",
    )

    assert resolved_user is user
    assert resolved_context == tenant_context
    assert events == [("lookup", 17, 0), ("tenant", 17, 301)]


def test_close_short_lived_session_clears_rls_context_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        def __init__(self) -> None:
            self.events: list[str] = []

        def close(self) -> None:
            self.events.append("close")

    db = _FakeSession()
    monkeypatch.setattr(
        agent_reasoning,
        "clear_rls_session_context",
        lambda session: session.events.append("clear"),
    )

    agent_reasoning._close_short_lived_session(db)

    assert db.events == ["clear", "close"]
