"""Add platform installation provisioning state.

Revision ID: 0002_setup_state
Revises: 0001_initial_current_schema
Create Date: 2026-07-08 16:30:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_setup_state"
down_revision: Union[str, Sequence[str], None] = "0001_initial_current_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_installations",
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
    )
    op.add_column("platform_installations", sa.Column("setup_error", sa.Text(), nullable=True))
    op.add_column("platform_installations", sa.Column("provisioning_metadata", sa.JSON(), nullable=True))
    op.create_index(
        op.f("ix_platform_installations_status"),
        "platform_installations",
        ["status"],
        unique=False,
    )
    op.execute(
        "UPDATE platform_installations "
        "SET status = CASE WHEN completed_at IS NULL THEN 'pending' ELSE 'complete' END"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_platform_installations_status"), table_name="platform_installations")
    op.drop_column("platform_installations", "provisioning_metadata")
    op.drop_column("platform_installations", "setup_error")
    op.drop_column("platform_installations", "status")
