"""Retire legacy OpenAI settings mirrors and provider credential uniqueness.

Revision ID: 0009_llm_legacy_retirement
Revises: 0008_llm_conversation_origin
Create Date: 2026-07-19 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_llm_legacy_retirement"
down_revision: Union[str, Sequence[str], None] = "0008_llm_conversation_origin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove retired text-LLM mirrors and allow duplicate provider rows."""

    with op.batch_alter_table("user_llm_provider_credentials") as batch_op:
        batch_op.drop_constraint(
            "uq_user_llm_provider_credentials_user_provider",
            type_="unique",
        )

    with op.batch_alter_table("user_settings") as batch_op:
        batch_op.drop_column("openai_api_key")
        batch_op.drop_column("openai_model")
        batch_op.drop_column("enable_ai")


def downgrade() -> None:
    """Restore legacy columns and uniqueness for local downgrade workflows."""

    with op.batch_alter_table("user_settings") as batch_op:
        batch_op.add_column(sa.Column("enable_ai", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("openai_model", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("openai_api_key", sa.Text(), nullable=True))

    with op.batch_alter_table("user_llm_provider_credentials") as batch_op:
        batch_op.create_unique_constraint(
            "uq_user_llm_provider_credentials_user_provider",
            ["user_id", "provider"],
        )
