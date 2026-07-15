"""Tests for reporting contract constants and validation helpers."""

from __future__ import annotations

import ast
from dataclasses import is_dataclass

import pytest

from backend.services.reporting import contracts


def test_reporting_contract_values_match_mvp_vocabulary() -> None:
    assert contracts.REPORT_TYPES == ("pentest", "vulnerability_assessment")
    assert contracts.MEMO_STATUSES == ("preparing", "ready", "failed")
    assert contracts.REPORT_STATUSES == ("generating", "ready", "failed")
    assert contracts.REPORT_JOB_STATUSES == (
        "queued",
        "generating",
        "ready",
        "failed",
        "cancelled",
    )
    assert contracts.MEMO_MODES == ("supported", "limited")
    assert contracts.INPUT_STATES == (
        "not_prepared",
        "preparing",
        "ready",
        "failed",
        "stale",
    )


def test_reporting_reason_codes_are_centralized() -> None:
    assert contracts.REPORTING_REASON_CODES == (
        contracts.REASON_TASK_NOT_STOPPED,
        contracts.REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
        contracts.REASON_NO_USEFUL_RUNTIME_EXECUTION,
        contracts.REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    )
    assert (
        contracts.REPORTING_CONTRACTS.reason_codes == contracts.REPORTING_REASON_CODES
    )


def test_task_closure_memo_contract_values_are_centralized() -> None:
    assert contracts.TASK_CLOSURE_MEMO_SCHEMA_VERSION == "task_closure_memo.v1"
    assert contracts.TASK_CLOSURE_MEMO_PROMPT_FAMILY == "task_closure_memo"
    assert contracts.TASK_CLOSURE_MEMO_PROMPT_TEMPLATE_IDS == (
        "task_closure_memo_system",
        "task_closure_memo_user",
    )
    assert (
        contracts.TASK_CLOSURE_MEMO_GENERATION_PURPOSE == "reporting.task_closure_memo"
    )
    assert contracts.TASK_CLOSURE_MEMO_CONTRACTS.schema_version == (
        contracts.TASK_CLOSURE_MEMO_SCHEMA_VERSION
    )
    assert contracts.TASK_CLOSURE_MEMO_CONTRACTS.prompt_template_ids == (
        contracts.TASK_CLOSURE_MEMO_PROMPT_TEMPLATE_IDS
    )


def test_engagement_report_generation_contract_values_are_centralized() -> None:
    assert contracts.ENGAGEMENT_REPORT_SCHEMA_VERSION == "engagement_report.v1"
    assert contracts.REPORT_SECTION_SCHEMA_VERSION == "report_section.v1"
    assert (
        contracts.ENGAGEMENT_REPORT_SECTION_PROMPT_FAMILY == "engagement_report_section"
    )
    assert contracts.REPORT_SECTION_STATUSES == ("ready", "needs_review", "failed")
    assert contracts.CURRENT_REPORT_SECTION_STATUSES == ("ready",)
    assert contracts.REPORT_SECTION_TYPES == (
        "narrative",
        "summary",
        "findings",
        "recommendations",
        "limitations",
        "appendix",
    )
    assert contracts.REPORT_SECTION_BLOCK_TYPES == (
        "finding",
        "evidence_note",
        "asset_note",
        "appendix_note",
    )
    assert contracts.ENGAGEMENT_REPORT_GENERATION_CONTRACTS.report_schema_version == (
        contracts.ENGAGEMENT_REPORT_SCHEMA_VERSION
    )
    assert contracts.ENGAGEMENT_REPORT_GENERATION_CONTRACTS.section_statuses == (
        contracts.REPORT_SECTION_STATUSES
    )
    assert (
        contracts.ENGAGEMENT_REPORT_GENERATION_CONTRACTS.current_report_section_statuses
        == contracts.CURRENT_REPORT_SECTION_STATUSES
    )


def test_engagement_report_generation_metadata_keys_are_safe_identifiers() -> None:
    assert contracts.ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS == (
        "section_plan_version",
        "renderer_version",
        "source_watermark_hash",
        "prompt_version",
        "validation_version",
        "provider",
        "model",
        "reasoning_effort",
        "usage",
        "duration_ms",
    )
    assert "prompt" not in contracts.ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS
    assert "model_output" not in contracts.ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS
    assert "evidence" not in contracts.ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS
    assert "transcript" not in contracts.ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS


def test_report_generation_service_error_reasons_are_centralized() -> None:
    assert contracts.REPORT_GENERATION_SERVICE_ERROR_REASONS == (
        contracts.REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
        contracts.REPORT_GENERATION_ERROR_INVALID_REQUEST,
        contracts.REPORT_GENERATION_ERROR_STALE_MEMO,
        contracts.REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
        contracts.REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE,
        contracts.REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        contracts.REPORT_GENERATION_ERROR_SECTION_TIMEOUT,
        contracts.REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED,
        contracts.REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
        contracts.REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
        contracts.REPORT_GENERATION_ERROR_JOB_CLAIM_CONFLICT,
        contracts.REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
    )
    assert contracts.ENGAGEMENT_REPORT_GENERATION_CONTRACTS.service_error_reasons == (
        contracts.REPORT_GENERATION_SERVICE_ERROR_REASONS
    )


def test_task_closure_memo_generation_metadata_keys_are_safe_identifiers() -> None:
    assert contracts.TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS == (
        "prompt_family",
        "prompt_version",
        "prompt_template_ids",
        "provider",
        "model",
        "reasoning_effort",
        "usage",
        "duration_ms",
        "memo_schema_version",
        "source_watermark_schema_version",
        "validation_version",
        "validation_status",
    )
    assert "prompt" not in contracts.TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS
    assert "model_output" not in contracts.TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS
    assert "evidence" not in contracts.TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS
    assert "transcript" not in contracts.TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS


def test_task_memo_service_error_reasons_are_centralized() -> None:
    assert contracts.TASK_MEMO_SERVICE_ERROR_REASONS == (
        contracts.TASK_MEMO_ERROR_TASK_NOT_FOUND,
        contracts.TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
        contracts.TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
        contracts.TASK_MEMO_ERROR_TASK_NOT_STOPPED,
        contracts.TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED,
        contracts.TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION,
        contracts.TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
        contracts.TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
        contracts.TASK_MEMO_ERROR_PROMPT_RENDER_FAILED,
        contracts.TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
        contracts.TASK_MEMO_ERROR_GENERATION_FAILED,
        contracts.TASK_MEMO_ERROR_VALIDATION_FAILED,
        contracts.TASK_MEMO_ERROR_PERSISTENCE_FAILED,
        contracts.TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
    )
    assert contracts.TASK_CLOSURE_MEMO_CONTRACTS.service_error_reasons == (
        contracts.TASK_MEMO_SERVICE_ERROR_REASONS
    )


def test_task_memo_contract_constants_are_static_values() -> None:
    uppercase_values = {
        name: getattr(contracts, name)
        for name in dir(contracts)
        if name.isupper()
        and (
            name.startswith("TASK_CLOSURE_MEMO")
            or name.startswith("TASK_MEMO")
            or name.startswith("GENERATION_METADATA")
        )
    }

    for value in uppercase_values.values():
        assert isinstance(value, (str, tuple)) or is_dataclass(value)

    assert is_dataclass(contracts.TaskClosureMemoContractValues)
    assert is_dataclass(contracts.TASK_CLOSURE_MEMO_CONTRACTS)
    assert is_dataclass(contracts.EngagementReportGenerationContractValues)
    assert is_dataclass(contracts.ENGAGEMENT_REPORT_GENERATION_CONTRACTS)


def test_task_memo_contract_constants_avoid_planning_terms() -> None:
    planning_token = "wa" + "ve"
    values = [
        str(getattr(contracts, name)).lower()
        for name in dir(contracts)
        if name.isupper()
        and (
            name.startswith("TASK_CLOSURE_MEMO")
            or name.startswith("TASK_MEMO")
            or name.startswith("GENERATION_METADATA")
        )
    ]

    assert all(planning_token not in value for value in values)


def test_report_generation_contract_constants_avoid_planning_terms() -> None:
    planning_token = "wa" + "ve"
    values = [
        str(getattr(contracts, name)).lower()
        for name in dir(contracts)
        if name.isupper()
        and (
            name.startswith("ENGAGEMENT_REPORT")
            or name.startswith("REPORT_GENERATION")
            or name.startswith("REPORT_SECTION")
            or name.startswith("CURRENT_REPORT_SECTION")
        )
    ]

    assert all(planning_token not in value for value in values)


@pytest.mark.parametrize(
    ("validator", "valid_value", "invalid_value"),
    [
        (contracts.validate_report_type, "pentest", "red_team"),
        (contracts.validate_memo_status, "ready", "queued"),
        (contracts.validate_report_status, "generating", "queued"),
        (contracts.validate_report_job_status, "cancelled", "preparing"),
        (contracts.validate_memo_mode, "limited", "full"),
        (contracts.validate_input_state, "stale", "cancelled"),
        (
            contracts.validate_reporting_reason_code,
            "runtime_retirement_not_confirmed",
            "unknown_reason",
        ),
        (
            contracts.validate_task_memo_service_error_reason,
            "memo_validation_failed",
            "raw_prompt_failed",
        ),
        (
            contracts.validate_report_section_status,
            "needs_review",
            "draft",
        ),
        (
            contracts.validate_current_report_section_status,
            "ready",
            "needs_review",
        ),
        (
            contracts.validate_report_section_type,
            "findings",
            "executive",
        ),
        (
            contracts.validate_report_section_block_type,
            "evidence_note",
            "chart",
        ),
        (
            contracts.validate_report_generation_service_error_reason,
            "section_validation_failed",
            "raw_prompt_failed",
        ),
    ],
)
def test_reporting_validators_fail_closed(
    validator: object,
    valid_value: str,
    invalid_value: str,
) -> None:
    assert validator(valid_value) == valid_value  # type: ignore[operator]
    with pytest.raises(ValueError):
        validator(invalid_value)  # type: ignore[operator]


def test_contracts_module_has_no_database_router_llm_or_worker_imports() -> None:
    source_path = contracts.__file__
    assert source_path is not None
    imports = [
        node.module or ""
        for node in ast.walk(ast.parse(open(source_path, encoding="utf-8").read()))
        if isinstance(node, ast.ImportFrom)
    ]
    imports.extend(
        alias.name
        for node in ast.walk(ast.parse(open(source_path, encoding="utf-8").read()))
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    forbidden_import_prefixes = (
        "sqlalchemy",
        "fastapi",
        "backend.database",
        "backend.routers",
        "backend.services.llm_provider",
        "backend.services.worker",
    )

    for imported_module in imports:
        assert not imported_module.startswith(forbidden_import_prefixes)
