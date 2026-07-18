"""Backfill deterministic legacy-default LLM deployment identities.

Revision ID: 0007_llm_deployment_backfill
Revises: 0006_llm_deployment_identity
Create Date: 2026-07-18 00:00:00.000000+00:00
"""

from collections import defaultdict
from typing import Any, Sequence, Union
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op


revision: str = "0007_llm_deployment_backfill"
down_revision: Union[str, Sequence[str], None] = "0006_llm_deployment_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAMESPACE = UUID("155b4c21-9f15-4c52-bfec-7fbf407bc63d")
_SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic"})
_RUNTIME_FAMILIES = {
    "openai": "openai_native",
    "anthropic": "anthropic_native",
}


def upgrade() -> None:
    """Map legacy credentials and exact text selections without substitution."""

    bind = op.get_bind()
    metadata = sa.MetaData()
    tables = {
        name: sa.Table(name, metadata, autoload_with=bind)
        for name in (
            "user_settings",
            "user_llm_provider_credentials",
            "user_llm_selections",
            "user_reporting_llm_selections",
            "user_memory_llm_selections",
            "llm_inference_connections",
            "llm_model_deployments",
        )
    }
    _copy_legacy_openai_inputs(bind, tables)
    connections = _ensure_legacy_connections(bind, tables)
    _map_selection_deployments(bind, tables, connections)


def downgrade() -> None:
    """Retain additive identity data so rollback never deletes user mappings."""


def _copy_legacy_openai_inputs(
    bind: sa.Connection,
    tables: dict[str, sa.Table],
) -> None:
    settings_table = tables["user_settings"]
    credential_table = tables["user_llm_provider_credentials"]
    selection_table = tables["user_llm_selections"]
    settings_rows = bind.execute(
        sa.select(settings_table).order_by(settings_table.c.user_id)
    ).mappings()
    for settings in settings_rows:
        user_id = int(settings["user_id"])
        ciphertext = settings.get("openai_api_key")
        existing_credential = bind.execute(
            sa.select(credential_table.c.id).where(
                credential_table.c.user_id == user_id,
                sa.func.lower(credential_table.c.provider) == "openai",
            ).limit(1)
        ).scalar_one_or_none()
        if (
            existing_credential is None
            and isinstance(ciphertext, str)
            and ciphertext.strip()
        ):
            bind.execute(
                credential_table.insert().values(
                    user_id=user_id,
                    provider="openai",
                    encrypted_api_key=ciphertext,
                    enabled=True,
                )
            )

        model = settings.get("openai_model")
        existing_selection = bind.execute(
            sa.select(selection_table.c.id).where(
                selection_table.c.user_id == user_id
            )
        ).scalar_one_or_none()
        if (
            existing_selection is None
            and isinstance(model, str)
            and model.strip()
        ):
            bind.execute(
                selection_table.insert().values(
                    user_id=user_id,
                    provider="openai",
                    model=model.strip(),
                )
            )


def _ensure_legacy_connections(
    bind: sa.Connection,
    tables: dict[str, sa.Table],
) -> dict[tuple[int, str], Any]:
    credential_table = tables["user_llm_provider_credentials"]
    connection_table = tables["llm_inference_connections"]
    credentials: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    rows = bind.execute(
        sa.select(credential_table).order_by(
            credential_table.c.user_id,
            credential_table.c.provider,
            credential_table.c.id,
        )
    ).mappings()
    for row in rows:
        provider = _provider(row["provider"])
        if provider is not None:
            credentials[(int(row["user_id"]), provider)].append(dict(row))

    connections: dict[tuple[int, str], Any] = {}
    for (user_id, provider), provider_credentials in sorted(credentials.items()):
        connection_id = bind.execute(
            sa.select(connection_table.c.id).where(
                connection_table.c.user_id == user_id,
                connection_table.c.legacy_default_provider == provider,
            )
        ).scalar_one_or_none()
        if connection_id is None:
            stable_id = uuid5(
                _NAMESPACE,
                f"legacy-connection:{user_id}:{provider}",
            )
            usable = any(
                bool(row.get("enabled"))
                and bool(str(row.get("encrypted_api_key") or "").strip())
                for row in provider_credentials
            )
            connection_id = _guid_value(bind, stable_id)
            bind.execute(
                connection_table.insert().values(
                    id=connection_id,
                    user_id=user_id,
                    display_name=f"Legacy {provider.title()}",
                    connection_preset_id=provider,
                    runtime_family_id=_RUNTIME_FAMILIES[provider],
                    serving_operator_id=provider,
                    transport_origin="backend",
                    endpoint_policy_id="fixed_provider_v1",
                    config_schema_version=1,
                    state="enabled" if usable else "disabled",
                    revision=1,
                    legacy_default_provider=provider,
                )
            )
        connections[(user_id, provider)] = connection_id
    return connections


def _map_selection_deployments(
    bind: sa.Connection,
    tables: dict[str, sa.Table],
    connections: dict[tuple[int, str], Any],
) -> None:
    deployment_table = tables["llm_model_deployments"]
    targets: list[tuple[sa.Table, Any, str, int, str, str]] = []
    for table_name in (
        "user_llm_selections",
        "user_reporting_llm_selections",
    ):
        table = tables[table_name]
        for row in bind.execute(sa.select(table).order_by(table.c.id)).mappings():
            targets.append(
                (
                    table,
                    row["id"],
                    "deployment_id",
                    int(row["user_id"]),
                    str(row["provider"]),
                    str(row["model"]),
                )
            )
    memory_table = tables["user_memory_llm_selections"]
    for row in bind.execute(
        sa.select(memory_table).order_by(memory_table.c.id)
    ).mappings():
        targets.extend(
            (
                (
                    memory_table,
                    row["id"],
                    "gate_deployment_id",
                    int(row["user_id"]),
                    str(row["provider"]),
                    str(row["gate_model"]),
                ),
                (
                    memory_table,
                    row["id"],
                    "extraction_deployment_id",
                    int(row["user_id"]),
                    str(row["provider"]),
                    str(row["extraction_model"]),
                ),
            )
        )

    for table, row_id, field_name, user_id, raw_provider, wire_model_id in targets:
        current_ref = bind.execute(
            sa.select(getattr(table.c, field_name)).where(table.c.id == row_id)
        ).scalar_one()
        if current_ref is not None:
            continue
        provider = _provider(raw_provider)
        connection_id = connections.get((user_id, provider or ""))
        if connection_id is None or not wire_model_id.strip():
            continue
        deployment_id = bind.execute(
            sa.select(deployment_table.c.id).where(
                deployment_table.c.connection_id == connection_id,
                deployment_table.c.wire_model_id == wire_model_id,
            )
        ).scalar_one_or_none()
        if deployment_id is None:
            stable_id = uuid5(
                _NAMESPACE,
                (
                    f"legacy-deployment:{connection_id}:"
                    f"{wire_model_id}"
                ),
            )
            deployment_id = _guid_value(bind, stable_id)
            bind.execute(
                deployment_table.insert().values(
                    id=deployment_id,
                    connection_id=connection_id,
                    wire_model_id=wire_model_id,
                    display_name=wire_model_id,
                    discovery_source="legacy_backfill",
                    lifecycle_state="active",
                    availability_state="unknown",
                    enabled=True,
                    revision=1,
                )
            )
        bind.execute(
            table.update()
            .where(table.c.id == row_id)
            .values({field_name: deployment_id})
        )


def _provider(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in _SUPPORTED_PROVIDERS else None


def _guid_value(bind: sa.Connection, value: UUID) -> UUID | str:
    return value if bind.dialect.name == "postgresql" else str(value)
