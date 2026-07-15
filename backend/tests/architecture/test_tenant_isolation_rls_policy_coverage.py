"""Static tenant-isolation RLS policy coverage guardrail.

This module fails when any ORM table with a direct `tenant_id` column is not
covered by the tenant-isolation RLS policy migration inventory.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from backend.database import Base
import backend.models  # noqa: F401


_REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _REPO_ROOT / "backend" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / "0001_initial_current_schema.py"
_SPECIAL_POLICY_TABLES: set[str] = {"tenant_memberships", "semantic_memories"}


def _load_tenant_isolation_migration():
    spec = importlib.util.spec_from_file_location("tenant_isolation_rls_policies", _MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tenant_isolation_rls_policy_inventory_covers_all_direct_tenant_tables() -> None:
    migration = _load_tenant_isolation_migration()

    direct_tenant_tables = {
        table_name
        for table_name, table in Base.metadata.tables.items()
        if "tenant_id" in table.columns
    }
    covered_tables = set(migration.TENANT_POLICY_TABLES) | _SPECIAL_POLICY_TABLES

    missing = sorted(direct_tenant_tables - covered_tables)
    assert not missing, (
        "Tenant isolation RLS policy coverage is missing direct-tenant tables:\n  - "
        + "\n  - ".join(missing)
    )


def test_tenant_isolation_rls_special_policy_tables_are_declared_explicitly() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "tenant_isolation_tenant_memberships_user_lookup_read" in source
    assert "tenant_isolation_semantic_memories_scope" in source
