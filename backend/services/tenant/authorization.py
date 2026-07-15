"""Centralized Tenant Isolation tenant authorization policy.

Responsibilities:
- Define the MVP tenant action vocabulary and role matrix.
- Provide fail-closed role/action authorization helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

POLICY_VERSION = "tenant_isolation-v1"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

ACTION_TENANT_MEMBERSHIP_MANAGE = "tenant.membership.manage"
ACTION_TENANT_SETTINGS_MANAGE = "tenant.settings.manage"
ACTION_RUNNER_MANAGE = "runner.manage"
ACTION_TASK_CREATE = "task.create"
ACTION_TASK_READ = "task.read"
ACTION_TASK_UPDATE = "task.update"
ACTION_TASK_CONTROL = "task.control"
ACTION_TASK_DELETE = "task.delete"
ACTION_TASK_ARCHIVE = "task.archive"
ACTION_CHAT_READ = "chat.read"
ACTION_CHAT_WRITE = "chat.write"
ACTION_CHAT_CANCEL = "chat.cancel"
ACTION_CHAT_RETRY = "chat.retry"
ACTION_FILE_BROWSE = "file.browse"
ACTION_FILE_READ = "file.read"
ACTION_FILE_DOWNLOAD = "file.download"
ACTION_ARTIFACT_READ = "artifact.read"
ACTION_ARTIFACT_DOWNLOAD = "artifact.download"
ACTION_ARTIFACT_DELETE = "artifact.delete"
ACTION_KNOWLEDGE_READ = "knowledge.read"
ACTION_KNOWLEDGE_WRITE = "knowledge.write"
ACTION_KNOWLEDGE_REBUILD = "knowledge.rebuild"
ACTION_REPORT_READ = "report.read"
ACTION_REPORT_WRITE = "report.write"
ACTION_REPORT_DELETE = "report.delete"
ACTION_USAGE_READ = "usage.read"
ACTION_USAGE_EXPORT = "usage.export"
ACTION_STREAM_SUBSCRIBE = "stream.subscribe"
ACTION_STREAM_REPLAY = "stream.replay"

ROLE_ACTIONS: dict[str, tuple[str, ...]] = {
    ROLE_OWNER: (
        ACTION_TENANT_MEMBERSHIP_MANAGE,
        ACTION_TENANT_SETTINGS_MANAGE,
        ACTION_RUNNER_MANAGE,
        ACTION_TASK_CREATE,
        ACTION_TASK_READ,
        ACTION_TASK_UPDATE,
        ACTION_TASK_CONTROL,
        ACTION_TASK_DELETE,
        ACTION_TASK_ARCHIVE,
        ACTION_CHAT_READ,
        ACTION_CHAT_WRITE,
        ACTION_CHAT_CANCEL,
        ACTION_CHAT_RETRY,
        ACTION_FILE_BROWSE,
        ACTION_FILE_READ,
        ACTION_FILE_DOWNLOAD,
        ACTION_ARTIFACT_READ,
        ACTION_ARTIFACT_DOWNLOAD,
        ACTION_ARTIFACT_DELETE,
        ACTION_KNOWLEDGE_READ,
        ACTION_KNOWLEDGE_WRITE,
        ACTION_KNOWLEDGE_REBUILD,
        ACTION_REPORT_READ,
        ACTION_REPORT_WRITE,
        ACTION_REPORT_DELETE,
        ACTION_USAGE_READ,
        ACTION_USAGE_EXPORT,
        ACTION_STREAM_SUBSCRIBE,
        ACTION_STREAM_REPLAY,
    ),
    ROLE_ADMIN: (
        ACTION_TENANT_MEMBERSHIP_MANAGE,
        ACTION_TENANT_SETTINGS_MANAGE,
        ACTION_RUNNER_MANAGE,
        ACTION_TASK_CREATE,
        ACTION_TASK_READ,
        ACTION_TASK_UPDATE,
        ACTION_TASK_CONTROL,
        ACTION_TASK_DELETE,
        ACTION_TASK_ARCHIVE,
        ACTION_CHAT_READ,
        ACTION_CHAT_WRITE,
        ACTION_CHAT_CANCEL,
        ACTION_CHAT_RETRY,
        ACTION_FILE_BROWSE,
        ACTION_FILE_READ,
        ACTION_FILE_DOWNLOAD,
        ACTION_ARTIFACT_READ,
        ACTION_ARTIFACT_DOWNLOAD,
        ACTION_ARTIFACT_DELETE,
        ACTION_KNOWLEDGE_READ,
        ACTION_KNOWLEDGE_WRITE,
        ACTION_KNOWLEDGE_REBUILD,
        ACTION_REPORT_READ,
        ACTION_REPORT_WRITE,
        ACTION_REPORT_DELETE,
        ACTION_USAGE_READ,
        ACTION_USAGE_EXPORT,
        ACTION_STREAM_SUBSCRIBE,
        ACTION_STREAM_REPLAY,
    ),
    ROLE_OPERATOR: (
        ACTION_TASK_CREATE,
        ACTION_TASK_READ,
        ACTION_TASK_UPDATE,
        ACTION_TASK_CONTROL,
        ACTION_CHAT_READ,
        ACTION_CHAT_WRITE,
        ACTION_CHAT_CANCEL,
        ACTION_CHAT_RETRY,
        ACTION_FILE_BROWSE,
        ACTION_FILE_READ,
        ACTION_FILE_DOWNLOAD,
        ACTION_ARTIFACT_READ,
        ACTION_ARTIFACT_DOWNLOAD,
        ACTION_KNOWLEDGE_READ,
        ACTION_KNOWLEDGE_WRITE,
        ACTION_REPORT_READ,
        ACTION_REPORT_WRITE,
        ACTION_STREAM_SUBSCRIBE,
        ACTION_STREAM_REPLAY,
    ),
    ROLE_VIEWER: (
        ACTION_TASK_READ,
        ACTION_CHAT_READ,
        ACTION_FILE_BROWSE,
        ACTION_FILE_READ,
        ACTION_FILE_DOWNLOAD,
        ACTION_ARTIFACT_READ,
        ACTION_ARTIFACT_DOWNLOAD,
        ACTION_KNOWLEDGE_READ,
        ACTION_REPORT_READ,
        ACTION_STREAM_SUBSCRIBE,
    ),
}

KNOWN_ACTIONS: tuple[str, ...] = tuple(
    dict.fromkeys(action for actions in ROLE_ACTIONS.values() for action in actions)
)


@dataclass(frozen=True, slots=True)
class TenantAuthorizationDecision:
    """Authorization decision for one role/action check."""

    role: str
    action: str
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION


def _normalize_value(value: object) -> str:
    return str(value or "").strip().lower()


def allowed_actions_for_role(role: object) -> tuple[str, ...]:
    """Return ordered allowed actions for a role or empty for unsupported roles."""

    normalized_role = _normalize_value(role)
    return ROLE_ACTIONS.get(normalized_role, tuple())


def is_action_allowed(*, role: object, action: object) -> bool:
    """Return whether role can perform action under the MVP policy."""

    normalized_action = _normalize_value(action)
    if normalized_action not in KNOWN_ACTIONS:
        return False
    return normalized_action in allowed_actions_for_role(role)


def decide_action(*, role: object, action: object) -> TenantAuthorizationDecision:
    """Return a fail-closed decision for one role/action check."""

    normalized_role = _normalize_value(role)
    normalized_action = _normalize_value(action)
    if normalized_role not in ROLE_ACTIONS:
        return TenantAuthorizationDecision(
            role=normalized_role,
            action=normalized_action,
            allowed=False,
            reason="unknown_role",
        )
    if normalized_action not in KNOWN_ACTIONS:
        return TenantAuthorizationDecision(
            role=normalized_role,
            action=normalized_action,
            allowed=False,
            reason="unknown_action",
        )
    allowed = normalized_action in ROLE_ACTIONS[normalized_role]
    return TenantAuthorizationDecision(
        role=normalized_role,
        action=normalized_action,
        allowed=allowed,
        reason="allowed" if allowed else "forbidden",
    )
