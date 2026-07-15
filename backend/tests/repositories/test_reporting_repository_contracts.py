"""Contract tests for canonical focused reporting repositories.

This module freezes signatures and shared helpers by repository responsibility;
persistence behavior belongs to the focused behavior-test modules.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)
from backend.repositories.reporting.reporting_retention_repository import (
    ReportingRetentionRepository,
)
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)


EXPECTED_PARAMETERS_BY_TARGET = {
    "shared_base": {
        "__init__": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("db", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
        "_parse_uuid": (("value", "POSITIONAL_OR_KEYWORD", "<required>"),),
        "normalize_selected_memo_ids": (
            ("selected_task_memo_ids", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
        "_canonical_memo_id_strings": (
            ("selected_task_memo_ids", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
    },
    "memo": {
        "get_current_ready_memo": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "list_selected_current_ready_memos": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("selected_task_memo_ids", "KEYWORD_ONLY", "<required>"),
        ),
        "get_selected_memo_tasks": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("selected_task_memo_ids", "KEYWORD_ONLY", "<required>"),
        ),
        "get_task_for_memo_preparation": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_memo_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("memo_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_latest_memo_attempt": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_preparing_memo_attempt": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "mark_stale_preparing_memos_failed": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("stale_before", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
        ),
        "list_memo_history_for_task": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "50"),
            ("offset", "KEYWORD_ONLY", "0"),
        ),
        "next_memo_version": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "create_memo_attempt": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("created_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("version", "KEYWORD_ONLY", "<required>"),
            ("memo_mode", "KEYWORD_ONLY", "'supported'"),
            ("source_watermark", "KEYWORD_ONLY", "None"),
            ("memo", "KEYWORD_ONLY", "None"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
            ("status", "KEYWORD_ONLY", "'preparing'"),
            ("error_message", "KEYWORD_ONLY", "None"),
        ),
        "mark_memo_ready": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("memo_id", "KEYWORD_ONLY", "<required>"),
            ("memo", "KEYWORD_ONLY", "<required>"),
            ("source_watermark", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
            ("generated_at", "KEYWORD_ONLY", "None"),
            ("memo_mode", "KEYWORD_ONLY", "None"),
        ),
        "mark_memo_failed": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
            ("memo_id", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
            ("source_watermark", "KEYWORD_ONLY", "None"),
        ),
        "clear_current_ready_memos_for_task": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_id", "KEYWORD_ONLY", "<required>"),
        ),
        "list_memos_for_tasks": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("task_ids", "KEYWORD_ONLY", "<required>"),
            ("current_ready_only", "KEYWORD_ONLY", "False"),
        ),
    },
    "report": {
        "get_current_ready_report": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
        ),
        "get_report_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_report_by_id_for_owned_engagement": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
        ),
        "next_report_version": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
        ),
        "create_report_attempt": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("created_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("version", "KEYWORD_ONLY", "<required>"),
            ("title", "KEYWORD_ONLY", "<required>"),
            ("source_task_memo_ids", "KEYWORD_ONLY", "<required>"),
            ("engagement_name_snapshot", "KEYWORD_ONLY", "None"),
            ("engagement_status_snapshot", "KEYWORD_ONLY", "None"),
            ("sections", "KEYWORD_ONLY", "None"),
            ("source_knowledge_refs", "KEYWORD_ONLY", "None"),
            ("source_evidence_refs", "KEYWORD_ONLY", "None"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
            ("markdown_snapshot", "KEYWORD_ONLY", "None"),
            ("status", "KEYWORD_ONLY", "'generating'"),
            ("error_message", "KEYWORD_ONLY", "None"),
        ),
        "update_report_sections": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
            ("sections", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
        ),
        "merge_report_generation_metadata": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "<required>"),
        ),
        "mark_report_ready": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
            ("markdown_snapshot", "KEYWORD_ONLY", "<required>"),
            ("source_task_memo_ids", "KEYWORD_ONLY", "<required>"),
            ("source_knowledge_refs", "KEYWORD_ONLY", "<required>"),
            ("source_evidence_refs", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "<required>"),
            ("generated_at", "KEYWORD_ONLY", "None"),
        ),
        "mark_report_failed": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
            ("generation_metadata", "KEYWORD_ONLY", "None"),
        ),
        "clear_current_ready_reports_for_type": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
        ),
        "find_ready_current_report_by_source": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("selected_task_memo_ids", "KEYWORD_ONLY", "<required>"),
            ("source_watermark_hash", "KEYWORD_ONLY", "<required>"),
            ("llm_runtime_selection", "KEYWORD_ONLY", "None"),
        ),
        "list_report_history": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "None"),
            ("limit", "KEYWORD_ONLY", "50"),
            ("offset", "KEYWORD_ONLY", "0"),
        ),
        "list_report_library": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "None"),
            ("engagement_id", "KEYWORD_ONLY", "None"),
            ("query", "KEYWORD_ONLY", "None"),
            ("limit", "KEYWORD_ONLY", "50"),
            ("offset", "KEYWORD_ONLY", "0"),
        ),
        "count_report_library": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "None"),
            ("engagement_id", "KEYWORD_ONLY", "None"),
            ("query", "KEYWORD_ONLY", "None"),
        ),
        "_report_library_query": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("query", "KEYWORD_ONLY", "<required>"),
        ),
        "get_report_by_id_for_lifecycle": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
        ),
        "list_ready_reports_for_type": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("include_pending_deletion", "KEYWORD_ONLY", "False"),
        ),
        "schedule_report_deletion": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("report", "KEYWORD_ONLY", "<required>"),
            ("deleted_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("reason", "KEYWORD_ONLY", "<required>"),
            ("scheduled_at", "KEYWORD_ONLY", "<required>"),
            ("undo_until", "KEYWORD_ONLY", "<required>"),
            ("metadata", "KEYWORD_ONLY", "None"),
        ),
        "cancel_report_deletion": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("report", "KEYWORD_ONLY", "<required>"),
        ),
        "finalize_report_deletion": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("report", "KEYWORD_ONLY", "<required>"),
            ("finalized_at", "KEYWORD_ONLY", "<required>"),
        ),
    },
    "scoped_job": {
        "get_report_job": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_report_job_by_id_for_requester": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("requested_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
        ),
        "get_active_job_by_idempotency_key": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("requested_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("idempotency_key", "KEYWORD_ONLY", "<required>"),
        ),
        "get_active_report_job_for_requester": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("requested_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
        ),
        "create_report_job": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("requested_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("idempotency_key", "KEYWORD_ONLY", "<required>"),
            ("selected_task_memo_ids", "KEYWORD_ONLY", "<required>"),
            ("include_candidate_findings", "KEYWORD_ONLY", "<required>"),
            ("source_watermark", "KEYWORD_ONLY", "<required>"),
            ("llm_runtime_selection", "KEYWORD_ONLY", "None"),
            ("total_sections", "KEYWORD_ONLY", "0"),
            ("max_attempts", "KEYWORD_ONLY", "3"),
        ),
        "mark_report_job_ready": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
            ("finished_at", "KEYWORD_ONLY", "None"),
        ),
        "mark_report_job_failed": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
            ("last_error_code", "KEYWORD_ONLY", "None"),
            ("finished_at", "KEYWORD_ONLY", "None"),
        ),
        "update_report_job_progress": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("current_section_id", "KEYWORD_ONLY", "<required>"),
            ("completed_sections", "KEYWORD_ONLY", "<required>"),
            ("total_sections", "KEYWORD_ONLY", "<required>"),
        ),
        "_get_report_job_by_idempotency_key": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("requested_by_user_id", "KEYWORD_ONLY", "<required>"),
            ("engagement_id", "KEYWORD_ONLY", "<required>"),
            ("report_type", "KEYWORD_ONLY", "<required>"),
            ("idempotency_key", "KEYWORD_ONLY", "<required>"),
        ),
        "_retry_idempotency_key": (
            ("idempotency_key", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
        "_source_watermark_with_idempotency_key": (
            ("source_watermark", "KEYWORD_ONLY", "<required>"),
            ("idempotency_key", "KEYWORD_ONLY", "<required>"),
            ("original_idempotency_key", "KEYWORD_ONLY", "<required>"),
        ),
    },
    "worker_job": {
        "link_report_job_attempt_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("report_id", "KEYWORD_ONLY", "<required>"),
        ),
        "list_claimable_report_jobs": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("now", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "25"),
        ),
        "count_active_report_jobs": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "None"),
            ("user_id", "KEYWORD_ONLY", "None"),
        ),
        "acquire_report_job_claim_limit_lock": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("namespace_key", "KEYWORD_ONLY", "<required>"),
            ("claim_key", "KEYWORD_ONLY", "<required>"),
        ),
        "claim_report_job": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("worker_id", "KEYWORD_ONLY", "<required>"),
            ("claimed_at", "KEYWORD_ONLY", "<required>"),
            ("global_limit", "KEYWORD_ONLY", "None"),
            ("per_tenant_limit", "KEYWORD_ONLY", "None"),
            ("per_user_limit", "KEYWORD_ONLY", "None"),
        ),
        "_active_limit_filters": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("user_id", "KEYWORD_ONLY", "<required>"),
            ("global_limit", "KEYWORD_ONLY", "<required>"),
            ("per_tenant_limit", "KEYWORD_ONLY", "<required>"),
            ("per_user_limit", "KEYWORD_ONLY", "<required>"),
        ),
        "_active_job_count_subquery": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "None"),
            ("user_id", "KEYWORD_ONLY", "None"),
        ),
        "list_stale_generating_report_jobs": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("stale_before", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "100"),
        ),
        "requeue_stale_report_job": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("stale_before", "KEYWORD_ONLY", "<required>"),
        ),
        "requeue_report_job_after_failure_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("last_error_code", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
            ("next_attempt_at", "KEYWORD_ONLY", "<required>"),
            ("last_error_at", "KEYWORD_ONLY", "<required>"),
        ),
        "mark_report_job_failed_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("error_message", "KEYWORD_ONLY", "<required>"),
            ("last_error_code", "KEYWORD_ONLY", "None"),
            ("finished_at", "KEYWORD_ONLY", "None"),
        ),
        "update_report_job_progress_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
            ("current_section_id", "KEYWORD_ONLY", "<required>"),
            ("completed_sections", "KEYWORD_ONLY", "<required>"),
            ("total_sections", "KEYWORD_ONLY", "<required>"),
            ("generation_phase", "KEYWORD_ONLY", "None"),
            ("clear_error", "KEYWORD_ONLY", "False"),
        ),
        "get_report_job_by_id": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("job_id", "KEYWORD_ONLY", "<required>"),
        ),
    },
    "retention": {
        "list_reports_pending_deletion": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("now", "KEYWORD_ONLY", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "None"),
            ("limit", "KEYWORD_ONLY", "100"),
        ),
        "list_retention_candidate_reports": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("generated_before", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "100"),
        ),
        "count_retention_protected_current_reports": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("generated_before", "KEYWORD_ONLY", "<required>"),
        ),
        "list_retention_candidate_report_jobs": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("finished_before", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "100"),
        ),
        "delete_report_jobs": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("jobs", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
        "list_retention_candidate_task_memos": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("memo_before", "KEYWORD_ONLY", "<required>"),
            ("limit", "KEYWORD_ONLY", "100"),
        ),
        "count_retention_protected_current_task_memos": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("tenant_id", "KEYWORD_ONLY", "<required>"),
            ("memo_before", "KEYWORD_ONLY", "<required>"),
        ),
        "delete_task_memos": (
            ("self", "POSITIONAL_OR_KEYWORD", "<required>"),
            ("memos", "POSITIONAL_OR_KEYWORD", "<required>"),
        ),
    },
}


REPOSITORY_TARGETS = {
    "shared_base": ReportingRepositoryBase,
    "memo": TaskClosureMemoRepository,
    "report": EngagementReportRepository,
    "scoped_job": EngagementReportJobRepository,
    "worker_job": ReportJobWorkerRepository,
    "retention": ReportingRetentionRepository,
}


def _parameter_contract(
    repository_type: type, method_name: str
) -> tuple[tuple[str, str, str], ...]:
    signature = inspect.signature(getattr(repository_type, method_name))
    return tuple(
        (
            parameter.name,
            parameter.kind.name,
            "<required>"
            if parameter.default is inspect.Parameter.empty
            else repr(parameter.default),
        )
        for parameter in signature.parameters.values()
    )


def _cases(target: str) -> list[pytest.ParameterSet]:
    return [
        pytest.param(method_name, expected, id=method_name)
        for method_name, expected in EXPECTED_PARAMETERS_BY_TARGET[target].items()
    ]


@pytest.mark.parametrize("method_name, expected", _cases("shared_base"))
def test_shared_base_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert (
        _parameter_contract(REPOSITORY_TARGETS["shared_base"], method_name) == expected
    )


@pytest.mark.parametrize("method_name, expected", _cases("memo"))
def test_task_closure_memo_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert _parameter_contract(REPOSITORY_TARGETS["memo"], method_name) == expected


@pytest.mark.parametrize("method_name, expected", _cases("report"))
def test_engagement_report_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert _parameter_contract(REPOSITORY_TARGETS["report"], method_name) == expected


@pytest.mark.parametrize("method_name, expected", _cases("scoped_job"))
def test_engagement_report_job_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert (
        _parameter_contract(REPOSITORY_TARGETS["scoped_job"], method_name) == expected
    )


@pytest.mark.parametrize("method_name, expected", _cases("worker_job"))
def test_report_job_worker_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert (
        _parameter_contract(REPOSITORY_TARGETS["worker_job"], method_name) == expected
    )


@pytest.mark.parametrize("method_name, expected", _cases("retention"))
def test_reporting_retention_parameter_contract(
    method_name: str,
    expected: tuple[tuple[str, str, str], ...],
) -> None:
    assert _parameter_contract(REPOSITORY_TARGETS["retention"], method_name) == expected


def test_shared_base_normalizes_selected_memo_ids_and_reports_duplicates() -> None:
    first = uuid.UUID("00000000-0000-0000-0000-000000000001")
    second = uuid.UUID("00000000-0000-0000-0000-000000000002")

    normalized_ids, duplicate_ids = REPOSITORY_TARGETS[
        "shared_base"
    ].normalize_selected_memo_ids([str(second), first, second, "not-a-uuid"])

    assert normalized_ids == [second, first]
    assert duplicate_ids == [second]


def test_shared_base_uuid_and_canonical_id_helpers_preserve_legacy_behavior() -> None:
    first = uuid.UUID("00000000-0000-0000-0000-000000000001")
    second = uuid.UUID("00000000-0000-0000-0000-000000000002")

    assert REPOSITORY_TARGETS["shared_base"]._parse_uuid(str(first)) == first
    assert REPOSITORY_TARGETS["shared_base"]._parse_uuid("not-a-uuid") is None
    assert REPOSITORY_TARGETS["shared_base"]._canonical_memo_id_strings(
        [second, first, second, "not-a-uuid"]
    ) == [str(first), str(second)]
