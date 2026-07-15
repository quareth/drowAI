"""Tests for CVE affected-product ORM schema and lookup index coverage."""

from __future__ import annotations

from backend.models.cve import CveAffectedProduct, CveIndexState, CveIndexSyncRun, CveRecord


def test_cve_affected_product_columns_cover_match_ready_projection_fields() -> None:
    columns = CveAffectedProduct.__table__.c

    assert "cve_record_id" in columns
    assert "cve_id" in columns
    assert "vendor_raw" in columns
    assert "vendor_norm" in columns
    assert "product_raw" in columns
    assert "product_norm" in columns
    assert "default_status" in columns
    assert "versions_json" in columns
    assert "cpes_json" in columns


def test_cve_affected_product_fk_targets_cve_records() -> None:
    foreign_keys = list(CveAffectedProduct.__table__.foreign_keys)

    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk.parent.name == "cve_record_id"
    assert fk.column.table.name == "cve_records"
    assert fk.column.name == "id"


def test_cve_affected_product_has_normalized_lookup_indexes() -> None:
    indexes = CveAffectedProduct.__table__.indexes
    by_name = {index.name: tuple(column.name for column in index.columns) for index in indexes}

    assert by_name["ix_cve_affected_products_vendor_norm"] == ("vendor_norm",)
    assert by_name["ix_cve_affected_products_product_norm"] == ("product_norm",)
    assert by_name["ix_cve_affected_products_vendor_product_norm"] == ("vendor_norm", "product_norm")


def test_cve_record_projection_state_columns_exist_for_durable_readiness() -> None:
    columns = CveRecord.__table__.c

    assert "projection_status" in columns
    assert "projection_affected_count" in columns
    assert "projection_error_code" in columns
    assert "projection_last_projected_at" in columns


def test_cve_record_projection_status_index_exists() -> None:
    indexes = CveRecord.__table__.indexes
    by_name = {index.name: tuple(column.name for column in index.columns) for index in indexes}

    assert by_name["ix_cve_records_projection_status"] == ("projection_status",)


def test_cve_sync_run_columns_include_phase_progress_markers() -> None:
    columns = CveIndexSyncRun.__table__.c

    assert "phase" in columns
    assert "progress_updated_at" in columns


def test_cve_index_state_columns_include_current_phase_progress_markers() -> None:
    columns = CveIndexState.__table__.c

    assert "current_phase" in columns
    assert "progress_updated_at" in columns
