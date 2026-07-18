"""Canonical aggregate export surface for SQLAlchemy ORM model classes.

This package imports all ORM model modules so Alembic autogenerate and
test/dev metadata utilities see every table on the shared `Base.metadata`.
"""

from importlib import import_module

from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.cve import (
    CveAffectedProduct,
    CveIndexSettings,
    CveIndexState,
    CveIndexSyncRun,
    CveRecord,
)
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.chat import AgentLog, ChatMessage, ChatTurnEvent, ToolCall
from backend.models.core import (
    Engagement,
    Report,
    Task,
    TaskHistory,
    TaskTurnCounter,
    User,
    UserSession,
    UserSettings,
)
from backend.models.tenant import Tenant, TenantMembership
from backend.models.data_management import TenantDataManagementSettings
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.platform_installation import PlatformInstallation
from backend.models.streaming import StreamEvent, SystemLog
from backend.models.llm import (
    LLMCapabilityObservation,
    LLMConversation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    LLMUsageRecord,
    UserEmbeddingSelection,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
)
from backend.models.semantic_memory import SemanticMemory
from backend.schemas import *  # noqa: F401,F403
from backend.domain.task_lifecycle import (
    TaskStateTransition,
    TaskStatus,
    TaskStatusValidator,
    get_status_metadata,
    validate_status_change,
)
from backend.database import GUID
from backend.schemas import __all__ as _SCHEMA_EXPORTS
from backend.services.cve_indexing.schemas import (
    CvePurgeResponse,
    CveSettingsResponse,
    CveSettingsStaticResponse,
    CveSettingsStatusResponse,
    CveSettingsUpdateRequest,
    CveSyncDispatchResponse,
    CveSyncRunSummaryResponse,
    CveSyncStatusResponse,
)

__all__ = [
    "User",
    "UserSession",
    "UserSettings",
    "Task",
    "Engagement",
    "TaskHistory",
    "TaskTurnCounter",
    "Report",
    "Tenant",
    "TenantMembership",
    "TenantDataManagementSettings",
    "ExecutionSite",
    "Runner",
    "RunnerCredential",
    "RunnerInstallToken",
    "RuntimeJob",
    "RunnerConnection",
    "RunnerControlMessage",
    "KnowledgeIngestionRun",
    "KnowledgeObservation",
    "KnowledgeEvidenceArchive",
    "KnowledgeAsset",
    "KnowledgeService",
    "KnowledgeFinding",
    "KnowledgeRelationship",
    "EngagementAssetLink",
    "EngagementServiceLink",
    "EngagementFindingLink",
    "KnowledgeWebPath",
    "EngagementWebPathLink",
    "KnowledgeEntityProvenance",
    "CveIndexSettings",
    "CveIndexSyncRun",
    "CveIndexState",
    "CveRecord",
    "CveAffectedProduct",
    "TurnWorkflow",
    "InterruptTicketState",
    "InterruptTicket",
    "AgentLog",
    "ChatMessage",
    "ToolCall",
    "ChatTurnEvent",
    "ToolExecution",
    "ArtifactManifest",
    "ExecutionArtifact",
    "TaskClosureMemo",
    "EngagementReport",
    "EngagementReportJob",
    "PlatformInstallation",
    "SystemLog",
    "StreamEvent",
    "LLMInferenceConnection",
    "LLMModelDeployment",
    "LLMDeploymentRoute",
    "LLMCapabilityObservation",
    "UserEmbeddingSelection",
    "UserLLMProviderCredential",
    "UserLLMSelection",
    "UserMemoryLLMSelection",
    "UserReportingLLMSelection",
    "LLMConversation",
    "LLMUsageRecord",
    "SemanticMemory",
    *list(_SCHEMA_EXPORTS),
    "TaskStatus",
    "TaskStateTransition",
    "TaskStatusValidator",
    "validate_status_change",
    "get_status_metadata",
    "GUID",
    "CveSettingsUpdateRequest",
    "CveSettingsResponse",
    "CveSettingsStaticResponse",
    "CveSettingsStatusResponse",
    "CveSyncStatusResponse",
    "CveSyncRunSummaryResponse",
    "CveSyncDispatchResponse",
    "CvePurgeResponse",
]


def __getattr__(name: str):
    """Provide lazy compatibility access to auth token schemas."""

    if name in {"Token", "TokenData"}:
        module = import_module("backend.auth")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
