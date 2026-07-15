"""Tenant bootstrap helpers for startup and migration-repair paths.

Responsibilities:
- Ensure default tenant identity exists during backend startup.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.database import SessionLocal

from .context import TenantContextService
from .rls import privileged_rls_bypass


def bootstrap_default_tenant_state() -> None:
    """Ensure default tenant identity exists after schema readiness checks."""
    db: Session = SessionLocal()
    try:
        # Startup bootstrap/repair is a trusted maintenance path, not a user request.
        with privileged_rls_bypass(db, scope="repair", actor_type="system"):
            service = TenantContextService(db)
            service.ensure_default_tenant()
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
