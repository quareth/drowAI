"""Tenant ownership ORM models for tenant baseline runtime decoupling.

Scope:
- Defines tenant identity and tenant-to-user membership tables used to
  establish baseline ownership for tasks and engagements.

Boundaries:
- ORM table/relationship definitions only; tenant resolution and bootstrap
  workflows are implemented in dedicated services.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="active", server_default="active")
    # Nullable tenant-wide quota override; NULL means use configured defaults/unlimited.
    max_concurrent_tasks = Column(Integer, nullable=True)
    # Nullable per-user quota default within this tenant; NULL means fallback to config.
    max_concurrent_tasks_per_user = Column(Integer, nullable=True)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    memberships = relationship("TenantMembership", back_populates="tenant", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="tenant")
    engagements = relationship("Engagement", back_populates="tenant")


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),)

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="owner", server_default="owner")
    status = Column(String(32), nullable=False, default="active", server_default="active")
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    deactivated_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tenant = relationship("Tenant", back_populates="memberships")
    user = relationship("User", back_populates="tenant_memberships", foreign_keys=[user_id])
