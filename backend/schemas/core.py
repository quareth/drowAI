"""Core Pydantic request/response schemas shared across backend APIs.

Scope:
- Defines user, task, history, agent log, and report API contracts.
- Provides the canonical schema location for core app domains.

Boundaries:
- API schema definitions only; no ORM models, persistence, or service logic.
- Backward-compatible re-exports remain in `backend.models` during parallel run.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UserBase(BaseModel):
    username: str
    email: Optional[str] = None


class UserCreate(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class UserSettingsBase(BaseModel):
    shodan_api_key: Optional[str] = None
    session_timeout: int = 1800
    theme: str = "dark"
    timezone: str = "UTC"


class UserSettingsCreate(UserSettingsBase):
    pass


class UserSettingsUpdate(BaseModel):
    shodan_api_key: Optional[str] = None
    session_timeout: Optional[int] = None
    theme: Optional[str] = None
    timezone: Optional[str] = None


class UserSettingsResponse(UserSettingsBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserResponse(UserBase):
    id: int
    created_at: datetime
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class EffectivePermissionsResponse(BaseModel):
    actions: list[str] = Field(default_factory=list)
    role: str
    tenant_id: int
    policy_version: str


class TenantMembershipSummaryResponse(BaseModel):
    membership_id: int
    tenant_id: int
    tenant_slug: str
    tenant_name: str
    role: str
    membership_status: str
    tenant_status: str
    is_default_tenant: bool


class ActiveTenantContextResponse(BaseModel):
    tenant_id: int
    membership_id: int
    role: str
    is_default_tenant: bool
    source: str


class AuthMeResponse(UserResponse):
    active_tenant: Optional[ActiveTenantContextResponse] = None
    membership_summaries: list[TenantMembershipSummaryResponse] = Field(default_factory=list)
    effective_permissions: Optional[EffectivePermissionsResponse] = None


class TenantSwitchRequest(BaseModel):
    tenant_id: int = Field(..., gt=0)


class TenantMembershipUpdateRequest(BaseModel):
    role: Optional[str] = None
    deactivate: bool = False

    @model_validator(mode="after")
    def validate_mutation_request(self):
        has_role_change = self.role is not None and str(self.role).strip() != ""
        if self.deactivate and has_role_change:
            raise ValueError("role and deactivate cannot be requested together")
        if not self.deactivate and not has_role_change:
            raise ValueError("role or deactivate must be requested")
        return self


class TenantContextResponse(BaseModel):
    active_tenant: Optional[ActiveTenantContextResponse] = None
    membership_summaries: list[TenantMembershipSummaryResponse] = Field(default_factory=list)
    effective_permissions: Optional[EffectivePermissionsResponse] = None


class TenantManagedMembershipResponse(BaseModel):
    membership_id: int
    tenant_id: int
    user_id: int
    role: str
    status: str
    deactivated_at: Optional[datetime] = None
    deactivated_by_user_id: Optional[int] = None


class TaskBase(BaseModel):
    name: str
    description: Optional[str] = None
    scope: Optional[str] = None


class TaskCreate(TaskBase):
    engagement_id: Optional[int] = None
    timeout_seconds: Optional[int] = 3600
    max_retries: Optional[int] = 3
    priority: Optional[int] = 1


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    scope: Optional[str] = None
    engagement_id: Optional[int] = None
    status: Optional[str] = None
    current_step: Optional[str] = None
    progress_percentage: Optional[int] = None
    error_message: Optional[str] = None
    failure_reason: Optional[str] = None


class TaskResponse(TaskBase):
    id: int
    user_id: int
    engagement_id: Optional[int] = None
    engagement_name: Optional[str] = None
    status: str
    mode: str
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    container_id: Optional[str] = None
    agent_pid: Optional[int] = None
    resource_usage: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    failure_reason: Optional[str] = None
    retry_count: int = 0
    current_step: Optional[str] = None
    total_steps: Optional[int] = None
    progress_percentage: int = 0
    timeout_seconds: int = 3600
    max_retries: int = 3
    priority: int = 1

    model_config = ConfigDict(from_attributes=True)


class EngagementCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must contain non-whitespace characters")
        return stripped


class EngagementResponse(BaseModel):
    id: int
    user_id: int
    name: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaskStatusUpdate(BaseModel):
    """Model for status change requests with validation."""

    new_status: str
    reason: Optional[str] = None
    error_message: Optional[str] = None


class TaskProgressUpdate(BaseModel):
    """Model for progress updates during execution."""

    current_step: Optional[str] = None
    progress_percentage: Optional[int] = None
    total_steps: Optional[int] = None


class TaskExecutionMetadata(BaseModel):
    """Model for execution metadata."""

    container_id: Optional[str] = None
    agent_pid: Optional[int] = None
    resource_usage: Optional[Dict[str, Any]] = None


class TaskHistoryBase(BaseModel):
    """Base model for task history entries."""

    old_status: Optional[str] = None
    new_status: str
    transition_reason: Optional[str] = None
    change_source: str = "manual"
    change_metadata: Optional[Dict[str, Any]] = None


class TaskHistoryCreate(TaskHistoryBase):
    """Model for creating task history entries."""

    task_id: int
    user_id: Optional[int] = None


class TaskHistoryResponse(TaskHistoryBase):
    """Model for task history API responses."""

    id: int
    task_id: int
    user_id: Optional[int] = None
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentLogBase(BaseModel):
    level: str
    message: str
    log_metadata: Optional[Dict[str, Any]] = None


class AgentLogCreate(AgentLogBase):
    task_id: int


class AgentLogResponse(AgentLogBase):
    id: int
    task_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReportBase(BaseModel):
    title: str
    content: str
    findings: Optional[Dict[str, Any]] = None
    severity: Optional[str] = "info"


class ReportCreate(ReportBase):
    task_id: int


class ReportResponse(ReportBase):
    id: int
    task_id: int
    user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
