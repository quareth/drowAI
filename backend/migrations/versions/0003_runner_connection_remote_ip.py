"""Record the peer IP observed for Runner control-channel connections.

Revision ID: 0003_runner_peer_ip
Revises: 0002_setup_state
Create Date: 2026-07-10 11:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_runner_peer_ip"
down_revision: Union[str, Sequence[str], None] = "0002_setup_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runner_connections",
        sa.Column("remote_ip_address", sa.String(length=45), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runner_connections", "remote_ip_address")
