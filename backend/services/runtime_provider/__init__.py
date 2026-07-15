"""Task execution runtime provider package.

Responsibilities:
- Export the runtime provider interface used by management-plane services.
- Export runtime provider request/result contracts for provider implementations.
"""

from .contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    build_runtime_result,
    is_pending_runner_operation_result,
    is_runner_placement_mode,
    is_runner_assignment_probe_result,
    normalize_runtime_call_scope,
)
from .context import RuntimeProviderContextResolver, RuntimeRequestContext
from .cloud_runner_provider import CloudRunnerRuntimeProvider
from .operations import RuntimeOperationService, provider_result_detail, provider_result_success
from .provider import TaskExecutionRuntimeProvider
from .registry import (
    RuntimeProviderRegistry,
    UnsupportedRuntimePlacementError,
    resolve_task_runtime_placement_mode,
)
from .runner_provider_selection import (
    ManagedRunnerProviderUnavailableError,
    build_runner_runtime_provider,
    validate_managed_runner_control_enabled,
)

__all__ = [
    "RuntimeActorType",
    "RuntimeCallScope",
    "RuntimeOperationRequest",
    "RuntimeOperationResult",
    "RuntimeOperationStatus",
    "RuntimePlacementMode",
    "RuntimeProviderContextResolver",
    "RuntimeRequestContext",
    "CloudRunnerRuntimeProvider",
    "RuntimeProviderRegistry",
    "RuntimeOperationService",
    "TaskExecutionRuntimeProvider",
    "ManagedRunnerProviderUnavailableError",
    "UnsupportedRuntimePlacementError",
    "build_runner_runtime_provider",
    "build_runtime_result",
    "is_pending_runner_operation_result",
    "is_runner_placement_mode",
    "is_runner_assignment_probe_result",
    "normalize_runtime_call_scope",
    "provider_result_detail",
    "provider_result_success",
    "resolve_task_runtime_placement_mode",
    "validate_managed_runner_control_enabled",
]
