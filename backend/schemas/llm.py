"""Pydantic schemas for LLM provider, conversation, and usage APIs.

Scope:
- Defines serialized request/response contracts for provider-neutral LLM
  credentials, selections, usage records, and conversations.
- Serves as the canonical schema module for LLM-related API response models.

Boundaries:
- API schema definitions only; no ORM models, persistence, or provider logic.
- Backward-compatible re-exports remain in `backend.models` during parallel run.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMConnectionRefResponse(BaseModel):
    """Opaque revisioned connection identity safe for API responses."""

    connection_id: str
    expected_revision: int


class LLMDeploymentRef(BaseModel):
    """Opaque revisioned deployment identity accepted by selection APIs."""

    deployment_id: str
    expected_revision: int


class UserLLMProviderCredentialUpsert(BaseModel):
    """Request body for creating or replacing a provider credential."""

    api_key: str
    enabled: bool = True


class UserLLMProviderCredentialResponse(BaseModel):
    """Non-secret credential status for a user/provider pair."""

    id: int
    user_id: int
    provider: str
    enabled: bool
    has_api_key: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LLMProviderCredentialStatusResponse(BaseModel):
    """Non-secret provider credential status exposed by runtime routes."""

    user_id: int
    provider: str
    enabled: bool
    has_api_key: bool
    masked_api_key: Optional[str] = None
    connection_ref: Optional[LLMConnectionRefResponse] = None
    auth_mode: Optional[str] = None


class LLMCatalogModelResponse(BaseModel):
    """Public model metadata returned by the provider catalog."""

    id: str
    label: str
    api_surface: str = Field(alias="apiSurface")
    capabilities: list[str]
    context_window_tokens: int = Field(alias="contextWindowTokens")
    max_output_tokens: int = Field(alias="maxOutputTokens")
    reasoning_efforts: list[str] = Field(alias="reasoningEfforts")
    visible_reasoning_efforts: list[str] = Field(alias="visibleReasoningEfforts")
    default_reasoning_effort: Optional[str] = Field(default=None, alias="defaultReasoningEffort")
    default_visible_reasoning_effort: Optional[str] = Field(
        default=None,
        alias="defaultVisibleReasoningEffort",
    )
    tool_choice_modes: list[str] = Field(alias="toolChoiceModes")
    structured_output_strategies: list[str] = Field(alias="structuredOutputStrategies")
    pricing_status: str = Field(alias="pricingStatus")
    deployment_ref: Optional[LLMDeploymentRef] = None
    runnable: bool = False


class LLMCatalogProviderResponse(BaseModel):
    """Public provider metadata returned by the provider catalog."""

    id: str
    label: str
    capabilities: list[str]
    available: bool
    selectable: bool
    credential: LLMProviderCredentialStatusResponse
    models: list[LLMCatalogModelResponse]
    default_model: str = Field(alias="defaultModel")


class LLMModelCatalogResponse(BaseModel):
    """Response body for `/api/llm/models`."""

    providers: list[LLMCatalogProviderResponse]


class LLMSelectionStatusResponse(BaseModel):
    """Descriptive runnability status for a saved provider/model selection."""

    status: str
    selectable: bool
    runnable: bool
    reason: Optional[str] = None


class LLMSelectionResponse(BaseModel):
    """Current provider/model selection plus non-mutating status metadata."""

    provider: str
    model: str
    selection_status: LLMSelectionStatusResponse


class DeploymentLLMSelectionResponse(LLMSelectionResponse):
    """Deployment-aware conversation selection response."""

    deployment_ref: LLMDeploymentRef


class LLMSelectionUpsert(BaseModel):
    """Legacy provider/model or deployment-aware conversation selection."""

    provider: Optional[str] = None
    model: Optional[str] = None
    deployment_ref: Optional[LLMDeploymentRef] = None

    @model_validator(mode="after")
    def validate_identity(self) -> "LLMSelectionUpsert":
        """Require one unambiguous selection identity."""

        has_legacy = self.provider is not None or self.model is not None
        if self.deployment_ref is not None:
            if has_legacy:
                raise ValueError("Use either deployment_ref or provider/model")
            return self
        if self.provider is None or self.model is None:
            raise ValueError("provider and model are required without deployment_ref")
        return self


class LLMSelectionWriteResponse(BaseModel):
    """Legacy-compatible conversation selection mutation response."""

    provider: str
    model: str


class DeploymentLLMSelectionWriteResponse(LLMSelectionWriteResponse):
    """Deployment-aware conversation selection mutation response."""

    deployment_ref: LLMDeploymentRef


class ReportingLLMSelectionUpsert(BaseModel):
    """Request body for storing the reporting provider/model selection."""

    provider: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    deployment_ref: Optional[LLMDeploymentRef] = None

    @model_validator(mode="after")
    def validate_identity(self) -> "ReportingLLMSelectionUpsert":
        """Require one unambiguous reporting selection identity."""

        has_legacy = self.provider is not None or self.model is not None
        if self.deployment_ref is not None:
            if has_legacy:
                raise ValueError("Use either deployment_ref or provider/model")
            return self
        if self.provider is None or self.model is None:
            raise ValueError("provider and model are required without deployment_ref")
        return self


class ReportingLLMSelectionResponse(BaseModel):
    """Current reporting model selection and runnability status."""

    provider: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    selection_status: LLMSelectionStatusResponse


class ReportingDeploymentLLMSelectionResponse(ReportingLLMSelectionResponse):
    """Deployment-aware reporting selection response."""

    deployment_ref: LLMDeploymentRef


class LLMProviderCredentialTestRequest(BaseModel):
    """Request body for testing a supplied or stored provider credential."""

    api_key: Optional[str] = None


class LLMProviderCredentialTestResponse(BaseModel):
    """Provider-neutral credential health-check result."""

    provider: str
    status: str
    message: str
    model_count: Optional[int] = None


class LLMProviderCredentialDeleteResponse(BaseModel):
    """Response body for provider credential deletion."""

    success: bool


class UserLLMSelectionUpsert(BaseModel):
    """Request body for storing the selected conversation provider/model."""

    provider: str
    model: str


class UserLLMSelectionResponse(BaseModel):
    """Provider-neutral selected conversation model for a user."""

    id: int
    user_id: int
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserEmbeddingSelectionUpsert(BaseModel):
    """Request body for storing the selected memory embedding model."""

    provider: str
    model: str


class UserEmbeddingSelectionResponse(BaseModel):
    """Provider-neutral selected semantic-memory embedding model."""

    id: Optional[int] = None
    user_id: int
    provider: str
    model: str
    dimensions: int
    vector_family: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UserMemoryLLMSelectionUpsert(BaseModel):
    """Request body for storing selected memory LLM models."""

    provider: str
    gate_model: str
    extraction_model: str


class UserMemoryLLMSelectionResponse(BaseModel):
    """Provider-neutral selected semantic-memory LLM dependencies."""

    id: Optional[int] = None
    user_id: int
    provider: str
    gate_model: str
    extraction_model: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UserMemoryDependencySelectionsResponse(BaseModel):
    """Combined semantic-memory dependency selections for one user."""

    embedding: UserEmbeddingSelectionResponse
    memory_llm: UserMemoryLLMSelectionResponse
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_vector_family: Optional[str] = None


class LLMUsageRecordResponse(BaseModel):
    """Pydantic model for LLMUsageRecord API responses."""

    id: int
    task_id: int
    user_id: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    model: str
    provider: str
    source: str
    conversation_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LLMConversationResponse(BaseModel):
    """Pydantic model for LLMConversation API responses."""

    id: Optional[int] = None
    provider: str
    model: Optional[str] = None
    conversation_id: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)
