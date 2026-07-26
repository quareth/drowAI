"""Phase 6 direct-cutover gate for canonical Knowledge production authority.

This module aggregates existing test-owned inventories without moving the
production ingestion authority or duplicating focused parity assertions.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from backend.tests.services.test_knowledge_ingestion_service import (
    EXPECTED_NON_TEST_STATISTICS_CONSUMERS,
    STATISTICS_DISPOSITION_INVENTORY,
    _production_paths_containing,
)
from backend.tests.services.test_knowledge_pentest_fact_bridge import (
    HISTORICAL_STRICT_ADMISSION_UNSUPPORTED_SHAPES,
    _producer_readiness_entries,
    assert_task5_all_current_adapters_have_exact_canonical_differential,
)
from backend.tests.services.test_knowledge_replay_source_resolver import (
    HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS,
    SUPPORTED_DURABLE_SNAPSHOT_AUDIT,
)


PHASE6_DIRECT_CUTOVER_CRITERIA = (
    "producer_readiness_fact_complete",
    "historical_audit_backfill_green",
    "differential_comparisons_green",
    "candidate_extraction_separate",
    "statistics_fact_mapping_approved",
    "direct_canonical_production_authority",
    "adapter_registry_has_no_production_consumers",
)


def _blocked_cutover_criteria(criteria: Mapping[str, bool]) -> tuple[str, ...]:
    assert tuple(criteria) == PHASE6_DIRECT_CUTOVER_CRITERIA
    return tuple(name for name in PHASE6_DIRECT_CUTOVER_CRITERIA if not criteria[name])


def _task5_differential_comparisons_green() -> bool:
    try:
        assert_task5_all_current_adapters_have_exact_canonical_differential()
    except AssertionError:
        return False
    return True


def _phase6_direct_cutover_criteria() -> dict[str, bool]:
    readiness_entries = _producer_readiness_entries()
    historical_dispositions = HISTORICAL_SEMANTIC_INPUT_BLOCKER_DISPOSITIONS
    build_observation_consumers = _production_paths_containing(
        "build_knowledge_observations"
    )
    registry_consumers = _production_paths_containing(
        "KnowledgeAdapterRegistryService"
    )

    return {
        "producer_readiness_fact_complete": bool(readiness_entries)
        and all(
            entry.status == "fact-complete" and entry.migration_ready
            for entry in readiness_entries
        ),
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
        )
        and set(HISTORICAL_STRICT_ADMISSION_UNSUPPORTED_SHAPES) == {
            "asset",
            "finding",
            "relationship",
        },
        "differential_comparisons_green": _task5_differential_comparisons_green(),
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
        and registry_consumers == ("backend/services/knowledge/adapter_registry.py",),
        "adapter_registry_has_no_production_consumers": "backend/services/knowledge/ingestion_service.py"
        not in registry_consumers,
    }


def test_phase6_direct_cutover_gate_is_green_after_ingestion_cutover() -> None:
    criteria = _phase6_direct_cutover_criteria()

    assert tuple(criteria) == PHASE6_DIRECT_CUTOVER_CRITERIA
    assert _blocked_cutover_criteria(criteria) == ()


@pytest.mark.parametrize("failed_criterion", PHASE6_DIRECT_CUTOVER_CRITERIA)
def test_phase6_direct_cutover_gate_fails_closed_for_any_failed_criterion(
    failed_criterion: str,
) -> None:
    criteria = {name: True for name in PHASE6_DIRECT_CUTOVER_CRITERIA}
    criteria[failed_criterion] = False

    assert _blocked_cutover_criteria(criteria) == (failed_criterion,)
