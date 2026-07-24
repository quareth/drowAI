"""Consolidate LLM connectors and enforce one connector per user and preset.

Revision ID: 0012_llm_connector_singletons
Revises: 0011_connection_credentials
Create Date: 2026-07-24 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_llm_connector_singletons"
down_revision: Union[str, Sequence[str], None] = "0011_connection_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEPLOYMENT_REFERENCES = (
    ("user_llm_selections", "deployment_id"),
    ("user_reporting_llm_selections", "deployment_id"),
    ("user_memory_llm_selections", "gate_deployment_id"),
    ("user_memory_llm_selections", "extraction_deployment_id"),
    ("llm_conversations", "deployment_id"),
    ("llm_usage_records", "deployment_id"),
)
_CONNECTION_REFERENCES = (
    ("llm_conversations", "connection_id"),
    ("llm_usage_records", "connection_id"),
)


def upgrade() -> None:
    """Merge existing duplicates, then add storage-level singleton guards."""

    bind = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(bind=bind)
    _consolidate_provider_credentials(bind, metadata)
    _consolidate_connections(bind, metadata)
    with op.batch_alter_table("user_llm_provider_credentials") as batch:
        batch.create_unique_constraint(
            "uq_user_llm_provider_credentials_user_provider",
            ["user_id", "provider"],
        )
    with op.batch_alter_table("llm_inference_connections") as batch:
        batch.create_unique_constraint(
            "uq_llm_inference_connections_user_preset",
            ["user_id", "connection_preset_id"],
        )


def downgrade() -> None:
    """Remove singleton guards without recreating discarded duplicates."""

    with op.batch_alter_table("llm_inference_connections") as batch:
        batch.drop_constraint(
            "uq_llm_inference_connections_user_preset",
            type_="unique",
        )
    with op.batch_alter_table("user_llm_provider_credentials") as batch:
        batch.drop_constraint(
            "uq_user_llm_provider_credentials_user_provider",
            type_="unique",
        )


def _consolidate_provider_credentials(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    table = metadata.tables["user_llm_provider_credentials"]
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(sa.select(table)).mappings():
        grouped[(int(row["user_id"]), str(row["provider"]))].append(dict(row))
    for rows in grouped.values():
        if len(rows) < 2:
            continue
        survivor = max(rows, key=_credential_score)
        duplicate_ids = [row["id"] for row in rows if row["id"] != survivor["id"]]
        bind.execute(sa.delete(table).where(table.c.id.in_(duplicate_ids)))


def _consolidate_connections(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    connections = metadata.tables["llm_inference_connections"]
    deployments = metadata.tables["llm_model_deployments"]
    credentials = metadata.tables["llm_connection_credentials"]
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(sa.select(connections)).mappings():
        grouped[
            (int(row["user_id"]), str(row["connection_preset_id"]))
        ].append(dict(row))

    for rows in grouped.values():
        if len(rows) < 2:
            continue
        references = {
            row["id"]: _connection_reference_count(
                bind,
                metadata,
                deployments,
                row["id"],
            )
            for row in rows
        }
        survivor = max(
            rows,
            key=lambda row: (
                references[row["id"]],
                int(row["state"] == "enabled"),
                row["updated_at"] or row["created_at"],
                str(row["id"]),
            ),
        )
        survivor_id = survivor["id"]
        loser_ids = [row["id"] for row in rows if row["id"] != survivor_id]
        _move_latest_connection_credential(
            bind,
            credentials,
            survivor_id=survivor_id,
            connection_ids=[row["id"] for row in rows],
        )
        for loser_id in loser_ids:
            _merge_connection_deployments(
                bind,
                metadata,
                deployments,
                survivor_id=survivor_id,
                loser_id=loser_id,
            )
            _replace_references(
                bind,
                metadata,
                _CONNECTION_REFERENCES,
                old_id=loser_id,
                new_id=survivor_id,
            )
        bind.execute(sa.delete(connections).where(connections.c.id.in_(loser_ids)))


def _connection_reference_count(
    bind: sa.Connection,
    metadata: sa.MetaData,
    deployments: sa.Table,
    connection_id: Any,
) -> int:
    deployment_ids = tuple(
        bind.execute(
            sa.select(deployments.c.id).where(
                deployments.c.connection_id == connection_id
            )
        ).scalars()
    )
    count = 0
    for table_name, column_name in _CONNECTION_REFERENCES:
        table = metadata.tables.get(table_name)
        if table is not None:
            count += int(
                bind.execute(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(table.c[column_name] == connection_id)
                ).scalar_one()
            )
    if deployment_ids:
        for table_name, column_name in _DEPLOYMENT_REFERENCES:
            table = metadata.tables.get(table_name)
            if table is not None:
                count += int(
                    bind.execute(
                        sa.select(sa.func.count())
                        .select_from(table)
                        .where(table.c[column_name].in_(deployment_ids))
                    ).scalar_one()
                )
    return count


def _move_latest_connection_credential(
    bind: sa.Connection,
    credentials: sa.Table,
    *,
    survivor_id: Any,
    connection_ids: list[Any],
) -> None:
    rows = [
        dict(row)
        for row in bind.execute(
            sa.select(credentials).where(
                credentials.c.connection_id.in_(connection_ids)
            )
        ).mappings()
    ]
    if not rows:
        return
    latest = max(rows, key=_credential_score)
    survivor = next(
        (row for row in rows if row["connection_id"] == survivor_id),
        None,
    )
    values = {
        "encrypted_api_key": latest["encrypted_api_key"],
        "enabled": latest["enabled"],
        "updated_at": latest["updated_at"],
    }
    if survivor is None:
        bind.execute(
            sa.insert(credentials).values(
                connection_id=survivor_id,
                created_at=latest["created_at"],
                **values,
            )
        )
    else:
        bind.execute(
            sa.update(credentials)
            .where(credentials.c.connection_id == survivor_id)
            .values(**values)
        )


def _merge_connection_deployments(
    bind: sa.Connection,
    metadata: sa.MetaData,
    deployments: sa.Table,
    *,
    survivor_id: Any,
    loser_id: Any,
) -> None:
    survivor_by_wire = {
        row["wire_model_id"]: row["id"]
        for row in bind.execute(
            sa.select(deployments).where(
                deployments.c.connection_id == survivor_id
            )
        ).mappings()
    }
    loser_rows = list(
        bind.execute(
            sa.select(deployments).where(deployments.c.connection_id == loser_id)
        ).mappings()
    )
    for row in loser_rows:
        target_id = survivor_by_wire.get(row["wire_model_id"])
        if target_id is None:
            bind.execute(
                sa.update(deployments)
                .where(deployments.c.id == row["id"])
                .values(connection_id=survivor_id)
            )
            survivor_by_wire[row["wire_model_id"]] = row["id"]
            continue
        _replace_references(
            bind,
            metadata,
            _DEPLOYMENT_REFERENCES,
            old_id=row["id"],
            new_id=target_id,
        )
        bind.execute(sa.delete(deployments).where(deployments.c.id == row["id"]))


def _replace_references(
    bind: sa.Connection,
    metadata: sa.MetaData,
    references: tuple[tuple[str, str], ...],
    *,
    old_id: Any,
    new_id: Any,
) -> None:
    for table_name, column_name in references:
        table = metadata.tables.get(table_name)
        if table is None:
            continue
        bind.execute(
            sa.update(table)
            .where(table.c[column_name] == old_id)
            .values({column_name: new_id})
        )


def _credential_score(row: dict[str, Any]) -> tuple[int, int, Any, str]:
    return (
        int(bool(row.get("enabled"))),
        int(bool(str(row.get("encrypted_api_key") or "").strip())),
        row.get("updated_at") or row.get("created_at"),
        str(row.get("id") or row.get("connection_id")),
    )
