"""Snapshot remote conversation lifecycle origins.

Revision ID: 0008_llm_conversation_origin
Revises: 0007_llm_deployment_backfill
Create Date: 2026-07-18 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_llm_conversation_origin"
down_revision: Union[str, Sequence[str], None] = "0007_llm_deployment_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable origin snapshots without guessing legacy row identities."""

    with op.batch_alter_table("llm_conversations") as batch_op:
        batch_op.add_column(sa.Column("origin_revision", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("origin_deployment_revision", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("remote_resource_id", sa.String(length=256), nullable=True)
        )
        batch_op.create_index(
            "ix_llm_conversations_remote_resource_id",
            ["remote_resource_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove remote origin snapshots in reverse order."""

    with op.batch_alter_table("llm_conversations") as batch_op:
        batch_op.drop_index("ix_llm_conversations_remote_resource_id")
        batch_op.drop_column("remote_resource_id")
        batch_op.drop_column("origin_deployment_revision")
        batch_op.drop_column("origin_revision")
