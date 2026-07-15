"""Purge legacy retired Runner Sites and their operational runtime jobs.

Revision ID: 0004_purge_retired_sites
Revises: 0003_runner_peer_ip
Create Date: 2026-07-10 12:00:00.000000+00:00

This one-way data migration removes obsolete runner-control registry state.
Durable data-plane foreign keys use ``ON DELETE SET NULL`` and survive.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_purge_retired_sites"
down_revision: Union[str, Sequence[str], None] = "0003_runner_peer_ip"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    retired_sites = "SELECT id FROM execution_sites WHERE status = 'retired'"
    retired_runners = (
        "SELECT runners.id FROM runners JOIN execution_sites "
        "ON execution_sites.id = runners.execution_site_id "
        "WHERE execution_sites.status = 'retired'"
    )
    retired_jobs = (
        "SELECT id FROM runtime_jobs "
        f"WHERE execution_site_id IN ({retired_sites}) OR runner_id IN ({retired_runners})"
    )

    for table_name in ("artifact_manifests", "execution_artifacts"):
        op.execute(f"UPDATE {table_name} SET runtime_job_id = NULL WHERE runtime_job_id IN ({retired_jobs})")
        op.execute(f"UPDATE {table_name} SET runner_id = NULL WHERE runner_id IN ({retired_runners})")
    op.execute(f"UPDATE tool_executions SET runtime_job_id = NULL WHERE runtime_job_id IN ({retired_jobs})")
    op.execute(f"UPDATE tool_executions SET runner_id = NULL WHERE runner_id IN ({retired_runners})")
    op.execute(f"UPDATE tool_executions SET execution_site_id = NULL WHERE execution_site_id IN ({retired_sites})")
    op.execute(
        f"DELETE FROM runner_control_messages "
        f"WHERE runner_id IN ({retired_runners}) OR runtime_job_id IN ({retired_jobs})"
    )
    op.execute(
        f"DELETE FROM runtime_jobs WHERE id IN ({retired_jobs})"
    )
    op.execute(f"DELETE FROM runner_connections WHERE runner_id IN ({retired_runners})")
    op.execute(f"DELETE FROM runner_credentials WHERE runner_id IN ({retired_runners})")
    op.execute(f"DELETE FROM runners WHERE id IN ({retired_runners})")
    op.execute(f"DELETE FROM runner_install_tokens WHERE execution_site_id IN ({retired_sites})")
    op.execute(f"DELETE FROM execution_sites WHERE id IN ({retired_sites})")


def downgrade() -> None:
    """Deleted registry state is intentionally not recreated."""
