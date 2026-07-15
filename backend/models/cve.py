"""CVE indexing SQLAlchemy ORM models.

Scope:
- Declares the CVE indexing settings, run history, cursor state, canonical CVE
  record, and affected-product projection tables.
- Registers all CVE ORM models on the shared `Base` from `backend.database`.

Boundaries:
- ORM table definitions and relationships only; no sync orchestration, query
  policies, or API routing logic.
- CVE indexing services and contracts remain in `backend.services.cve_indexing`.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base


class CveIndexSettings(Base):
    """Global CVE indexing settings row (singleton-style)."""

    __tablename__ = "cve_index_settings"

    id = Column(Integer, primary_key=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    daily_sync_hour_utc = Column(Integer, nullable=False, default=2, server_default="2")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CveIndexSyncRun(Base):
    """Per-run execution history for CVE baseline/delta/noop sync operations."""

    __tablename__ = "cve_index_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    trigger_kind = Column(String(24), nullable=False, default="manual", server_default="manual")
    sync_kind = Column(String(24), nullable=False)
    status = Column(String(24), nullable=False, default="running", server_default="running")
    baseline_date = Column(Date, nullable=True)
    delta_from_hour_utc = Column(DateTime(timezone=True), nullable=True)
    delta_to_hour_utc = Column(DateTime(timezone=True), nullable=True)
    phase = Column(String(24), nullable=True)
    progress_updated_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    processed_records = Column(Integer, nullable=False, default=0, server_default="0")
    inserted_records = Column(Integer, nullable=False, default=0, server_default="0")
    updated_records = Column(Integer, nullable=False, default=0, server_default="0")
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_cve_index_sync_runs_started_at", "started_at"),
        Index("ix_cve_index_sync_runs_finished_at", "finished_at"),
        Index("ix_cve_index_sync_runs_status", "status"),
    )


class CveIndexState(Base):
    """Global CVE indexing operational cursor and health status."""

    __tablename__ = "cve_index_state"

    id = Column(Integer, primary_key=True, index=True)
    last_sync_status = Column(String(24), nullable=False, default="idle", server_default="idle")
    last_successful_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_started_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    last_applied_baseline_date = Column(Date, nullable=True)
    last_applied_delta_hour_utc = Column(DateTime(timezone=True), nullable=True)
    rebuild_required = Column(Boolean, nullable=False, default=False, server_default="false")
    active_run_id = Column(Integer, ForeignKey("cve_index_sync_runs.id"), nullable=True)
    lease_owner_id = Column(String(128), nullable=True)
    lease_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    current_phase = Column(String(24), nullable=True)
    progress_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    active_run = relationship("CveIndexSyncRun", foreign_keys=[active_run_id])

    __table_args__ = (
        Index("ix_cve_index_state_last_attempt_started_at", "last_attempt_started_at"),
        Index("ix_cve_index_state_last_attempt_finished_at", "last_attempt_finished_at"),
        Index("ix_cve_index_state_last_sync_status", "last_sync_status"),
        Index("ix_cve_index_state_lease_expires_at", "lease_expires_at"),
    )


class CveRecord(Base):
    """Locally indexed canonical CVE record snapshot."""

    __tablename__ = "cve_records"

    id = Column(BigInteger, primary_key=True, index=True)
    cve_id = Column(String(32), nullable=False, unique=True)
    source = Column(String(24), nullable=False, default="cvelist_v5", server_default="cvelist_v5")
    record_state = Column(String(24), nullable=False, default="published", server_default="published")
    published_at = Column(DateTime(timezone=True), nullable=True)
    source_updated_at = Column(DateTime(timezone=True), nullable=True)
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    severity = Column(String(24), nullable=True)
    metrics = Column(JSON, nullable=True)
    weaknesses = Column(JSON, nullable=True)
    references = Column(JSON, nullable=True)
    cve_json = Column(JSON, nullable=False)
    projection_status = Column(String(32), nullable=False, default="pending", server_default="pending")
    projection_affected_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    projection_error_code = Column(String(64), nullable=True)
    projection_last_projected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    affected_products = relationship("CveAffectedProduct", back_populates="cve_record", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_cve_records_cve_id", "cve_id"),
        Index("ix_cve_records_record_state", "record_state"),
        Index("ix_cve_records_projection_status", "projection_status"),
        Index("ix_cve_records_source_updated_at", "source_updated_at"),
    )


class CveAffectedProduct(Base):
    """Searchable affected-product projection rows keyed to one canonical CVE record."""

    __tablename__ = "cve_affected_products"

    id = Column(BigInteger, primary_key=True, index=True)
    cve_record_id = Column(BigInteger, ForeignKey("cve_records.id", ondelete="CASCADE"), nullable=False)
    cve_id = Column(String(32), nullable=False)
    vendor_raw = Column(Text, nullable=True)
    vendor_norm = Column(String(255), nullable=True)
    product_raw = Column(Text, nullable=True)
    product_norm = Column(String(255), nullable=True)
    default_status = Column(String(32), nullable=True)
    versions_json = Column(JSON, nullable=True)
    cpes_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    cve_record = relationship("CveRecord", back_populates="affected_products")

    __table_args__ = (
        Index("ix_cve_affected_products_cve_record_id", "cve_record_id"),
        Index("ix_cve_affected_products_cve_id", "cve_id"),
        Index("ix_cve_affected_products_vendor_norm", "vendor_norm"),
        Index("ix_cve_affected_products_product_norm", "product_norm"),
        Index("ix_cve_affected_products_vendor_product_norm", "vendor_norm", "product_norm"),
    )

