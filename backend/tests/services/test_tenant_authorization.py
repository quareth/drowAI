"""Tests for Tenant Isolation centralized tenant authorization policy decisions."""

from __future__ import annotations

import pytest

from backend.services.tenant import authorization


EXPECTED_ALLOWED_ACTIONS: dict[str, set[str]] = {
    authorization.ROLE_OWNER: set(authorization.ROLE_ACTIONS[authorization.ROLE_OWNER]),
    authorization.ROLE_ADMIN: set(authorization.ROLE_ACTIONS[authorization.ROLE_ADMIN]),
    authorization.ROLE_OPERATOR: set(authorization.ROLE_ACTIONS[authorization.ROLE_OPERATOR]),
    authorization.ROLE_VIEWER: set(authorization.ROLE_ACTIONS[authorization.ROLE_VIEWER]),
}


def test_owner_and_admin_can_manage_tenant_memberships_and_runners() -> None:
    for role in (authorization.ROLE_OWNER, authorization.ROLE_ADMIN):
        assert authorization.is_action_allowed(
            role=role,
            action=authorization.ACTION_TENANT_MEMBERSHIP_MANAGE,
        )
        assert authorization.is_action_allowed(
            role=role,
            action=authorization.ACTION_RUNNER_MANAGE,
        )


def test_operator_denies_task_delete_and_archive() -> None:
    assert not authorization.is_action_allowed(
        role=authorization.ROLE_OPERATOR,
        action=authorization.ACTION_TASK_DELETE,
    )
    assert not authorization.is_action_allowed(
        role=authorization.ROLE_OPERATOR,
        action=authorization.ACTION_TASK_ARCHIVE,
    )


def test_viewer_denies_usage_read_and_export() -> None:
    assert not authorization.is_action_allowed(
        role=authorization.ROLE_VIEWER,
        action=authorization.ACTION_USAGE_READ,
    )
    assert not authorization.is_action_allowed(
        role=authorization.ROLE_VIEWER,
        action=authorization.ACTION_USAGE_EXPORT,
    )


def test_unknown_actions_and_roles_fail_closed() -> None:
    unknown_action = "task.unknown"
    unknown_role = "member"

    action_decision = authorization.decide_action(role=authorization.ROLE_OWNER, action=unknown_action)
    role_decision = authorization.decide_action(role=unknown_role, action=authorization.ACTION_TASK_READ)

    assert action_decision.allowed is False
    assert action_decision.reason == "unknown_action"
    assert role_decision.allowed is False
    assert role_decision.reason == "unknown_role"
    assert not authorization.is_action_allowed(role=unknown_role, action=authorization.ACTION_TASK_READ)


@pytest.mark.parametrize(
    ("role", "action"),
    [
        (role, action)
        for role in (
            authorization.ROLE_OWNER,
            authorization.ROLE_ADMIN,
            authorization.ROLE_OPERATOR,
            authorization.ROLE_VIEWER,
        )
        for action in authorization.KNOWN_ACTIONS
    ],
)
def test_mvp_matrix_covers_every_role_and_action_combination(role: str, action: str) -> None:
    expected_allowed = action in EXPECTED_ALLOWED_ACTIONS[role]

    decision = authorization.decide_action(role=role, action=action)

    assert decision.policy_version == authorization.POLICY_VERSION
    assert decision.allowed is expected_allowed
    assert decision.reason == ("allowed" if expected_allowed else "forbidden")
    assert authorization.is_action_allowed(role=role, action=action) is expected_allowed
