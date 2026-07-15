"""API schemas for runner-control management and registration endpoints.

Scope:
- Defines request/response contracts for execution-site CRUD-lite, install token
  issuance, runner reads, credential revocation, and runner registration.

Boundaries:
- Schema contracts only; no persistence, auth, or registration orchestration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExecutionSiteCreateRequest(BaseModel):
    """Create contract for a tenant-scoped execution site."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128)
    network_label: str | None = Field(default=None, max_length=255)
    labels: dict[str, str] | None = None


class ExecutionSiteResponse(BaseModel):
    """Read model for one execution site."""

    id: UUID
    tenant_id: int
    name: str
    slug: str
    network_label: str | None = None
    status: str
    labels: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime


class RunnerSiteResponse(BaseModel):
    """Product-facing read model for a Runner Site without tenant internals."""

    id: UUID
    name: str
    slug: str
    network_label: str | None = None
    status: str
    connectivity_status: str = "waiting"
    runner_count: int = 0
    connected_runner_count: int = 0
    last_seen_at: datetime | None = None
    labels: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime


class RunnerReadinessResponse(BaseModel):
    """Product-facing tenant Runner readiness plus detailed reason codes."""

    status: str
    ready: bool
    reason_codes: list[str] = Field(default_factory=list)
    runner_site_count: int
    connected_runner_count: int
    evaluated_runner_count: int
    selected_runner_id: UUID | None = None
    execution_site_id: UUID | None = None


class RunnerEnrollmentCreateRequest(BaseModel):
    """Create a product enrollment for a Runner Site."""

    model_config = ConfigDict(extra="forbid")

    site_name: str = Field(min_length=1, max_length=255)
    site_slug: str | None = Field(default=None, min_length=1, max_length=128)
    management_url: str | None = Field(default=None, min_length=1, max_length=2048)
    tls_verify: bool = True
    allow_insecure_management_url: bool | None = None
    network_label: str | None = Field(default=None, max_length=255)
    labels: dict[str, str] | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class ManagementUrlResponse(BaseModel):
    """Canonical Runner-facing Management URL."""

    management_url: str
    source: str


class ManagementUrlUpdateRequest(BaseModel):
    """Persist the canonical Runner-facing Management URL."""

    model_config = ConfigDict(extra="forbid")

    management_url: str = Field(min_length=1, max_length=2048)


class RunnerEnrollmentCreateResponse(BaseModel):
    """Product response for one-time Runner enrollment material."""

    runner_site: RunnerSiteResponse
    enrollment_id: UUID
    expires_at: datetime
    status: str
    enrollment_toml: str = Field(
        description="One-time sensitive Runner enrollment material returned only by explicit admin/package flows."
    )
    package_name: str
    install_commands: list[str]


class InstallTokenCreateRequest(BaseModel):
    """Create contract for issuing a one-time runner install token."""

    model_config = ConfigDict(extra="forbid")

    execution_site_id: UUID
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class InstallTokenCreateResponse(BaseModel):
    """Response contract that returns install token plaintext exactly once."""

    install_token_id: UUID
    execution_site_id: UUID
    install_token: str = Field(
        description="One-time sensitive install token returned only by this explicit admin/API enrollment operation."
    )
    expires_at: datetime


class RunnerCredentialSummaryResponse(BaseModel):
    """Safe credential read model without secret hash material."""

    id: UUID
    credential_fingerprint: str
    status: str
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class RunnerListItemResponse(BaseModel):
    """Runner summary read model for tenant-scoped listing."""

    id: UUID
    execution_site_id: UUID
    name: str
    status: str
    version: str | None = None
    labels: dict[str, str] | list[str] | None = None
    capabilities: dict[str, str] | list[str] | None = None
    capacity: dict[str, Any] | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunnerDetailResponse(RunnerListItemResponse):
    """Runner detail read model including safe credential summaries."""

    tenant_id: int
    credentials: list[RunnerCredentialSummaryResponse]


class RunnerRevokeResponse(BaseModel):
    """Response contract for runner credential revocation."""

    runner_id: UUID
    revoked_credential_count: int


class RunnerRegistrationRequest(BaseModel):
    """Registration payload for exchanging install token for runner credentials."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: int | None = Field(default=None, ge=1)
    install_token: str = Field(min_length=1, max_length=512)
    runner_name: str = Field(min_length=1, max_length=255)
    runner_version: str | None = Field(default=None, max_length=64)
    labels: dict[str, str] | None = None
    capabilities: list[str] | dict[str, str] | None = None


class RunnerRegistrationResponse(BaseModel):
    """Registration response containing one-time credential material."""

    runner_id: UUID
    tenant_id: int
    credential_id: UUID
    credential_fingerprint: str
    credential_secret: str
    channel_endpoint: str
    protocol_version: str
    heartbeat_interval_seconds: int


class TaskRunnerAssignmentRequest(BaseModel):
    """Assignment request for choosing an eligible runner for one task."""

    model_config = ConfigDict(extra="forbid")

    execution_site_id: UUID | None = None
    required_protocol_version: str | None = Field(default=None, max_length=64)
    required_runtime_version: str | None = Field(default=None, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list)
    required_labels: dict[str, str] | None = None
    minimum_available_tasks: int = Field(default=1, ge=1, le=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    correlation_id: str | None = Field(default=None, max_length=255)
    payload_json: dict[str, Any] | None = None


class TaskRunnerAssignmentResponse(BaseModel):
    """Assignment response snapshot for Runner Control runtime-job creation."""

    runtime_job_id: UUID
    runtime_job_status: str
    task_id: int
    runner_id: UUID
    execution_site_id: UUID
    idempotency_key: str
    reason_codes: list[str] = Field(default_factory=list)


class RuntimeJobResponse(BaseModel):
    """Read model for tenant-bound runtime-job records."""

    id: UUID
    tenant_id: int
    task_id: int | None = None
    runner_id: UUID | None = None
    execution_site_id: UUID | None = None
    job_type: str
    status: str
    idempotency_key: str
    correlation_id: str | None = None
    payload_json: dict[str, Any] | None = None
    result_json: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
