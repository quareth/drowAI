"""Add deployment-aware identity for text LLM workloads.

Revision ID: 0006_llm_deployment_identity
Revises: 0005_resumable_reports
Create Date: 2026-07-18 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006_llm_deployment_identity"
down_revision: Union[str, Sequence[str], None] = "0005_resumable_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _guid_type() -> sa.types.TypeEngine:
    """Return the portable UUID type without importing application modules."""

    return sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


def upgrade() -> None:
    """Add identity tables and nullable references while preserving snapshots."""

    op.create_table(
        "llm_inference_connections",
        sa.Column("id", _guid_type(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("connection_preset_id", sa.String(length=100), nullable=False),
        sa.Column("runtime_family_id", sa.String(length=100), nullable=False),
        sa.Column("serving_operator_id", sa.String(length=100), nullable=True),
        sa.Column(
            "transport_origin",
            sa.String(length=32),
            nullable=False,
            server_default="backend",
        ),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("endpoint_policy_id", sa.String(length=100), nullable=True),
        sa.Column(
            "config_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("non_secret_config", sa.JSON(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("legacy_default_provider", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'disabled', 'enabled')",
            name="ck_llm_inference_connections_state",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_llm_inference_connections_revision",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "legacy_default_provider",
            name="uq_llm_inference_connections_legacy_default",
        ),
    )
    op.create_index(
        "ix_llm_inference_connections_user_id",
        "llm_inference_connections",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_inference_connections_user_state",
        "llm_inference_connections",
        ["user_id", "state"],
        unique=False,
    )

    op.create_table(
        "llm_model_deployments",
        sa.Column("id", _guid_type(), nullable=False),
        sa.Column("connection_id", _guid_type(), nullable=False),
        sa.Column("wire_model_id", sa.String(length=512), nullable=False),
        sa.Column("canonical_model_id", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("discovery_source", sa.String(length=50), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "availability_state",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_llm_model_deployments_revision",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["llm_inference_connections.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "wire_model_id",
            name="uq_llm_model_deployments_connection_wire_model",
        ),
    )
    op.create_index(
        "ix_llm_model_deployments_connection_id",
        "llm_model_deployments",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_model_deployments_connection_enabled",
        "llm_model_deployments",
        ["connection_id", "enabled"],
        unique=False,
    )

    op.create_table(
        "llm_deployment_routes",
        sa.Column("id", _guid_type(), nullable=False),
        sa.Column("deployment_id", _guid_type(), nullable=False),
        sa.Column("adapter_id", sa.String(length=100), nullable=False),
        sa.Column("adapter_version", sa.String(length=50), nullable=False),
        sa.Column("api_surface", sa.String(length=100), nullable=False),
        sa.Column("dialect_policy_id", sa.String(length=100), nullable=False),
        sa.Column("billing_provider_id", sa.String(length=100), nullable=True),
        sa.Column("route_config", sa.JSON(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["llm_model_deployments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "adapter_id",
            "api_surface",
            "dialect_policy_id",
            name="uq_llm_deployment_routes_protocol",
        ),
    )
    op.create_index(
        "ix_llm_deployment_routes_deployment_id",
        "llm_deployment_routes",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_deployment_routes_deployment_enabled",
        "llm_deployment_routes",
        ["deployment_id", "enabled"],
        unique=False,
    )

    op.create_table(
        "llm_capability_observations",
        sa.Column("id", _guid_type(), nullable=False),
        sa.Column("deployment_id", _guid_type(), nullable=False),
        sa.Column("route_id", _guid_type(), nullable=True),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column(
            "support_state",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("constraints", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "support_state IN ('supported', 'unsupported', 'unknown')",
            name="ck_llm_capability_observations_support_state",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_llm_capability_observations_revision",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["llm_model_deployments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["llm_deployment_routes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "route_id",
            "capability",
            "revision",
            name="uq_llm_capability_observations_revision",
        ),
    )
    op.create_index(
        "ix_llm_capability_observations_deployment_id",
        "llm_capability_observations",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_capability_observations_route_id",
        "llm_capability_observations",
        ["route_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_capability_observations_lookup",
        "llm_capability_observations",
        ["deployment_id", "capability", "observed_at"],
        unique=False,
    )

    _add_identity_references(
        "user_llm_selections",
        (("deployment_id", "llm_model_deployments.id"),),
    )
    _add_identity_references(
        "user_reporting_llm_selections",
        (("deployment_id", "llm_model_deployments.id"),),
    )
    _add_identity_references(
        "user_memory_llm_selections",
        (
            ("gate_deployment_id", "llm_model_deployments.id"),
            ("extraction_deployment_id", "llm_model_deployments.id"),
        ),
    )
    for table_name in ("llm_conversations", "llm_usage_records"):
        _add_identity_references(
            table_name,
            (
                ("connection_id", "llm_inference_connections.id"),
                ("deployment_id", "llm_model_deployments.id"),
                ("route_id", "llm_deployment_routes.id"),
            ),
        )


def downgrade() -> None:
    """Remove deployment identity additions in reverse dependency order."""

    for table_name in ("llm_usage_records", "llm_conversations"):
        _drop_identity_references(
            table_name,
            ("route_id", "deployment_id", "connection_id"),
        )
    _drop_identity_references(
        "user_memory_llm_selections",
        ("extraction_deployment_id", "gate_deployment_id"),
    )
    _drop_identity_references(
        "user_reporting_llm_selections",
        ("deployment_id",),
    )
    _drop_identity_references("user_llm_selections", ("deployment_id",))

    op.drop_index(
        "ix_llm_capability_observations_lookup",
        table_name="llm_capability_observations",
    )
    op.drop_index(
        "ix_llm_capability_observations_route_id",
        table_name="llm_capability_observations",
    )
    op.drop_index(
        "ix_llm_capability_observations_deployment_id",
        table_name="llm_capability_observations",
    )
    op.drop_table("llm_capability_observations")
    op.drop_index(
        "ix_llm_deployment_routes_deployment_enabled",
        table_name="llm_deployment_routes",
    )
    op.drop_index(
        "ix_llm_deployment_routes_deployment_id",
        table_name="llm_deployment_routes",
    )
    op.drop_table("llm_deployment_routes")
    op.drop_index(
        "ix_llm_model_deployments_connection_enabled",
        table_name="llm_model_deployments",
    )
    op.drop_index(
        "ix_llm_model_deployments_connection_id",
        table_name="llm_model_deployments",
    )
    op.drop_table("llm_model_deployments")
    op.drop_index(
        "ix_llm_inference_connections_user_state",
        table_name="llm_inference_connections",
    )
    op.drop_index(
        "ix_llm_inference_connections_user_id",
        table_name="llm_inference_connections",
    )
    op.drop_table("llm_inference_connections")


def _add_identity_references(
    table_name: str,
    references: tuple[tuple[str, str], ...],
) -> None:
    """Add nullable indexed identity foreign keys in one table rebuild."""

    with op.batch_alter_table(table_name) as batch_op:
        for column_name, target in references:
            target_table, target_column = target.split(".", maxsplit=1)
            batch_op.add_column(
                sa.Column(
                    column_name,
                    _guid_type(),
                    nullable=True,
                )
            )
            batch_op.create_foreign_key(
                f"fk_{table_name}_{column_name}",
                target_table,
                [column_name],
                [target_column],
                ondelete="SET NULL",
            )
            batch_op.create_index(
                f"ix_{table_name}_{column_name}",
                [column_name],
                unique=False,
            )


def _drop_identity_references(
    table_name: str,
    column_names: tuple[str, ...],
) -> None:
    """Drop indexed identity foreign keys in one table rebuild."""

    with op.batch_alter_table(table_name) as batch_op:
        for column_name in column_names:
            batch_op.drop_index(f"ix_{table_name}_{column_name}")
            batch_op.drop_constraint(
                f"fk_{table_name}_{column_name}",
                type_="foreignkey",
            )
            batch_op.drop_column(column_name)
