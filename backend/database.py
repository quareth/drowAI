"""Database engine/session setup and shared SQLAlchemy primitives.

This module owns Base/session lifecycle and shared DB-level types used by
multiple ORM modules, including the cross-dialect GUID TypeDecorator.
"""

import os
import uuid as uuid_lib

from dotenv import load_dotenv
from sqlalchemy import CHAR, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQL_UUID
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import TypeDecorator

from backend.config.generated_config import resolve_config_value
from backend.config.feature_flags import is_cloud_runner_control_enabled
from backend.config.retention import RETENTION_POLICY_DEFAULTS

load_dotenv()

# Database URL from environment or generated deployment config.
DATABASE_URL = resolve_config_value("DATABASE_URL") or os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")
    
# Use psycopg2 driver for synchronous operations
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Create synchronous engine
def _create_engine(database_url: str):
    return create_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
    )


engine = _create_engine(DATABASE_URL)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create declarative base
Base = declarative_base()


class GUID(TypeDecorator):
    """Platform-independent GUID type with native PostgreSQL UUID support."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgreSQL_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid_lib.UUID) else uuid_lib.UUID(value)
        if isinstance(value, uuid_lib.UUID):
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid_lib.UUID):
            return value
        return uuid_lib.UUID(value) if value else None

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        db.rollback()
        raise e
    finally:
        try:
            from backend.services.tenant.rls import clear_rls_session_context

            clear_rls_session_context(db)
        except Exception:
            # Cleanup must never mask the original request error path.
            pass
        db.close()


def reconfigure_database(database_url: str) -> None:
    """Swap the global SQLAlchemy engine/session binding after config rotation."""
    global DATABASE_URL, engine
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise ValueError("database_url must not be empty.")
    if normalized_url.startswith("postgres://"):
        normalized_url = normalized_url.replace("postgres://", "postgresql://", 1)
    previous_engine = engine
    engine = _create_engine(normalized_url)
    SessionLocal.configure(bind=engine)
    DATABASE_URL = normalized_url
    previous_engine.dispose()

# Initialize database
def ensure_pgvector_extension() -> None:
    """Enable pgvector on PostgreSQL before DDL that references VECTOR columns."""
    if not DATABASE_URL.startswith("postgresql"):
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def init_db():
    """Create ORM tables for tests/dev utilities only.

    Product startup paths must run Alembic migrations instead of this helper so
    database schema, indexes, seed rows, extensions, and RLS policies share one
    authoritative migration path.
    """

    import backend.models  # noqa: F401
    ensure_pgvector_extension()
    Base.metadata.create_all(bind=engine)


def ensure_tenant_baseline_schema_ready() -> None:
    """Fail fast when tenant baseline migrations are missing."""

    inspector = inspect(engine)
    missing = []

    required_tables = ("tenants", "tenant_memberships")
    for table_name in required_tables:
        if not inspector.has_table(table_name):
            missing.append(f"missing table `{table_name}`")

    required_columns = {
        "tasks": (
            "tenant_id",
            "runtime_placement_mode",
            "runner_id",
            "execution_site_id",
            "workspace_id",
            "graph_thread_id",
        ),
        "engagements": ("tenant_id",),
    }
    for table_name, columns in required_columns.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in columns:
            if column_name not in existing:
                missing.append(f"missing column `{table_name}.{column_name}`")

    if missing:
        details = ", ".join(missing)
        raise RuntimeError(
            "Tenant baseline schema is not applied "
            f"({details}). Run `cd backend && alembic upgrade head` before starting the backend."
        )

    ensure_tenant_isolation_schema_ready(inspector=inspector)


def ensure_reporting_lifecycle_schema_ready() -> None:
    """Fail fast when engagement report lifecycle migrations are missing."""

    inspector = inspect(engine)
    missing: list[str] = []

    required_tables = (
        "engagement_reports",
        "engagement_report_jobs",
        "tenant_data_management_settings",
    )
    for table_name in required_tables:
        if not inspector.has_table(table_name):
            missing.append(f"missing table `{table_name}`")

    tenant_data_management_settings_columns = (
        "tenant_id",
        "report_retention_enabled",
        *RETENTION_POLICY_DEFAULTS.keys(),
    )

    required_columns = {
        "engagement_reports": (
            "delete_scheduled_at",
            "delete_undo_until",
            "deletion_finalized_at",
            "deleted_by_user_id",
            "deletion_reason",
            "deletion_metadata",
            "deletion_original_is_current",
        ),
        "engagement_report_jobs": (
            "generation_phase",
            "next_attempt_at",
            "last_error_at",
        ),
        "tenant_data_management_settings": tenant_data_management_settings_columns,
    }
    for table_name, columns in required_columns.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in columns:
            if column_name not in existing:
                missing.append(f"missing column `{table_name}.{column_name}`")

    if missing:
        details = ", ".join(missing)
        raise RuntimeError(
            "Reporting lifecycle schema is not applied "
            f"({details}). Run `cd backend && PYTHONPATH=.. alembic upgrade head` "
            "before starting the backend."
        )


def ensure_runner_control_schema_ready() -> None:
    """Fail fast for cloud-runner mode when runner control plane schema is missing."""
    if not is_cloud_runner_control_enabled():
        return

    inspector = inspect(engine)
    missing: list[str] = []

    required_tables = (
        "execution_sites",
        "runners",
        "runner_credentials",
        "runner_install_tokens",
        "runtime_jobs",
        "runner_connections",
        "runner_control_messages",
    )
    for table_name in required_tables:
        if not inspector.has_table(table_name):
            missing.append(f"missing table `{table_name}`")

    required_columns = {
        "execution_sites": ("tenant_id",),
        "runners": ("tenant_id", "execution_site_id"),
        "runner_credentials": ("tenant_id", "runner_id", "secret_hash"),
        "runner_install_tokens": ("tenant_id", "execution_site_id", "token_hash"),
        "runtime_jobs": ("tenant_id", "job_type", "idempotency_key"),
        "runner_connections": (
            "tenant_id",
            "runner_id",
            "pod_id",
            "connection_id",
            "remote_ip_address",
        ),
        "runner_control_messages": (
            "tenant_id",
            "runner_id",
            "message_id",
            "direction",
            "delivery_attempt_count",
        ),
    }
    for table_name, columns in required_columns.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in columns:
            if column_name not in existing:
                missing.append(f"missing column `{table_name}.{column_name}`")

    if inspector.has_table("runner_credentials"):
        credential_columns = {column["name"] for column in inspector.get_columns("runner_credentials")}
        if "secret_hash" not in credential_columns:
            missing.append("missing column `runner_credentials.secret_hash`")
        if "secret" in credential_columns:
            missing.append("disallowed plaintext column `runner_credentials.secret`")

    if missing:
        details = ", ".join(missing)
        raise RuntimeError(
            "Runner control-plane schema is not applied "
            f"({details}). Run `cd backend && alembic upgrade head` before starting with "
            "`ENABLE_CLOUD_RUNNER_CONTROL=true`."
        )


def _is_tenant_isolation_schema_readiness_required() -> bool:
    multi_tenant_required = str(
        os.getenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", "")
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    rls_required = str(os.getenv("TENANT_ISOLATION_RLS_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return multi_tenant_required or rls_required


def ensure_tenant_isolation_schema_ready(*, inspector=None) -> None:
    """Fail fast for Tenant Isolation tenant ownership columns when SaaS/RLS enforcement is enabled."""
    if not _is_tenant_isolation_schema_readiness_required():
        return

    schema_inspector = inspector or inspect(engine)
    missing: list[str] = []

    required_columns = {
        "reports": ("tenant_id",),
        "llm_conversations": ("tenant_id",),
        "llm_usage_records": ("tenant_id",),
        "agent_logs": ("tenant_id",),
        "chat_messages": ("tenant_id",),
        "chat_turn_events": ("tenant_id",),
        "tool_calls": ("tenant_id",),
        "system_logs": ("tenant_id",),
        "stream_events": ("tenant_id",),
        "turn_workflows": ("tenant_id",),
        "interrupt_tickets": ("tenant_id",),
        "task_history": ("tenant_id",),
        "knowledge_assets": ("tenant_id",),
        "knowledge_services": ("tenant_id",),
        "knowledge_findings": ("tenant_id",),
        "knowledge_relationships": ("tenant_id",),
        "knowledge_web_paths": ("tenant_id",),
        "engagement_asset_links": ("tenant_id",),
        "engagement_service_links": ("tenant_id",),
        "engagement_finding_links": ("tenant_id",),
        "engagement_web_path_links": ("tenant_id",),
        "knowledge_entity_provenance": ("tenant_id",),
        "semantic_memories": ("tenant_id",),
    }

    for table_name, columns in required_columns.items():
        if not schema_inspector.has_table(table_name):
            missing.append(f"missing table `{table_name}`")
            continue
        existing = {column["name"] for column in schema_inspector.get_columns(table_name)}
        for column_name in columns:
            if column_name not in existing:
                missing.append(f"missing column `{table_name}.{column_name}`")

    if missing:
        details = ", ".join(missing)
        raise RuntimeError(
            "Tenant isolation schema is not applied "
            f"({details}). Run `cd backend && alembic upgrade head` before starting with "
            "`TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED=true` or "
            "`TENANT_ISOLATION_RLS_ENABLED=true`."
        )
