"""Knowledge-domain SQLAlchemy ORM models.

This module owns durable ingestion, observation, evidence archive, canonical
entity, engagement-link, and provenance tables for the knowledge subsystem.
Models are extracted from `backend.models` to reduce monolith coupling while
preserving string-based relationship mappings for import-order safety.
"""

import uuid as uuid_lib

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base, GUID


class KnowledgeIngestionRun(Base):
    """Durable ingestion lifecycle row for one execution replay unit."""

    __tablename__ = "knowledge_ingestion_runs"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False)
    task_id = Column(Integer, nullable=True)
    source_execution_id = Column(GUID(), nullable=False)
    extractor_family = Column(String(100), nullable=False)
    extractor_version = Column(String(50), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    run_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    engagement = relationship("Engagement", back_populates="knowledge_ingestion_runs")
    observations = relationship("KnowledgeObservation", back_populates="ingestion_run", cascade="all, delete-orphan")

    __table_args__ = (
        Index(
            "ux_knowledge_runs_engagement_exec_extractor",
            "engagement_id",
            "source_execution_id",
            "extractor_family",
            "extractor_version",
            unique=True,
        ),
        Index("ix_knowledge_runs_engagement_created", "engagement_id", "created_at"),
        Index("ix_knowledge_runs_source_execution", "source_execution_id"),
        Index("ix_knowledge_runs_user_created", "user_id", "created_at"),
        Index("ix_knowledge_runs_tenant_created", "tenant_id", "created_at"),
        Index("ix_knowledge_runs_tenant_source_execution", "tenant_id", "source_execution_id"),
    )


class KnowledgeObservation(Base):
    """Append-only durable observation row linked to one ingestion run."""

    __tablename__ = "knowledge_observations"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ingestion_run_id = Column(
        GUID(),
        ForeignKey("knowledge_ingestion_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False)
    task_id = Column(Integer, nullable=True)
    source_execution_id = Column(GUID(), nullable=False)
    observation_type = Column(String(120), nullable=False)
    subject_type = Column(String(120), nullable=False)
    subject_key = Column(String(512), nullable=False)
    assertion_level = Column(String(32), nullable=False)
    dedupe_key = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    observation_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ingestion_run = relationship("KnowledgeIngestionRun", back_populates="observations")
    engagement = relationship("Engagement", back_populates="knowledge_observations")

    __table_args__ = (
        UniqueConstraint(
            "ingestion_run_id",
            "dedupe_key",
            name="ux_knowledge_observations_run_dedupe",
        ),
        Index("ix_knowledge_observations_engagement_created", "engagement_id", "created_at"),
        Index("ix_knowledge_observations_engagement_subject", "engagement_id", "subject_type", "subject_key"),
        Index("ix_knowledge_observations_source_execution", "source_execution_id"),
        Index("ix_knowledge_observations_user_created", "user_id", "created_at"),
        Index("ix_knowledge_observations_tenant_created", "tenant_id", "created_at"),
        Index("ix_knowledge_observations_tenant_source_execution", "tenant_id", "source_execution_id"),
    )


class KnowledgeEvidenceArchive(Base):
    """Minimal durable evidence snapshot row for task-delete survival."""

    __tablename__ = "knowledge_evidence_archives"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=False)
    task_id = Column(Integer, nullable=True)
    source_execution_id = Column(GUID(), nullable=False)
    source_artifact_id = Column(GUID(), nullable=True)
    storage_mode = Column(String(32), nullable=False)
    inline_excerpt = Column(Text, nullable=True)
    object_key = Column(Text, nullable=True)
    archived_file_ref = Column(Text, nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    mime_type = Column(String(255), nullable=True)
    lineage_snapshot = Column("lineage", JSON, nullable=False, default=dict)
    archive_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    engagement = relationship("Engagement", back_populates="knowledge_evidence_archives")

    __table_args__ = (
        Index("ix_knowledge_archives_engagement_created", "engagement_id", "created_at"),
        Index("ix_knowledge_archives_source_execution", "source_execution_id"),
        Index("ix_knowledge_archives_source_artifact", "source_artifact_id"),
        Index("ix_knowledge_archives_user_created", "user_id", "created_at"),
        Index("ix_knowledge_archives_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_knowledge_archives_tenant_user_engagement_created",
            "tenant_id",
            "user_id",
            "engagement_id",
            "created_at",
        ),
        Index("ix_knowledge_archives_tenant_object_key", "tenant_id", "object_key"),
    )


class KnowledgeAsset(Base):
    """Canonical user-owned asset record within a tenant."""

    __tablename__ = "knowledge_assets"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True)
    asset_key = Column(String(512), nullable=False)
    asset_type = Column(String(120), nullable=False)
    display_name = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    hostname = Column(String(255), nullable=True)
    status = Column(String(32), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    max_confidence = Column(String(32), nullable=True)
    asset_metadata = Column("metadata", JSON, nullable=True)

    engagement = relationship("Engagement", back_populates="knowledge_assets")
    services = relationship("KnowledgeService", back_populates="asset")
    findings = relationship("KnowledgeFinding", back_populates="asset")
    web_paths = relationship("KnowledgeWebPath", back_populates="asset")
    engagement_links = relationship("EngagementAssetLink", back_populates="asset", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "asset_key", name="ux_knowledge_assets_tenant_user_asset_key"),
        Index("ix_knowledge_assets_tenant_user_asset_key", "tenant_id", "user_id", "asset_key"),
        Index("ix_knowledge_assets_tenant_user_asset_type", "tenant_id", "user_id", "asset_type"),
        Index("ix_knowledge_assets_tenant_user_last_seen", "tenant_id", "user_id", "last_seen_at"),
    )


class KnowledgeService(Base):
    """Canonical user-owned service record within a tenant."""

    __tablename__ = "knowledge_services"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True)
    service_key = Column(String(512), nullable=False)
    asset_id = Column(GUID(), ForeignKey("knowledge_assets.id", ondelete="SET NULL"), nullable=True)
    protocol = Column(String(16), nullable=True)
    port = Column(Integer, nullable=True)
    service_name = Column(String(255), nullable=True)
    product = Column(String(255), nullable=True)
    version = Column(String(120), nullable=True)
    status = Column(String(32), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    service_metadata = Column("metadata", JSON, nullable=True)

    engagement = relationship("Engagement", back_populates="knowledge_services")
    asset = relationship("KnowledgeAsset", back_populates="services")
    findings = relationship("KnowledgeFinding", back_populates="service")
    web_paths = relationship("KnowledgeWebPath", back_populates="service")
    engagement_links = relationship("EngagementServiceLink", back_populates="service", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "service_key", name="ux_knowledge_services_tenant_user_service_key"),
        Index("ix_knowledge_services_tenant_user_service_key", "tenant_id", "user_id", "service_key"),
        Index("ix_knowledge_services_tenant_user_asset", "tenant_id", "user_id", "asset_id"),
        Index("ix_knowledge_services_tenant_user_last_seen", "tenant_id", "user_id", "last_seen_at"),
    )


class KnowledgeFinding(Base):
    """Canonical user-owned finding record within a tenant."""

    __tablename__ = "knowledge_findings"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True)
    finding_key = Column(String(512), nullable=False)
    finding_type = Column(String(120), nullable=False)
    subject_type = Column(String(120), nullable=False)
    subject_key = Column(String(512), nullable=False)
    asset_id = Column(GUID(), ForeignKey("knowledge_assets.id", ondelete="SET NULL"), nullable=True)
    service_id = Column(GUID(), ForeignKey("knowledge_services.id", ondelete="SET NULL"), nullable=True)
    title = Column(Text, nullable=True)
    severity = Column(String(32), nullable=True)
    status = Column(String(32), nullable=True)
    assertion_level = Column(String(32), nullable=True)
    confidence = Column(String(32), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    evidence_summary = Column(JSON, nullable=True)
    finding_metadata = Column("metadata", JSON, nullable=True)

    engagement = relationship("Engagement", back_populates="knowledge_findings")
    asset = relationship("KnowledgeAsset", back_populates="findings")
    service = relationship("KnowledgeService", back_populates="findings")
    engagement_links = relationship("EngagementFindingLink", back_populates="finding", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "finding_key", name="ux_knowledge_findings_tenant_user_finding_key"),
        Index("ix_knowledge_findings_tenant_user_finding_key", "tenant_id", "user_id", "finding_key"),
        Index("ix_knowledge_findings_tenant_user_asset", "tenant_id", "user_id", "asset_id"),
        Index("ix_knowledge_findings_tenant_user_service", "tenant_id", "user_id", "service_id"),
        Index("ix_knowledge_findings_tenant_user_status", "tenant_id", "user_id", "status"),
    )


class KnowledgeRelationship(Base):
    """Canonical user-owned relationship record within a tenant."""

    __tablename__ = "knowledge_relationships"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True)
    relationship_key = Column(String(512), nullable=False)
    source_subject_key = Column(String(512), nullable=False)
    relationship_type = Column(String(64), nullable=False)
    target_subject_key = Column(String(512), nullable=False)
    confidence = Column(String(32), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    relationship_metadata = Column("metadata", JSON, nullable=True)

    engagement = relationship("Engagement", back_populates="knowledge_relationships")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "relationship_key",
            name="ux_knowledge_relationships_tenant_user_relationship_key",
        ),
        Index(
            "ix_knowledge_relationships_tenant_user_relationship_key",
            "tenant_id",
            "user_id",
            "relationship_key",
        ),
        Index("ix_knowledge_relationships_tenant_user_source", "tenant_id", "user_id", "source_subject_key"),
        Index("ix_knowledge_relationships_tenant_user_target", "tenant_id", "user_id", "target_subject_key"),
        Index("ix_knowledge_relationships_tenant_user_type", "tenant_id", "user_id", "relationship_type"),
    )


class EngagementAssetLink(Base):
    """Engagement lens links to canonical assets with first/last seen timestamps."""

    __tablename__ = "engagement_asset_links"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    asset_id = Column(GUID(), ForeignKey("knowledge_assets.id", ondelete="CASCADE"), nullable=False)
    first_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)
    last_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)

    engagement = relationship("Engagement", back_populates="engagement_asset_links")
    asset = relationship("KnowledgeAsset", back_populates="engagement_links")

    __table_args__ = (
        UniqueConstraint("engagement_id", "asset_id", name="ux_engagement_asset_links"),
        Index("ix_engagement_asset_links_engagement", "engagement_id"),
        Index("ix_engagement_asset_links_asset", "asset_id"),
        Index("ix_engagement_asset_links_tenant_engagement", "tenant_id", "engagement_id"),
    )


class EngagementServiceLink(Base):
    """Engagement lens links to canonical services with first/last seen timestamps."""

    __tablename__ = "engagement_service_links"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    service_id = Column(GUID(), ForeignKey("knowledge_services.id", ondelete="CASCADE"), nullable=False)
    first_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)
    last_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)

    engagement = relationship("Engagement", back_populates="engagement_service_links")
    service = relationship("KnowledgeService", back_populates="engagement_links")

    __table_args__ = (
        UniqueConstraint("engagement_id", "service_id", name="ux_engagement_service_links"),
        Index("ix_engagement_service_links_engagement", "engagement_id"),
        Index("ix_engagement_service_links_service", "service_id"),
        Index("ix_engagement_service_links_tenant_engagement", "tenant_id", "engagement_id"),
    )


class EngagementFindingLink(Base):
    """Engagement lens links to canonical findings with first/last seen timestamps."""

    __tablename__ = "engagement_finding_links"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    finding_id = Column(GUID(), ForeignKey("knowledge_findings.id", ondelete="CASCADE"), nullable=False)
    first_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)
    last_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)

    engagement = relationship("Engagement", back_populates="engagement_finding_links")
    finding = relationship("KnowledgeFinding", back_populates="engagement_links")

    __table_args__ = (
        UniqueConstraint("engagement_id", "finding_id", name="ux_engagement_finding_links"),
        Index("ix_engagement_finding_links_engagement", "engagement_id"),
        Index("ix_engagement_finding_links_finding", "finding_id"),
        Index("ix_engagement_finding_links_tenant_engagement", "tenant_id", "engagement_id"),
    )


class KnowledgeWebPath(Base):
    """Canonical tenant-scoped web path row deduplicated by canonical URL."""

    __tablename__ = "knowledge_web_paths"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    asset_id = Column(GUID(), ForeignKey("knowledge_assets.id", ondelete="SET NULL"), nullable=True)
    service_id = Column(GUID(), ForeignKey("knowledge_services.id", ondelete="SET NULL"), nullable=True)
    canonical_url = Column(String(1024), nullable=False)
    origin_key = Column(String(512), nullable=False)
    path = Column(String(1024), nullable=False)
    last_status_code = Column(Integer, nullable=True)
    last_response_size = Column(BigInteger, nullable=True)
    calibrated_baseline = Column(Boolean, nullable=False, default=False)
    noise_score = Column(Float, nullable=False, default=0.0)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    producer_summary = Column(JSON, nullable=False, default=dict)
    evidence_refs = Column(JSON, nullable=False, default=list)

    asset = relationship("KnowledgeAsset", back_populates="web_paths")
    service = relationship("KnowledgeService", back_populates="web_paths")
    engagement_links = relationship("EngagementWebPathLink", back_populates="web_path", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "canonical_url", name="ux_knowledge_web_paths_tenant_user_url"),
        Index("ix_knowledge_web_paths_tenant_user_url", "tenant_id", "user_id", "canonical_url"),
        Index("ix_knowledge_web_paths_tenant_user_asset", "tenant_id", "user_id", "asset_id"),
        Index("ix_knowledge_web_paths_tenant_user_service", "tenant_id", "user_id", "service_id"),
        Index("ix_knowledge_web_paths_tenant_user_origin", "tenant_id", "user_id", "origin_key"),
        Index("ix_knowledge_web_paths_tenant_user_last_seen", "tenant_id", "user_id", "last_seen_at"),
    )


class EngagementWebPathLink(Base):
    """Engagement lens links to canonical web paths with first/last seen timestamps."""

    __tablename__ = "engagement_web_path_links"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    web_path_id = Column(GUID(), ForeignKey("knowledge_web_paths.id", ondelete="CASCADE"), nullable=False)
    first_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)
    last_seen_in_engagement = Column(DateTime(timezone=True), nullable=False)

    engagement = relationship("Engagement", back_populates="engagement_web_path_links")
    web_path = relationship("KnowledgeWebPath", back_populates="engagement_links")

    __table_args__ = (
        UniqueConstraint("engagement_id", "web_path_id", name="ux_engagement_web_path_links"),
        Index("ix_engagement_web_path_links_engagement", "engagement_id"),
        Index("ix_engagement_web_path_links_web_path", "web_path_id"),
        Index("ix_engagement_web_path_links_tenant_engagement", "tenant_id", "engagement_id"),
    )


class KnowledgeEntityProvenance(Base):
    """Entity provenance records linking canonical entities to execution evidence."""

    __tablename__ = "knowledge_entity_provenance"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(GUID(), nullable=False)
    engagement_id = Column(Integer, nullable=True)
    task_id = Column(Integer, nullable=True)
    execution_id = Column(GUID(), nullable=True)
    tool_name = Column(String(255), nullable=True)
    ingestion_run_id = Column(GUID(), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    confidence = Column(String(32), nullable=True)
    evidence_archive_id = Column(GUID(), nullable=True)

    __table_args__ = (
        Index("ix_provenance_user_entity", "user_id", "entity_type", "entity_id"),
        Index("ix_provenance_tenant_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_provenance_task", "task_id"),
        Index("ix_provenance_execution", "execution_id"),
    )
