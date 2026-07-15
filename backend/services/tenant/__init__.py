"""Tenant service entrypoints for tenant baseline context and bootstrap flows.

Responsibilities:
- Expose tenant context resolution for user-scoped write paths.
- Expose startup bootstrap helpers for default tenant membership repair.
- Expose centralized tenant authorization policy helpers.
"""

from .authorization import (
    KNOWN_ACTIONS,
    POLICY_VERSION,
    ROLE_ACTIONS,
    TenantAuthorizationDecision,
    allowed_actions_for_role,
    decide_action,
    is_action_allowed,
)
from .bootstrap import bootstrap_default_tenant_state
from .context import TenantContext, TenantContextService

__all__ = [
    "KNOWN_ACTIONS",
    "POLICY_VERSION",
    "ROLE_ACTIONS",
    "TenantAuthorizationDecision",
    "TenantContext",
    "TenantContextService",
    "allowed_actions_for_role",
    "bootstrap_default_tenant_state",
    "decide_action",
    "is_action_allowed",
]
