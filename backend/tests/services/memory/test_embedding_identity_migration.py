"""Validate semantic memory embedding identity migration behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "f1e2d3c4b5a6_add_semantic_memory_embedding_identity.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "semantic_memory_embedding_identity_migration",
        MIGRATION_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration_fn(module, connection, fn_name: str) -> None:
    context = MigrationContext.configure(connection)
    module.op = Operations(context)
    getattr(module, fn_name)()


def _create_legacy_semantic_memories(connection) -> Table:
    metadata = MetaData()
    table = Table(
        "semantic_memories",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("memory_tier", String(32), nullable=False),
        Column("scope_key", String(512), nullable=False),
        UniqueConstraint("scope_key", name="ux_semantic_memories_scope_key"),
    )
    metadata.create_all(connection)
    connection.execute(
        table.insert().values(
            id="memory-1",
            user_id=7,
            memory_tier="user_profile",
            scope_key="up:7:abc",
        )
    )
    return table


def test_embedding_identity_migration_backfills_existing_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            _create_legacy_semantic_memories(connection)
            module = _load_migration_module()

            _run_migration_fn(module, connection, "upgrade")

            inspector = inspect(connection)
            columns = {
                column["name"]
                for column in inspector.get_columns("semantic_memories")
            }
            assert {
                "embedding_provider",
                "embedding_model",
                "embedding_dimensions",
                "embedding_vector_family",
            }.issubset(columns)

            row = connection.execute(
                select(
                    Table(
                        "semantic_memories",
                        MetaData(),
                        autoload_with=connection,
                    )
                )
            ).mappings().one()
            assert row["embedding_provider"] == "openai"
            assert row["embedding_model"] == "text-embedding-3-small"
            assert row["embedding_dimensions"] == 1536
            assert (
                row["embedding_vector_family"]
                == "openai:text-embedding-3-small:1536"
            )

            unique_constraints = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(
                    "semantic_memories"
                )
            }
            assert "ux_semantic_memories_scope_key_identity" in unique_constraints
            assert "ux_semantic_memories_scope_key" not in unique_constraints

            indexes = {
                index["name"]
                for index in inspector.get_indexes("semantic_memories")
            }
            assert "ix_semantic_memories_embedding_identity" in indexes
    finally:
        engine.dispose()


def test_embedding_identity_migration_downgrade_rejects_duplicate_scope_keys() -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            _create_legacy_semantic_memories(connection)
            module = _load_migration_module()

            _run_migration_fn(module, connection, "upgrade")

            semantic_memories = Table(
                "semantic_memories",
                MetaData(),
                autoload_with=connection,
            )
            connection.execute(
                semantic_memories.insert().values(
                    id="memory-2",
                    user_id=7,
                    memory_tier="user_profile",
                    scope_key="up:7:abc",
                    embedding_provider="openai",
                    embedding_model="text-embedding-3-small-v2",
                    embedding_dimensions=1536,
                    embedding_vector_family="openai:text-embedding-3-small-v2:1536",
                )
            )

            with pytest.raises(RuntimeError, match="duplicate scope_key values"):
                _run_migration_fn(module, connection, "downgrade")
    finally:
        engine.dispose()
