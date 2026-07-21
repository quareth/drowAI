"""Store managed LLM credentials at connection granularity.

Revision ID: 0011_connection_credentials
Revises: 0010_gpt_oss_agent_dialect
Create Date: 2026-07-21 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_connection_credentials"
down_revision: Union[str, Sequence[str], None] = "0010_gpt_oss_agent_dialect"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _guid_type() -> sa.types.TypeEngine:
    """Return the portable UUID type without importing application modules."""

    return sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


def upgrade() -> None:
    """Create one encrypted credential row per managed LLM connection."""

    op.create_table(
        "llm_connection_credentials",
        sa.Column("connection_id", _guid_type(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
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
            ["connection_id"],
            ["llm_inference_connections.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id"),
    )


def downgrade() -> None:
    """Remove connection-owned credential storage."""

    op.drop_table("llm_connection_credentials")
