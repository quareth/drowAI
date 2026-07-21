"""Move approved GPT-OSS routes to the agent-capable compatible dialect.

Revision ID: 0010_gpt_oss_agent_dialect
Revises: 0009_llm_legacy_retirement
Create Date: 2026-07-19 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_gpt_oss_agent_dialect"
down_revision: Union[str, Sequence[str], None] = "0009_llm_legacy_retirement"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APPROVED_PRESETS = (
    "gpt_oss_20b_openai_compatible_proving",
    "huggingface_openai_compatible_chat",
    "nvidia_nim_openai_compatible_chat",
    "ollama_openai_compatible_chat",
    "vllm_openai_compatible_chat",
)


def _update_dialect(*, source: str, target: str) -> None:
    """Update only approved GPT-OSS routes without touching custom endpoints."""

    preset_params = {
        f"preset_{index}": preset
        for index, preset in enumerate(_APPROVED_PRESETS)
    }
    placeholders = ", ".join(f":preset_{index}" for index in range(len(_APPROVED_PRESETS)))
    op.execute(
        sa.text(
            f"""
            UPDATE llm_deployment_routes
            SET dialect_policy_id = :target
            WHERE dialect_policy_id = :source
              AND deployment_id IN (
                  SELECT deployment.id
                  FROM llm_model_deployments AS deployment
                  JOIN llm_inference_connections AS connection
                    ON connection.id = deployment.connection_id
                  WHERE connection.connection_preset_id IN ({placeholders})
              )
            """
        ).bindparams(source=source, target=target, **preset_params)
    )


def upgrade() -> None:
    """Enable the registered agent dialect for approved GPT-OSS routes."""

    _update_dialect(
        source="openai_compatible_chat.conservative_v1",
        target="openai_compatible_chat.agent_v1",
    )


def downgrade() -> None:
    """Restore the conservative dialect for approved GPT-OSS routes."""

    _update_dialect(
        source="openai_compatible_chat.agent_v1",
        target="openai_compatible_chat.conservative_v1",
    )
