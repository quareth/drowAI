"""Shared audit event helpers for tenant-scoped service boundaries.

This package centralizes event-shape and metadata-redaction behavior so service
emitters can produce consistent tenant audit envelopes without duplicating
security logic.
"""

from backend.services.audit.tenant_events import (
    DEFAULT_AUDIT_REASON_CODE,
    build_tenant_audit_event,
    redact_audit_metadata,
)

__all__ = [
    "DEFAULT_AUDIT_REASON_CODE",
    "build_tenant_audit_event",
    "redact_audit_metadata",
]

