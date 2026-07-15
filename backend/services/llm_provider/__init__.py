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
from .conversation_lifecycle_service import LLMConversationLifecycleService
from .credential_service import (
    LLMCredentialService,
    decrypt_api_key,
    encrypt_api_key,
    get_encryption_key,
)
from .environment_service import LLMProviderEnvironmentService
from .failure_policy import (
    LLMRuntimeFailureDisposition,
    LLMRuntimeFailureKind,
    classify_llm_runtime_failure,
)
from .health_service import LLMProviderHealthService
from .migration_service import LLMProviderMigrationService
from .runtime_client_resolver import LLMRuntimeClientResolver, resolve_call_target
from .runtime_config_service import LLMRuntimeConfigService
from .reporting_selection_service import (
    ReportingLLMSelectionMissingError,
    ReportingLLMSelectionRead,
    ReportingLLMSelectionService,
)
from .runtime_services import (
    LLMRuntimeServices,
    attach_runtime_services,
    get_runtime_services,
    strip_runtime_services,
)
from .selection_service import LLMProviderSelectionService
from .types import (
    CredentialAuthorizationError,
    CredentialEncryptionError,
    CredentialNotFoundError,
    CredentialStatus,
    LLMCredentialRef,
    LLMCallTarget,
    LLMProviderServiceError,
    LLMRuntimeSelection,
    LLMSelectionStatus,
    ProviderConfigurationError,
    ProviderHealthCheckResult,
    ProviderSecret,
)

__all__ = [
    "CatalogModelSummary",
    "CatalogProviderSummary",
    "CredentialAuthorizationError",
    "CredentialEncryptionError",
    "CredentialNotFoundError",
    "CredentialStatus",
    "LLMCallTarget",
    "LLMConversationLifecycleService",
    "LLMCredentialRef",
    "LLMCredentialService",
    "LLMProviderCatalogService",
    "LLMProviderEnvironmentService",
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
    "LLMRuntimeServices",
    "LLMSelectionStatus",
    "ProviderConfigurationError",
    "ProviderHealthCheckResult",
    "ProviderSecret",
    "attach_runtime_services",
    "classify_llm_runtime_failure",
    "decrypt_api_key",
    "encrypt_api_key",
    "get_encryption_key",
    "get_runtime_services",
    "resolve_call_target",
    "strip_runtime_services",
]
