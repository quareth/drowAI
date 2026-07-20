"""Backend LLM provider settings and runtime service package.

The package owns provider-neutral credential storage, model selection,
runtime selection payloads, health checks, and adapter-construction boundaries.
It intentionally keeps decrypted provider secrets out of graph state,
checkpointable metadata, queue payloads, and router response schemas.
"""

from .catalog_service import (
    CatalogModelSummary,
    CatalogProviderSummary,
    LLMProviderCatalogService,
)
from .catalog_application_service import LLMCatalogApplicationService
from .conversation_lifecycle_service import LLMConversationLifecycleService
from .connection_authorization import LLMConnectionAuthorizer
from .connection_service import LLMConnectionService
from .connection_status_service import LLMConnectionStatusService
from .credential_service import (
    LLMCredentialService,
    decrypt_api_key,
    encrypt_api_key,
    get_encryption_key,
)
from .environment_service import LLMProviderEnvironmentService
from .effective_profile_service import EffectiveProfileService
from .deployment_service import LLMDeploymentService
from .failure_policy import (
    LLMRuntimeFailureDisposition,
    LLMRuntimeFailureKind,
    classify_llm_runtime_failure,
)
from .health_service import LLMProviderHealthService
from .inventory_service import LLMInventoryService
from .managed_connection_lifecycle_service import (
    LLMManagedConnectionLifecycleService,
)
from .migration_service import LLMProviderMigrationService
from .runtime_client_resolver import LLMRuntimeClientResolver, resolve_call_target
from .runtime_config_service import LLMRuntimeConfigService
from .reporting_selection_service import (
    ReportingLLMSelectionMissingError,
    ReportingLLMSelectionRead,
    ReportingLLMSelectionService,
)
from .runtime_services import (
    LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
    LLMRuntimeServices,
    LLMServiceOperationContext,
    attach_runtime_services,
    get_runtime_services,
    strip_runtime_services,
)
from .selection_service import LLMProviderSelectionService
from .types import (
    AuthorizedLLMConnectionOperation,
    CredentialAuthorizationError,
    CredentialEncryptionError,
    CredentialNotFoundError,
    CredentialStatus,
    DeploymentRef,
    LLMAuthMode,
    LLMCredentialRef,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionCredentialRef,
    LLMConnectionNotFoundError,
    LLMConnectionOperation,
    LLMConnectionRevisionConflictError,
    LLMConnectionState,
    LLMConnectionStateTransitionError,
    LLMConnectionValidationError,
    LLMDeploymentNotFoundError,
    LLMDeploymentValidationError,
    LLMProviderServiceError,
    LLMRuntimeSelection,
    LLMRuntimeAccessContext,
    LLMRuntimeSelectionV2,
    LLMSelectionStatus,
    ProviderConfigurationError,
    ProviderHealthCheckResult,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)

__all__ = [
    "AuthorizedLLMConnectionOperation",
    "CatalogModelSummary",
    "CatalogProviderSummary",
    "CredentialAuthorizationError",
    "CredentialEncryptionError",
    "CredentialNotFoundError",
    "CredentialStatus",
    "DeploymentRef",
    "LLMAuthMode",
    "LLMCallTarget",
    "LLMCatalogApplicationService",
    "LLMConnectionAccessContext",
    "LLMConnectionAuthorizationError",
    "LLMConnectionAuthorizer",
    "LLMConnectionCredentialRef",
    "LLMConnectionNotFoundError",
    "LLMConnectionOperation",
    "LLMConnectionRevisionConflictError",
    "LLMConnectionService",
    "LLMConnectionStatusService",
    "LLMConnectionState",
    "LLMConnectionStateTransitionError",
    "LLMConnectionValidationError",
    "LLMConversationLifecycleService",
    "LLMCredentialRef",
    "LLMCredentialService",
    "LLMDeploymentNotFoundError",
    "LLMDeploymentValidationError",
    "LLMDeploymentService",
    "LLMInventoryService",
    "LLMManagedConnectionLifecycleService",
    "LLM_INVENTORY_REFRESH_SERVICE_ACTOR",
    "LLMProviderCatalogService",
    "LLMProviderEnvironmentService",
    "EffectiveProfileService",
    "LLMProviderHealthService",
    "LLMProviderMigrationService",
    "LLMProviderSelectionService",
    "LLMProviderServiceError",
    "ReportingLLMSelectionMissingError",
    "ReportingLLMSelectionRead",
    "ReportingLLMSelectionService",
    "LLMRuntimeClientResolver",
    "LLMRuntimeFailureDisposition",
    "LLMRuntimeFailureKind",
    "LLMRuntimeConfigService",
    "LLMRuntimeSelection",
    "LLMRuntimeAccessContext",
    "LLMRuntimeSelectionV2",
    "LLMRuntimeServices",
    "LLMServiceOperationContext",
    "LLMSelectionStatus",
    "ProviderConfigurationError",
    "ProviderHealthCheckResult",
    "ProviderSecret",
    "ResolvedAuth",
    "ResolvedConnectionTarget",
    "ResolvedLLMTarget",
    "attach_runtime_services",
    "classify_llm_runtime_failure",
    "decrypt_api_key",
    "encrypt_api_key",
    "get_encryption_key",
    "get_runtime_services",
    "resolve_call_target",
    "strip_runtime_services",
]
