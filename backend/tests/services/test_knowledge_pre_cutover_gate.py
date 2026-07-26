"""Adapter-retirement gate for canonical Knowledge production authority.

This module aggregates existing test-owned inventories without moving the
production ingestion authority or duplicating focused parity assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from backend.tests.services.test_knowledge_ingestion_service import (
    EXPECTED_NON_TEST_STATISTICS_CONSUMERS,
    STATISTICS_DISPOSITION_INVENTORY,
    _production_paths_containing,
)
from backend.tests.services.test_knowledge_replay_source_resolver import (
    HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS,
    SUPPORTED_DURABLE_SNAPSHOT_AUDIT,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REGISTRY_PATH = REPO_ROOT / "backend/services/knowledge/adapter_registry.py"
ADAPTER_PACKAGE_ROOT = REPO_ROOT / "backend/services/knowledge/adapters"

PHASE7_ADAPTER_RETIREMENT_CRITERIA = (
    "historical_audit_backfill_green",
    "candidate_extraction_separate",
    "statistics_fact_mapping_approved",
    "direct_canonical_production_authority",
    "adapter_registry_absent",
    "adapter_package_absent",
)


def _blocked_cutover_criteria(criteria: Mapping[str, bool]) -> tuple[str, ...]:
    assert tuple(criteria) == PHASE7_ADAPTER_RETIREMENT_CRITERIA
    return tuple(name for name in PHASE7_ADAPTER_RETIREMENT_CRITERIA if not criteria[name])


def _phase7_adapter_retirement_criteria() -> dict[str, bool]:
    historical_dispositions = HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS
    build_observation_consumers = _production_paths_containing(
        "build_knowledge_observations"
    )
    registry_consumers = _production_paths_containing(
        "Knowledge" + "Adapter" + "Registry" + "Service"
    )

    return {
        "historical_audit_backfill_green": set(historical_dispositions) == {
            "malformed_nmap_host_ip",
            "nuclei_malformed_or_empty_evidence_refs",
            "amass_relationship_payload_subject_mismatch",
        }
        and all(
            disposition["outcome"] == "no_supported_persisted_input_contains_blocker"
            and disposition["selected_source_execution_ids"] == ()
            and disposition["audit_evidence"]
            == SUPPORTED_DURABLE_SNAPSHOT_AUDIT["evidence"]
            for disposition in historical_dispositions.values()
        ),
        "candidate_extraction_separate": all(
            "candidate_extraction" not in path
            for path in registry_consumers
        ),
        "statistics_fact_mapping_approved": set(STATISTICS_DISPOSITION_INVENTORY)
        == {
            "preserve_run_result",
            "preserve_run_metadata",
            "preserve_candidate_policy",
            "retire_dispatch_only",
            "safe_failure_metadata",
        }
        and all(
            _production_paths_containing(field_name) == expected_paths
            for field_name, expected_paths in EXPECTED_NON_TEST_STATISTICS_CONSUMERS.items()
        ),
        "direct_canonical_production_authority": build_observation_consumers
        == (
            "backend/services/knowledge/ingestion_service.py",
            "backend/services/knowledge/pentest_facts/__init__.py",
            "backend/services/knowledge/pentest_facts/bridge.py",
        )
        and registry_consumers == (),
        "adapter_registry_absent": not ADAPTER_REGISTRY_PATH.exists(),
        "adapter_package_absent": not ADAPTER_PACKAGE_ROOT.exists(),
    }


def test_phase7_adapter_retirement_gate_is_green_after_production_deletion() -> None:
    criteria = _phase7_adapter_retirement_criteria()

    assert tuple(criteria) == PHASE7_ADAPTER_RETIREMENT_CRITERIA
    assert _blocked_cutover_criteria(criteria) == ()


@pytest.mark.parametrize("failed_criterion", PHASE7_ADAPTER_RETIREMENT_CRITERIA)
def test_phase7_adapter_retirement_gate_fails_closed_for_any_failed_criterion(
    failed_criterion: str,
) -> None:
    criteria = {name: True for name in PHASE7_ADAPTER_RETIREMENT_CRITERIA}
    criteria[failed_criterion] = False

    assert _blocked_cutover_criteria(criteria) == (failed_criterion,)
