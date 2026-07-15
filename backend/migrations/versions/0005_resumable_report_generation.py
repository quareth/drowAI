"""Add durable phase and retry scheduling state to report jobs.

Revision ID: 0005_resumable_reports
Revises: 0004_purge_retired_sites
Create Date: 2026-07-10 13:30:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_resumable_reports"
down_revision: Union[str, Sequence[str], None] = "0004_purge_retired_sites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "engagement_report_jobs",
        sa.Column(
            "generation_phase",
            sa.String(length=32),
            nullable=False,
            server_default="sections",
        ),
    )
    op.add_column(
        "engagement_report_jobs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "engagement_report_jobs",
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_engagement_report_jobs_status_next_attempt_created",
        "engagement_report_jobs",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_engagement_report_jobs_status_next_attempt_created",
        table_name="engagement_report_jobs",
    )
    op.drop_column("engagement_report_jobs", "last_error_at")
    op.drop_column("engagement_report_jobs", "next_attempt_at")
    op.drop_column("engagement_report_jobs", "generation_phase")
