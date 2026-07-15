"""Pydantic schemas for tenant retention run APIs.

These contracts expose tenant-scoped dry-run/apply requests and count-only
retention summaries without candidate identifiers or raw payload fields.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.services.retention.contracts import (
    RETENTION_CLASSES,
    RetentionBatchCounts,
    RetentionClass,
    RetentionExecutorResult,
    RetentionRunResult,
    validate_retention_class,
)


PositiveRetentionLimit = Annotated[int, Field(ge=1)]


class _RetentionSchema(BaseModel):
    """Base model configuration for retention API schemas."""

    model_config = ConfigDict(extra="forbid")


class RetentionRunRequestBase(_RetentionSchema):
    """Shared optional filters for one tenant-scoped retention API run."""

    retention_classes: tuple[RetentionClass, ...] = RETENTION_CLASSES
    limit_per_tenant: PositiveRetentionLimit | None = None

    @field_validator("retention_classes")
    @classmethod
    def _validate_retention_classes(
        cls,
        value: tuple[RetentionClass, ...],
    ) -> tuple[RetentionClass, ...]:
        if not value:
            raise ValueError("retention_classes must not be empty")
        return tuple(
            validate_retention_class(retention_class) for retention_class in value
        )


class RetentionDryRunRequest(RetentionRunRequestBase):
    """Tenant-scoped dry-run request body."""


class RetentionApplyRequest(RetentionRunRequestBase):
    """Tenant-scoped apply request body requiring explicit confirmation."""

    confirm: bool


class RetentionBatchCountsResponse(_RetentionSchema):
    """Count-only retention batch summary."""

    scanned_count: int = 0
    candidate_count: int = 0
    protected_count: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    preserved_count: int = 0
    already_deleted_count: int = 0
    batch_count: int = 0
    batch_limit: int | None = None

    @classmethod
    def from_counts(
        cls,
        counts: RetentionBatchCounts,
    ) -> "RetentionBatchCountsResponse":
        """Build a count-only API response from service counts."""

        return cls(
            scanned_count=counts.scanned_count,
            candidate_count=counts.candidate_count,
            protected_count=counts.protected_count,
            applied_count=counts.applied_count,
            skipped_count=counts.skipped_count,
            failed_count=counts.failed_count,
            preserved_count=counts.preserved_count,
            already_deleted_count=counts.already_deleted_count,
            batch_count=counts.batch_count,
            batch_limit=counts.batch_limit,
        )


class RetentionExecutorSummaryResponse(_RetentionSchema):
    """Safe per-executor count summary for a retention run."""

    executor_name: str
    retention_class: RetentionClass
    mode: str
    tenant_id: int
    counts: RetentionBatchCountsResponse
    reason_counts: dict[str, int]
    succeeded: bool
    error_code: str | None = None

    @classmethod
    def from_result(
        cls,
        result: RetentionExecutorResult,
    ) -> "RetentionExecutorSummaryResponse":
        """Build a safe executor response without candidate decisions."""

        return cls(
            executor_name=result.executor_name,
            retention_class=result.retention_class,
            mode=result.mode,
            tenant_id=result.tenant_id,
            counts=RetentionBatchCountsResponse.from_counts(result.counts),
            reason_counts=dict(result.reason_counts),
            succeeded=result.succeeded,
            error_code=result.error_code,
        )


class RetentionRunResponse(_RetentionSchema):
    """Count-only aggregate response for a tenant retention run."""

    mode: str
    tenant_id: int
    succeeded: bool
    counts: RetentionBatchCountsResponse
    executor_results: tuple[RetentionExecutorSummaryResponse, ...]

    @classmethod
    def from_run_result(
        cls,
        result: RetentionRunResult,
    ) -> "RetentionRunResponse":
        """Build an API response from an orchestrator result."""

        return cls(
            mode=result.mode,
            tenant_id=int(result.tenant_id or 0),
            succeeded=result.succeeded,
            counts=_aggregate_counts(result.results),
            executor_results=tuple(
                RetentionExecutorSummaryResponse.from_result(executor_result)
                for executor_result in result.results
            ),
        )


def _aggregate_counts(
    results: tuple[RetentionExecutorResult, ...],
) -> RetentionBatchCountsResponse:
    return RetentionBatchCountsResponse(
        scanned_count=sum(result.counts.scanned_count for result in results),
        candidate_count=sum(result.counts.candidate_count for result in results),
        protected_count=sum(result.counts.protected_count for result in results),
        applied_count=sum(result.counts.applied_count for result in results),
        skipped_count=sum(result.counts.skipped_count for result in results),
        failed_count=sum(result.counts.failed_count for result in results),
        preserved_count=sum(result.counts.preserved_count for result in results),
        already_deleted_count=sum(
            result.counts.already_deleted_count for result in results
        ),
        batch_count=sum(result.counts.batch_count for result in results),
        batch_limit=sum(
            result.counts.batch_limit or 0
            for result in results
            if result.counts.batch_limit is not None
        )
        or None,
    )


__all__ = [
    "RetentionApplyRequest",
    "RetentionBatchCountsResponse",
    "RetentionDryRunRequest",
    "RetentionExecutorSummaryResponse",
    "RetentionRunRequestBase",
    "RetentionRunResponse",
]
