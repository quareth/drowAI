"""LLM selection and task runtime control routes.

This router exposes user-facing model selection along with task-scoped runtime
controls such as conversation reset.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Dict, Any
import json
import logging
from pydantic import BaseModel

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID

from ..auth import get_current_user
from ..config.feature_flags import is_semantic_memory_runtime_enabled
from ..database import get_db
from ..models import User, LLMCapabilityObservation, LLMConversation, LLMConversationResponse
from ..schemas.llm import (
    LLMModelCatalogResponse,
    LLMManagedConnectionCreateRequest,
    LLMManagedConnectionEnableRequest,
    LLMManagedConnectionRefreshRequest,
    LLMManagedConnectionStatusResponse,
    LLMManagedConnectionTestRequest,
    LLMProvingConnectionCreateRequest,
    LLMProvingConnectionEnableRequest,
    LLMProvingConnectionStatusResponse,
    LLMProvingConnectionTestRequest,
    LLMProvingVerificationResponse,
    DeploymentLLMSelectionResponse,
    DeploymentLLMSelectionWriteResponse,
    LLMProviderCredentialDeleteResponse,
    LLMProviderCredentialStatusResponse,
    LLMProviderCredentialTestRequest,
    LLMProviderCredentialTestResponse,
    LLMSelectionUpsert,
    LLMSelectionResponse,
    LLMSelectionWriteResponse,
    ReportingDeploymentLLMSelectionResponse,
    ReportingLLMSelectionResponse,
    ReportingLLMSelectionUpsert,
    UserEmbeddingSelectionResponse,
    UserEmbeddingSelectionUpsert,
    UserLLMProviderCredentialUpsert,
    UserMemoryDependencySelectionsResponse,
    UserMemoryLLMSelectionResponse,
    UserMemoryLLMSelectionUpsert,
)
from ..services.embeddings.selection_service import EmbeddingRuntimeSelectionService
from ..services.llm_provider import (
    CredentialNotFoundError,
    LLMCredentialRef,
    LLMCredentialService,
    LLMAuthMode,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizer,
    LLMConnectionService,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    LLMConversationLifecycleService,
    LLMDeploymentService,
    EffectiveProfileService,
    LLMInventoryService,
    LLMProviderCatalogService,
    LLMProviderHealthService,
    LLMProviderMigrationService,
    LLMProviderServiceError,
    LLMProviderSelectionService,
    ProviderConfigurationError,
    ReportingLLMSelectionService,
)
from ..services.llm_provider.guarded_transport import GuardedTransport, GuardedTransportError
from ..services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from ..services.llm_provider.selection_deployment_resolver import (
    LLMSelectionDeploymentResolver,
    SelectionDeploymentTarget,
)
from ..services.llm_provider.types import ProviderSecret
from ..services.tenant.authorization import ACTION_CHAT_READ, ACTION_CHAT_WRITE
from ..services.tenant.context import TenantRequestContext
from ..services.tenant.dependencies import get_tenant_request_context
from ..services.task.runtime_input_service import TaskRuntimeInputService
from ..services.llm_provider.conversation_lifecycle_service import RemoteConversationOrigin
from .tasks.deps import enforce_tenant_action, get_tenant_task_or_404
router = APIRouter(prefix="/api/llm", tags=["llm"])
logger = logging.getLogger(__name__)
_runtime_input_service = TaskRuntimeInputService()


def _provider_configuration_exception(exc: LLMProviderServiceError) -> HTTPException:
    """Map provider service errors to stable route responses."""

    detail = str(exc)
    lowered = detail.lower()
    if "rate limit" in lowered:
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
    if "unknown llm provider" in lowered:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    if "does not support remote conversation lifecycle" in lowered or "not implemented" in lowered:
        return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)
    if "conversation create failed" in lowered:
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _credential_status_response(
    status_obj,
    *,
    db: Session,
) -> LLMProviderCredentialStatusResponse:
    """Convert service credential status to the public response schema."""

    connection_ref = None
    if status_obj.connection_id is not None:
        connection = LLMConnectionService(db).get_owned(
            user_id=status_obj.user_id,
            connection_id=status_obj.connection_id,
        )
        connection_ref = {
            "connection_id": str(connection.id),
            "expected_revision": int(connection.revision),
        }
    return LLMProviderCredentialStatusResponse(
        user_id=status_obj.user_id,
        provider=status_obj.provider,
        enabled=status_obj.enabled,
        has_api_key=status_obj.has_api_key,
        masked_api_key=status_obj.masked_api_key,
        connection_ref=connection_ref,
        auth_mode=(status_obj.auth_mode.value if status_obj.auth_mode else None),
    )


def _deployment_ref(
    db: Session,
    *,
    user_id: int,
    deployment_id,
) -> dict[str, object] | None:
    """Return a current owner-scoped opaque deployment ref."""

    if deployment_id is None:
        return None
    deployment = LLMDeploymentService(db).get_deployment(
        user_id=user_id,
        deployment_id=deployment_id,
    )
    return {
        "deployment_id": str(deployment.id),
        "expected_revision": int(deployment.revision),
    }


def _catalog_deployment_map(
    db: Session,
    *,
    user_id: int,
) -> dict[tuple[str, str], tuple[object, object]]:
    """Map catalog provider/model keys to current owner-scoped deployments."""

    LLMProviderMigrationService(db).backfill_deployment_identity_for_user(user_id)
    connections = LLMConnectionService(db).list_for_user(user_id=user_id)
    deployments = LLMDeploymentService(db)
    mapped: dict[tuple[str, str], tuple[object, object]] = {}
    for connection in connections:
        if connection.legacy_default_provider is None:
            continue
        for deployment in deployments.list_deployments(
            user_id=user_id,
            connection_id=connection.id,
        ):
            model = deployment.canonical_model_id or deployment.wire_model_id
            mapped[(connection.connection_preset_id, model.strip().lower())] = (
                connection,
                deployment,
            )
    return mapped


def _catalog_deployment_fields(
    *,
    provider: str,
    model: str,
    deployment_map: dict[tuple[str, str], tuple[object, object]],
    credential_runnable: bool,
) -> dict[str, object]:
    """Return public deployment metadata for one catalog model."""

    target = deployment_map.get((provider, model))
    if target is None:
        return {"deploymentRef": None, "runnable": False}
    connection, deployment = target
    return {
        "deploymentRef": {
            "deployment_id": str(deployment.id),
            "expected_revision": int(deployment.revision),
        },
        "runnable": bool(
            credential_runnable
            and connection.state == "enabled"
            and deployment.enabled
            and deployment.lifecycle_state == "active"
        ),
    }


def _catalog_connection_provider_rows(
    db: Session,
    *,
    user_id: int,
) -> list[dict[str, object]]:
    """Project reviewed connection presets and user deployments into catalog rows."""

    registry = ConnectionOperationRegistry()
    connections = LLMConnectionService(db).list_for_user(user_id=user_id)
    by_preset: dict[str, list[object]] = {}
    for connection in connections:
        by_preset.setdefault(connection.connection_preset_id, []).append(connection)

    rows: list[dict[str, object]] = []
    deployments = LLMDeploymentService(db)
    for preset_id in registry.list_connection_preset_ids():
        if preset_id == GPT_OSS_20B_PROVING_PRESET_ID:
            continue
        try:
            preset = registry.get_connection_preset(preset_id)
        except OperationRegistryError:
            continue
        models: list[dict[str, object]] = []
        for connection in by_preset.get(preset.id, []):
            owned_deployments = deployments.list_deployments(
                user_id=user_id,
                connection_id=connection.id,
            )
            if not owned_deployments:
                models.append(
                    _connection_catalog_model(
                        db,
                        user_id=user_id,
                        preset=preset,
                        connection=connection,
                        deployment=None,
                    )
                )
                continue
            for deployment in owned_deployments:
                models.append(
                    _connection_catalog_model(
                        db,
                        user_id=user_id,
                        preset=preset,
                        connection=connection,
                        deployment=deployment,
                    )
                )
        if not models:
            models.append(
                _connection_catalog_model(
                    db,
                    user_id=user_id,
                    preset=preset,
                    connection=None,
                    deployment=None,
                )
            )
        rows.append(
            {
                "id": preset.id,
                "label": _connection_provider_label(preset),
                "capabilities": sorted(
                    capability.value for capability in preset.capability_ceiling
                ),
                "available": True,
                "selectable": True,
                "credential": {
                    "user_id": user_id,
                    "provider": preset.id,
                    "enabled": any(
                        connection.state == LLMConnectionState.ENABLED.value
                        for connection in by_preset.get(preset.id, [])
                    ),
                    "has_api_key": bool(by_preset.get(preset.id)),
                    "masked_api_key": None,
                    "connection_ref": (
                        _connection_ref(by_preset[preset.id][0])
                        if by_preset.get(preset.id)
                        else None
                    ),
                    "auth_mode": "bearer",
                },
                "models": models,
                "defaultModel": str(models[0]["id"]),
            }
        )
    return rows


def _connection_catalog_model(
    db: Session,
    *,
    user_id: int,
    preset,
    connection,
    deployment,
) -> dict[str, object]:
    """Return one deployment-aware catalog model row for a connection preset."""

    route = None
    if deployment is not None:
        try:
            route = _first_route_for_deployment(
                db,
                user_id=user_id,
                deployment_id=deployment.id,
            )
        except HTTPException:
            route = None
    runnability = _connection_runnability(
        db,
        user_id=user_id,
        connection=connection,
        deployment=deployment,
        route=route,
    )
    wire_model_id = getattr(deployment, "wire_model_id", None)
    model_id = wire_model_id or preset.id
    label = getattr(deployment, "display_name", None) or preset.display_name
    deployment_ref = _deployment_ref_from_row(deployment) if deployment is not None else None
    return {
        "id": model_id,
        "canonicalModelId": (
            getattr(deployment, "canonical_model_id", None)
            or preset.canonical_model_id
            or model_id
        ),
        "exactWireModelId": wire_model_id,
        "label": label,
        "apiSurface": preset.api_surface,
        "capabilities": sorted(capability.value for capability in preset.capability_ceiling),
        "contextWindowTokens": 128000,
        "maxOutputTokens": 10000,
        "reasoningEfforts": [],
        "visibleReasoningEfforts": [],
        "defaultReasoningEffort": None,
        "defaultVisibleReasoningEffort": None,
        "toolChoiceModes": ["auto"],
        "structuredOutputStrategies": [],
        "pricingStatus": "unavailable",
        "deploymentRef": deployment_ref,
        "runnable": bool(runnability["runnable"]),
        "connection": _connection_catalog_metadata(
            preset=preset,
            connection=connection,
            deployment=deployment,
            runnability=runnability,
        ),
        "proving": None,
    }


def _connection_catalog_metadata(
    *,
    preset,
    connection,
    deployment,
    runnability: dict[str, object],
) -> dict[str, object]:
    """Return generic backend-declared connection management metadata."""

    fields = _connection_config_fields(
        preset,
        needs_wire_model=(
            deployment is None and preset.endpoint_config_field == "base_url"
        ),
    )
    return {
        "presetId": preset.id,
        "displayName": preset.display_name,
        "enabled": True,
        "authMode": preset.auth_mode,
        "userConfigFields": [field["name"] for field in fields],
        "configFields": fields,
        "lifecycleState": (
            connection.state if connection is not None else "not_created"
        ),
        "connectionRef": _connection_ref(connection) if connection is not None else None,
        "deploymentRef": (
            _deployment_ref_from_row(deployment) if deployment is not None else None
        ),
        "verification": _not_tested_verification(),
        "runnability": runnability,
    }


def _connection_config_fields(preset, *, needs_wire_model: bool) -> list[dict[str, object]]:
    """Return typed field metadata for one reviewed connection preset."""

    fields: list[dict[str, object]] = []
    for name in preset.user_config_fields:
        if name == "display_label":
            continue
        elif name == "api_key":
            fields.append(
                {
                    "name": "api_key",
                    "label": "API key",
                    "fieldType": "password",
                    "required": True,
                    "secret": True,
                }
            )
        elif name == "base_url":
            fields.append(
                {
                    "name": "base_url",
                    "label": "Base URL",
                    "fieldType": "url",
                    "required": True,
                    "secret": False,
                }
            )
    if needs_wire_model:
        fields.append(
            {
                "name": "wire_model_id",
                "label": "Wire model ID",
                "fieldType": "text",
                "required": True,
                "secret": False,
            }
        )
    return fields


def _connection_provider_label(preset) -> str:
    """Return a concise product label for a connection preset provider group."""

    if preset.serving_operator_id == "huggingface":
        return "Hugging Face"
    if preset.serving_operator_id == "nvidia_nim":
        return "NVIDIA NIM"
    if preset.serving_operator_id == "ollama_compatible":
        return "Ollama"
    if preset.serving_operator_id == "vllm":
        return "vLLM"
    if preset.serving_operator_id == "organization_managed":
        return "Custom OpenAI-compatible"
    return preset.display_name


def _connection_runnability(
    db: Session,
    *,
    user_id: int,
    connection,
    deployment,
    route,
) -> dict[str, object]:
    """Return current runnability metadata for generic connection deployments."""

    if connection is None:
        return {
            "status": "not_created",
            "selectable": True,
            "runnable": False,
            "reason": "Connection configuration is required.",
        }
    if deployment is None:
        return {
            "status": "deployment_missing",
            "selectable": True,
            "runnable": False,
            "reason": "Deployment model registration is required.",
        }
    if route is None or not route.enabled:
        return {
            "status": "capability_unknown",
            "selectable": True,
            "runnable": False,
            "reason": "Capability evidence is required.",
        }
    credential_service = LLMCredentialService(db)
    try:
        profile = EffectiveProfileService().resolve(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        status_obj = LLMSelectionDeploymentResolver(db).classify_runnability(
            user_id=user_id,
            target=SelectionDeploymentTarget(
                connection=connection,
                deployment=deployment,
                route=route,
                profile=profile,
            ),
            credential_available=credential_service.has_enabled_credential,
            credential_fingerprint=(
                credential_service.connection_credential_fingerprint
            ),
            missing_credential_reason="Stored connection credential is required.",
            required_capabilities=(LLMCapability.CHAT,),
            capability_missing_reason="Capability evidence is required.",
        )
    except LLMProviderServiceError as exc:
        return {
            "status": "invalid_selection",
            "selectable": False,
            "runnable": False,
            "reason": str(exc),
        }
    if status_obj is not None:
        return status_obj.to_dict()
    return {
        "status": "runnable",
        "selectable": True,
        "runnable": True,
        "reason": None,
    }


def _connection_ref(connection) -> dict[str, object]:
    """Return the current opaque connection ref for API responses."""

    return {
        "connection_id": str(connection.id),
        "expected_revision": int(connection.revision),
    }


def _deployment_ref_from_row(deployment) -> dict[str, object]:
    """Return the current opaque deployment ref for API responses."""

    return {
        "deployment_id": str(deployment.id),
        "expected_revision": int(deployment.revision),
    }


def _require_gpt_oss_proving_preset(preset_id: str) -> None:
    """Reject every Phase 4 proving route except the one code-owned preset."""

    if preset_id != GPT_OSS_20B_PROVING_PRESET_ID:
        raise HTTPException(status_code=404, detail="Unknown proving preset")
    try:
        ConnectionOperationRegistry().get_proving_preset(preset_id)
    except OperationRegistryError as exc:
        raise HTTPException(status_code=404, detail="Unknown proving preset") from exc


def _first_route_for_deployment(
    db: Session,
    *,
    user_id: int,
    deployment_id,
):
    """Return the single code-owned proving route for a deployment."""

    routes = LLMDeploymentService(db).list_routes(
        user_id=user_id,
        deployment_id=deployment_id,
    )
    if not routes:
        raise HTTPException(status_code=400, detail="Deployment route is unavailable")
    return routes[0]


def _proving_verification_response(result) -> dict[str, object]:
    """Serialize sanitized proving verification output without secrets."""

    return {
        "status": result.status,
        "code": result.code,
        "message": result.message,
        "retryable": result.retryable,
        "observed_at": result.observed_at,
        "expires_at": result.expires_at,
        "model_present": result.model_present,
        "usage": result.usage,
    }


def _not_tested_verification() -> dict[str, object]:
    """Return a stable no-evidence verification status for catalog metadata."""

    return {
        "status": "failed",
        "code": "not_tested",
        "message": "Verification has not run.",
        "retryable": False,
        "observed_at": None,
        "expires_at": None,
        "model_present": None,
        "usage": None,
    }


def _proving_runnability(
    db: Session,
    *,
    connection,
    deployment,
    route,
) -> dict[str, object]:
    """Return chat and usage evidence status for a proving deployment."""

    try:
        credential_fingerprint = LLMCredentialService(
            db
        ).connection_credential_fingerprint(
            user_id=int(connection.user_id),
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
        )
    except LLMProviderServiceError:
        return {
            "status": "credential_missing",
            "selectable": True,
            "runnable": False,
            "reason": "Stored proving credential is required.",
        }
    decision = EffectiveProfileService(db).classify_runnability(
        deployment=deployment,
        route=route,
        required_capabilities=(
            LLMCapability.CHAT,
            LLMCapability.USAGE_REPORTING,
        ),
        connection_id=str(connection.id),
        connection_revision=int(connection.revision),
        credential_fingerprint=credential_fingerprint,
    )
    if decision.runnable:
        return {
            "status": "runnable",
            "selectable": True,
            "runnable": True,
            "reason": None,
        }
    return {
        "status": decision.status,
        "selectable": True,
        "runnable": False,
        "reason": "Usage evidence is required.",
    }


def _proving_status_response(
    db: Session,
    *,
    user_id: int,
    connection,
    deployment,
    verification: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return one public proving lifecycle response."""

    route = _first_route_for_deployment(
        db,
        user_id=user_id,
        deployment_id=deployment.id,
    )
    return {
        "lifecycle_state": connection.state,
        "connection_ref": _connection_ref(connection),
        "deployment_ref": _deployment_ref_from_row(deployment),
        "verification": verification,
        "runnability": _proving_runnability(
            db,
            connection=connection,
            deployment=deployment,
            route=route,
        ),
    }


def _managed_connection_status_response(
    db: Session,
    *,
    user_id: int,
    connection,
    deployment,
) -> dict[str, object]:
    """Return one public reviewed connection lifecycle response."""

    route = None
    if deployment is not None:
        try:
            route = _first_route_for_deployment(
                db,
                user_id=user_id,
                deployment_id=deployment.id,
            )
        except HTTPException:
            route = None
    return {
        "lifecycle_state": connection.state,
        "connection_ref": _connection_ref(connection),
        "deployment_ref": (
            _deployment_ref_from_row(deployment) if deployment is not None else None
        ),
        "verification": _not_tested_verification(),
        "runnability": _connection_runnability(
            db,
            user_id=user_id,
            connection=connection,
            deployment=deployment,
            route=route,
        ),
    }


def _managed_connection_secret(
    db: Session,
    *,
    user_id: int,
    connection,
    api_key: str | None,
    purpose: str,
) -> str:
    """Return a supplied or stored connection API key for guarded operations."""

    supplied = api_key.strip() if isinstance(api_key, str) else ""
    if supplied:
        return supplied
    resolved = LLMCredentialService(db).resolve_connection_auth(
        LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        runtime_user_id=user_id,
        purpose=purpose,
        auth_mode=LLMAuthMode.BEARER,
    )
    return resolved.secret.value if resolved.secret is not None else ""


def _inventory_model_ids_from_response(body: bytes) -> tuple[str, ...]:
    """Parse model identifiers from a bounded OpenAI-compatible inventory body."""

    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProviderConfigurationError("Provider inventory response is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderConfigurationError("Provider inventory response is invalid")
    model_ids: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())
    if not model_ids:
        raise ProviderConfigurationError("Provider inventory response did not include models")
    return tuple(model_ids)


def _refresh_proving_observation_revision(
    db: Session,
    *,
    deployment,
    route,
    connection,
    previous_connection_revision: int,
) -> None:
    """Bind already-verified proving observations to the enabled revision."""

    rows = db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id,
            LLMCapabilityObservation.route_id == route.id,
        )
    ).scalars()
    for row in rows:
        constraints = row.constraints
        if not isinstance(constraints, dict):
            continue
        if str(constraints.get("connection_id")) != str(connection.id):
            continue
        try:
            observed_revision = int(constraints.get("connection_revision"))
        except (TypeError, ValueError):
            continue
        if observed_revision != int(previous_connection_revision):
            continue
        row.constraints = {
            **constraints,
            "connection_revision": int(connection.revision),
        }
    db.flush()


def _catalog_proving_metadata(
    db: Session,
    *,
    user_id: int,
    model: str,
    deployment_map: dict[tuple[str, str], tuple[object, object]],
) -> dict[str, object] | None:
    """Project the one Phase 4 proving preset into GPT-OSS catalog metadata."""

    if model != "gpt-oss-20b":
        return None
    preset = ConnectionOperationRegistry().get_proving_preset(
        GPT_OSS_20B_PROVING_PRESET_ID
    )
    base = {
        "presetId": preset.id,
        "displayName": preset.display_name,
        "enabled": True,
        "authMode": preset.auth_mode,
        "userConfigFields": list(preset.user_config_fields),
        "lifecycleState": "not_created",
        "connectionRef": None,
        "deploymentRef": None,
        "verification": _not_tested_verification(),
        "runnability": {
            "status": "capability_unknown",
            "selectable": True,
            "runnable": False,
            "reason": "Usage evidence is required.",
        },
    }
    target = deployment_map.get((GPT_OSS_20B_PROVING_PRESET_ID, "openai/gpt-oss-20b"))
    if target is None:
        return base
    connection, deployment = target
    route = _first_route_for_deployment(
        db,
        user_id=user_id,
        deployment_id=deployment.id,
    )
    runnability = _proving_runnability(
        db,
        connection=connection,
        deployment=deployment,
        route=route,
    )
    return {
        **base,
        "lifecycleState": connection.state,
        "connectionRef": _connection_ref(connection),
        "deploymentRef": _deployment_ref_from_row(deployment),
        "verification": (
            {
                "status": "passed",
                "code": "verified",
                "message": "GPT-OSS proving endpoint verified",
                "retryable": False,
                "observed_at": None,
                "expires_at": None,
                "model_present": True,
                "usage": None,
            }
            if runnability["runnable"]
            else _not_tested_verification()
        ),
        "runnability": runnability,
    }


def _memory_selection_service(db: Session) -> EmbeddingRuntimeSelectionService:
    """Build the service that owns memory dependency selections."""

    credential_service = LLMCredentialService(db)
    return EmbeddingRuntimeSelectionService(
        db=db,
        credential_ref_resolver=credential_service.get_credential_ref,
    )


def _ensure_semantic_memory_enabled() -> None:
    """Reject semantic-memory dependency routes while the feature is disabled."""

    if not is_semantic_memory_runtime_enabled():
        raise HTTPException(status_code=404, detail="Semantic memory is disabled")


@router.get("/models", response_model=LLMModelCatalogResponse)
async def list_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return available providers, curated models, and public capability metadata."""
    catalog = LLMProviderCatalogService()
    credential_service = LLMCredentialService(db, catalog_service=catalog)
    providers = catalog.list_providers()
    credential_statuses = {
        provider.id: credential_service.get_masked_status(
            current_user.id,
            provider.id,
        )
        for provider in providers
    }
    deployment_map = _catalog_deployment_map(db, user_id=current_user.id)
    provider_rows = [
            {
                "id": provider.id,
                "label": provider.label,
                "capabilities": list(provider.capabilities),
                "available": provider.available,
                "selectable": provider.selectable,
                "credential": _credential_status_response(
                    credential_statuses[provider.id],
                    db=db,
                ).model_dump(),
                "models": [
                    {
                        "id": model.id,
                        "canonicalModelId": model.canonical_model_id,
                        "exactWireModelId": model.exact_wire_model_id,
                        "label": model.label,
                        "apiSurface": model.api_surface,
                        "capabilities": list(model.capabilities),
                        "contextWindowTokens": model.context_window_tokens,
                        "maxOutputTokens": model.max_output_tokens,
                        "reasoningEfforts": list(model.reasoning_efforts),
                        "visibleReasoningEfforts": list(model.visible_reasoning_efforts),
                        "defaultReasoningEffort": model.default_reasoning_effort,
                        "defaultVisibleReasoningEffort": model.default_visible_reasoning_effort,
                        "toolChoiceModes": list(model.tool_choice_modes),
                        "structuredOutputStrategies": list(model.structured_output_strategies),
                        "pricingStatus": model.pricing_status,
                        **_catalog_deployment_fields(
                            provider=provider.id,
                            model=model.id,
                            deployment_map=deployment_map,
                            credential_runnable=bool(
                                credential_statuses[provider.id].enabled
                                and credential_statuses[provider.id].has_api_key
                            ),
                        ),
                        "proving": _catalog_proving_metadata(
                            db,
                            user_id=current_user.id,
                            model=model.id,
                            deployment_map=deployment_map,
                        ),
                    }
                    for model in provider.models
                ],
                "defaultModel": provider.default_model,
            }
            for provider in providers
        ]
    provider_rows.extend(
        _catalog_connection_provider_rows(db, user_id=current_user.id)
    )
    response = {"providers": provider_rows}
    db.commit()
    return response


@router.get(
    "/selection",
    response_model=DeploymentLLMSelectionResponse | LLMSelectionResponse,
)
async def get_selection(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current user's saved LLM selection and descriptive status."""
    try:
        read = LLMProviderSelectionService(db).get_selection_read(current_user.id)
        selection = read.selection
        db.commit()
        db.refresh(selection)
    except ProviderConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = {
        "provider": selection.provider,
        "model": selection.model,
        "selection_status": read.status.to_dict(),
    }
    deployment_ref = _deployment_ref(
        db,
        user_id=current_user.id,
        deployment_id=getattr(selection, "deployment_id", None),
    )
    if deployment_ref is not None:
        response["deployment_ref"] = deployment_ref
    return response


@router.put(
    "/selection",
    response_model=(
        DeploymentLLMSelectionWriteResponse | LLMSelectionWriteResponse
    ),
)
async def set_selection(
    body: LLMSelectionUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set current user's LLM selection; validates provider and persists model in settings."""
    try:
        service = LLMProviderSelectionService(db)
        if body.deployment_ref is not None:
            selection = service.set_deployment_selection(
                user_id=current_user.id,
                deployment_id=body.deployment_ref.deployment_id,
                expected_deployment_revision=body.deployment_ref.expected_revision,
            )
        else:
            selection = service.set_selection(
                user_id=current_user.id,
                provider=str(body.provider),
                model=str(body.model),
                require_enabled_credential=False,
            )
        db.commit()
        db.refresh(selection)
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{body.provider} credential is required to select {body.provider} model",
        ) from exc
    except LLMProviderServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "User %s set LLM selection to provider=%s model=%s",
        current_user.id,
        selection.provider,
        selection.model,
    )
    response = {"provider": selection.provider, "model": selection.model}
    deployment_ref = _deployment_ref(
        db,
        user_id=current_user.id,
        deployment_id=getattr(selection, "deployment_id", None),
    )
    if deployment_ref is not None:
        response["deployment_ref"] = deployment_ref
    return response


@router.get(
    "/reporting-selection",
    response_model=(
        ReportingDeploymentLLMSelectionResponse | ReportingLLMSelectionResponse
    ),
)
async def get_reporting_selection(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current user's reporting LLM selection and status."""

    read = ReportingLLMSelectionService(db).get_selection_read(current_user.id)
    selection = read.selection
    db.commit()
    if selection is None:
        return {
            "provider": None,
            "model": None,
            "reasoning_effort": None,
            "selection_status": read.status.to_dict(),
        }
    db.refresh(selection)
    response = {
        "provider": selection.provider,
        "model": selection.model,
        "reasoning_effort": selection.reasoning_effort,
        "selection_status": read.status.to_dict(),
    }
    deployment_ref = _deployment_ref(
        db,
        user_id=current_user.id,
        deployment_id=getattr(selection, "deployment_id", None),
    )
    if deployment_ref is not None:
        response["deployment_ref"] = deployment_ref
    return response


@router.put(
    "/reporting-selection",
    response_model=(
        ReportingDeploymentLLMSelectionResponse | ReportingLLMSelectionResponse
    ),
)
async def set_reporting_selection(
    body: ReportingLLMSelectionUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist current user's reporting LLM selection."""

    try:
        service = ReportingLLMSelectionService(db)
        if body.deployment_ref is not None:
            selection = service.set_deployment_selection(
                user_id=current_user.id,
                deployment_id=body.deployment_ref.deployment_id,
                expected_deployment_revision=body.deployment_ref.expected_revision,
                reasoning_effort=body.reasoning_effort,
            )
        else:
            selection = service.set_selection(
                user_id=current_user.id,
                provider=str(body.provider),
                model=str(body.model),
                reasoning_effort=body.reasoning_effort,
            )
        read = service.get_selection_read(current_user.id)
        db.commit()
        db.refresh(selection)
    except LLMProviderServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = {
        "provider": selection.provider,
        "model": selection.model,
        "reasoning_effort": selection.reasoning_effort,
        "selection_status": read.status.to_dict(),
    }
    deployment_ref = _deployment_ref(
        db,
        user_id=current_user.id,
        deployment_id=getattr(selection, "deployment_id", None),
    )
    if deployment_ref is not None:
        response["deployment_ref"] = deployment_ref
    return response


@router.get(
    "/memory/selections",
    response_model=UserMemoryDependencySelectionsResponse,
)
async def get_memory_dependency_selections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current user's semantic-memory dependency selections."""

    _ensure_semantic_memory_enabled()
    try:
        service = _memory_selection_service(db)
        embedding = service.get_embedding_selection(user_id=current_user.id)
        memory_llm = service.get_memory_llm_selection(user_id=current_user.id)
        db.commit()
        db.refresh(embedding)
        db.refresh(memory_llm)
        return {
            "embedding": embedding,
            "memory_llm": memory_llm,
            "embedding_provider": embedding.provider,
            "embedding_model": embedding.model,
            "embedding_vector_family": embedding.vector_family,
        }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/memory/embedding-selection",
    response_model=UserEmbeddingSelectionResponse,
)
async def set_memory_embedding_selection(
    body: UserEmbeddingSelectionUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist current user's semantic-memory embedding selection."""

    _ensure_semantic_memory_enabled()
    try:
        selection = _memory_selection_service(db).set_embedding_selection(
            user_id=current_user.id,
            provider=body.provider,
            model=body.model,
        )
        db.commit()
        db.refresh(selection)
        return selection
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/memory/llm-selection",
    response_model=UserMemoryLLMSelectionResponse,
)
async def set_memory_llm_selection(
    body: UserMemoryLLMSelectionUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist current user's semantic-memory LLM dependency selection."""

    _ensure_semantic_memory_enabled()
    try:
        selection = _memory_selection_service(db).set_memory_llm_selection(
            user_id=current_user.id,
            provider=body.provider,
            gate_model=body.gate_model,
            extraction_model=body.extraction_model,
        )
        db.commit()
        db.refresh(selection)
        return selection
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/providers/{provider}/credential",
    response_model=LLMProviderCredentialStatusResponse,
)
async def get_provider_credential(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return masked provider credential status for the current user."""

    try:
        status_obj = LLMCredentialService(db).get_masked_status(
            user_id=current_user.id,
            provider=provider,
        )
        db.commit()
        return _credential_status_response(status_obj, db=db)
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc


@router.put(
    "/providers/{provider}/credential",
    response_model=LLMProviderCredentialStatusResponse,
)
async def upsert_provider_credential(
    provider: str,
    body: UserLLMProviderCredentialUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or replace a provider credential without returning secret material."""

    try:
        status_obj = LLMCredentialService(db).upsert_api_key(
            user_id=current_user.id,
            provider=provider,
            api_key=body.api_key,
            enabled=body.enabled,
        )
        db.commit()
        return _credential_status_response(status_obj, db=db)
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc


@router.delete(
    "/providers/{provider}/credential",
    response_model=LLMProviderCredentialDeleteResponse,
)
async def delete_provider_credential(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a provider credential and clear legacy mirrors where applicable."""

    try:
        LLMCredentialService(db).delete(user_id=current_user.id, provider=provider)
        db.commit()
        return {"success": True}
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc


@router.post(
    "/providers/{provider}/credential/test",
    response_model=LLMProviderCredentialTestResponse,
)
async def test_provider_credential(
    provider: str,
    body: LLMProviderCredentialTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Test a supplied provider credential or the stored credential."""

    try:
        api_key = body.api_key.strip() if isinstance(body.api_key, str) else None
        result = LLMProviderHealthService(db).test_credential(
            user_id=current_user.id,
            provider=provider,
            api_key=api_key,
        )
        return {
            "provider": result.provider,
            "status": result.status,
            "message": result.message,
            "model_count": result.model_count,
        }
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No {provider} credential found. Please enter an API key to test.",
        ) from exc
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc


@router.post(
    "/connection-presets/{preset_id}/connection",
    response_model=LLMManagedConnectionStatusResponse,
)
async def create_managed_connection(
    preset_id: str,
    body: LLMManagedConnectionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a reviewed non-proving connection draft and optional deployment."""

    try:
        preset = ConnectionOperationRegistry().get_connection_preset(preset_id)
        if preset.id == GPT_OSS_20B_PROVING_PRESET_ID:
            raise HTTPException(
                status_code=400,
                detail="Use proving preset routes for GPT-OSS proving",
            )
        api_key = body.api_key.strip() if isinstance(body.api_key, str) else ""
        if not api_key:
            raise HTTPException(status_code=400, detail="Connection API key is required")
        non_secret_config = {"auth_mode": "bearer"}
        if preset.endpoint_config_field is not None:
            non_secret_config[preset.endpoint_config_field] = body.base_url
        connection = LLMConnectionService(db).create_draft(
            user_id=current_user.id,
            display_name=body.display_label or preset.display_name,
            connection_preset_id=preset.id,
            runtime_family_id=preset.runtime_family_id,
            serving_operator_id=preset.serving_operator_id,
            non_secret_config=non_secret_config,
        )
        LLMCredentialService(db).upsert_connection_api_key(
            user_id=current_user.id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=preset.id,
            api_key=api_key,
        )
        db.refresh(connection)
        deployment = None
        wire_model_id = (
            body.wire_model_id
            or preset.exact_wire_model_id
            or preset.canonical_model_id
        )
        if wire_model_id:
            deployment, _route = LLMInventoryService(db).register_custom_model(
                user_id=current_user.id,
                connection_id=connection.id,
                expected_connection_revision=int(connection.revision),
                wire_model_id=wire_model_id,
                display_name=body.model_label or wire_model_id,
                canonical_model_id=body.canonical_model_id or preset.canonical_model_id or None,
                requested_capabilities=(),
            )
        response = _managed_connection_status_response(
            db,
            user_id=current_user.id,
            connection=connection,
            deployment=deployment,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except (OperationRegistryError, LLMProviderServiceError) as exc:
        db.rollback()
        raise _provider_configuration_exception(
            ProviderConfigurationError(str(exc))
        ) from exc


@router.post(
    "/connection-presets/{preset_id}/connection/test",
    response_model=LLMProvingVerificationResponse,
)
async def test_managed_connection(
    preset_id: str,
    body: LLMManagedConnectionTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run a guarded health check for a reviewed connection preset."""

    try:
        preset = ConnectionOperationRegistry().get_connection_preset(preset_id)
        if preset.id == GPT_OSS_20B_PROVING_PRESET_ID:
            raise HTTPException(
                status_code=400,
                detail="Use proving preset routes for GPT-OSS proving",
            )
        connection = LLMConnectionService(db).get_owned_at_revision(
            user_id=current_user.id,
            connection_id=body.connection_ref.connection_id,
            expected_revision=body.connection_ref.expected_revision,
        )
        if connection.connection_preset_id != preset.id:
            raise HTTPException(status_code=400, detail="Connection preset mismatch")
        api_key = _managed_connection_secret(
            db,
            user_id=current_user.id,
            connection=connection,
            api_key=body.api_key,
            purpose="connection-preset-health-check",
        )
        authorized = LLMConnectionAuthorizer(db).authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=current_user.id,
            ),
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            operation=LLMConnectionOperation.HEALTH,
        )
        GuardedTransport().execute(
            LLMConnectionOperation.HEALTH,
            provider=preset.id,
            secret=ProviderSecret(provider=preset.id, value=api_key),
            operation_target=authorized.operation_target,
        )
        db.commit()
        return {
            "status": "passed",
            "code": "verified",
            "message": "Connection endpoint verified",
            "retryable": False,
            "observed_at": None,
            "expires_at": None,
            "model_present": None,
            "usage": None,
        }
    except HTTPException:
        db.rollback()
        raise
    except GuardedTransportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OperationRegistryError, LLMProviderServiceError) as exc:
        db.rollback()
        raise _provider_configuration_exception(
            ProviderConfigurationError(str(exc))
        ) from exc


@router.post(
    "/connection-presets/{preset_id}/connection/refresh",
    response_model=LLMManagedConnectionStatusResponse,
)
async def refresh_managed_connection_inventory(
    preset_id: str,
    body: LLMManagedConnectionRefreshRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Refresh reviewed connection inventory through guarded backend egress."""

    try:
        preset = ConnectionOperationRegistry().get_connection_preset(preset_id)
        if preset.id == GPT_OSS_20B_PROVING_PRESET_ID:
            raise HTTPException(
                status_code=400,
                detail="Use proving preset routes for GPT-OSS proving",
            )
        connection = LLMConnectionService(db).get_owned_at_revision(
            user_id=current_user.id,
            connection_id=body.connection_ref.connection_id,
            expected_revision=body.connection_ref.expected_revision,
        )
        if connection.connection_preset_id != preset.id:
            raise HTTPException(status_code=400, detail="Connection preset mismatch")
        api_key = _managed_connection_secret(
            db,
            user_id=current_user.id,
            connection=connection,
            api_key=body.api_key,
            purpose="connection-preset-inventory-refresh",
        )
        authorized = LLMConnectionAuthorizer(db).authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=current_user.id,
            ),
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            operation=LLMConnectionOperation.INVENTORY,
        )
        response = GuardedTransport().execute(
            LLMConnectionOperation.INVENTORY,
            provider=preset.id,
            secret=ProviderSecret(provider=preset.id, value=api_key),
            operation_target=authorized.operation_target,
        )
        deployments = LLMInventoryService(db).refresh_inventory(
            user_id=current_user.id,
            connection_id=connection.id,
            expected_connection_revision=int(connection.revision),
            discovered_model_ids=_inventory_model_ids_from_response(response.body),
        )
        status_response = _managed_connection_status_response(
            db,
            user_id=current_user.id,
            connection=connection,
            deployment=deployments[0] if len(deployments) == 1 else None,
        )
        db.commit()
        return status_response
    except HTTPException:
        db.rollback()
        raise
    except GuardedTransportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OperationRegistryError, LLMProviderServiceError) as exc:
        db.rollback()
        raise _provider_configuration_exception(
            ProviderConfigurationError(str(exc))
        ) from exc


@router.post(
    "/connection-presets/{preset_id}/connection/enable",
    response_model=LLMManagedConnectionStatusResponse,
)
async def enable_managed_connection(
    preset_id: str,
    body: LLMManagedConnectionEnableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enable a reviewed non-proving connection and return current status."""

    try:
        preset = ConnectionOperationRegistry().get_connection_preset(preset_id)
        if preset.id == GPT_OSS_20B_PROVING_PRESET_ID:
            raise HTTPException(
                status_code=400,
                detail="Use proving preset routes for GPT-OSS proving",
            )
        connections = LLMConnectionService(db)
        connection = connections.get_owned_at_revision(
            user_id=current_user.id,
            connection_id=body.connection_ref.connection_id,
            expected_revision=body.connection_ref.expected_revision,
        )
        if connection.connection_preset_id != preset.id:
            raise HTTPException(status_code=400, detail="Connection preset mismatch")
        deployment = None
        if body.deployment_ref is not None:
            deployment = LLMDeploymentService(db).get_deployment(
                user_id=current_user.id,
                deployment_id=body.deployment_ref.deployment_id,
            )
            if int(deployment.revision) != body.deployment_ref.expected_revision:
                raise HTTPException(status_code=400, detail="Deployment revision is stale")
            if str(deployment.connection_id) != str(connection.id):
                raise HTTPException(status_code=400, detail="Deployment connection mismatch")
        if connection.state == LLMConnectionState.DRAFT.value:
            connection = connections.transition_state(
                user_id=current_user.id,
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                target_state=LLMConnectionState.DISABLED,
            )
        if connection.state == LLMConnectionState.DISABLED.value:
            connection = connections.transition_state(
                user_id=current_user.id,
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                target_state=LLMConnectionState.ENABLED,
            )
        response = _managed_connection_status_response(
            db,
            user_id=current_user.id,
            connection=connection,
            deployment=deployment,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except (OperationRegistryError, LLMProviderServiceError) as exc:
        db.rollback()
        raise _provider_configuration_exception(
            ProviderConfigurationError(str(exc))
        ) from exc


@router.post(
    "/proving-presets/{preset_id}/connection",
    response_model=LLMProvingConnectionStatusResponse,
)
async def create_proving_connection(
    preset_id: str,
    body: LLMProvingConnectionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create the single GPT-OSS proving draft and deployment route."""

    _require_gpt_oss_proving_preset(preset_id)
    api_key = body.api_key.strip() if isinstance(body.api_key, str) else ""
    if not api_key:
        raise HTTPException(status_code=400, detail="Proving API key is required")
    try:
        connections = LLMConnectionService(db)
        connection = connections.create_gpt_oss_20b_proving_draft(
            user_id=current_user.id,
            display_label=body.display_label,
        )
        LLMCredentialService(db).upsert_connection_api_key(
            user_id=current_user.id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
            api_key=api_key,
        )
        db.refresh(connection)
        deployment, _route = LLMDeploymentService(
            db
        ).create_gpt_oss_20b_proving_deployment(
            user_id=current_user.id,
            connection_id=connection.id,
            expected_connection_revision=int(connection.revision),
        )
        response = _proving_status_response(
            db,
            user_id=current_user.id,
            connection=connection,
            deployment=deployment,
            verification=_not_tested_verification(),
        )
        db.commit()
        return response
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc


@router.post(
    "/proving-presets/{preset_id}/connection/test",
    response_model=LLMProvingVerificationResponse,
)
async def test_proving_connection(
    preset_id: str,
    body: LLMProvingConnectionTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run the bounded GPT-OSS proving inventory and usage probe."""

    _require_gpt_oss_proving_preset(preset_id)
    api_key = body.api_key.strip() if isinstance(body.api_key, str) else ""
    if not api_key:
        raise HTTPException(status_code=400, detail="Proving API key is required")
    try:
        deployment = LLMDeploymentService(db).get_deployment(
            user_id=current_user.id,
            deployment_id=body.deployment_ref.deployment_id,
        )
        if int(deployment.revision) != body.deployment_ref.expected_revision:
            raise HTTPException(status_code=400, detail="Deployment revision is stale")
        route = _first_route_for_deployment(
            db,
            user_id=current_user.id,
            deployment_id=deployment.id,
        )
        credential_service = LLMCredentialService(db)
        connection_ref = LLMConnectionCredentialRef(
            connection_id=body.connection_ref.connection_id,
            expected_revision=body.connection_ref.expected_revision,
        )
        stored_auth = credential_service.resolve_connection_auth(
            connection_ref,
            runtime_user_id=current_user.id,
            purpose="gpt-oss-proving-test",
            auth_mode=LLMAuthMode.BEARER,
        )
        stored_secret = stored_auth.secret.value if stored_auth.secret is not None else ""
        if stored_secret != api_key:
            raise HTTPException(
                status_code=400,
                detail="Stored proving credential must pass verification",
            )
        credential_fingerprint = credential_service.connection_credential_fingerprint(
            user_id=current_user.id,
            connection_ref=connection_ref,
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
        )
        result = LLMProviderHealthService(db).verify_gpt_oss_20b_proving_connection(
            user_id=current_user.id,
            connection_id=body.connection_ref.connection_id,
            expected_connection_revision=body.connection_ref.expected_revision,
            deployment_id=deployment.id,
            route_id=route.id,
            api_key=api_key,
            credential_fingerprint=credential_fingerprint,
        )
        response = _proving_verification_response(result)
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc


@router.post(
    "/proving-presets/{preset_id}/connection/enable",
    response_model=LLMProvingConnectionStatusResponse,
)
async def enable_proving_connection(
    preset_id: str,
    body: LLMProvingConnectionEnableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enable GPT-OSS proving only after recorded capability evidence exists."""

    _require_gpt_oss_proving_preset(preset_id)
    try:
        connections = LLMConnectionService(db)
        connection = connections.get_owned_at_revision(
            user_id=current_user.id,
            connection_id=body.connection_ref.connection_id,
            expected_revision=body.connection_ref.expected_revision,
        )
        deployment = LLMDeploymentService(db).get_deployment(
            user_id=current_user.id,
            deployment_id=body.deployment_ref.deployment_id,
        )
        if int(deployment.revision) != body.deployment_ref.expected_revision:
            raise HTTPException(status_code=400, detail="Deployment revision is stale")
        if str(deployment.connection_id) != str(connection.id):
            raise HTTPException(status_code=400, detail="Deployment route is unavailable")
        route = _first_route_for_deployment(
            db,
            user_id=current_user.id,
            deployment_id=deployment.id,
        )
        runnability = _proving_runnability(
            db,
            connection=connection,
            deployment=deployment,
            route=route,
        )
        if not runnability["runnable"]:
            raise HTTPException(
                status_code=400,
                detail="Successful proving verification is required before enablement",
            )
        verified_connection_revision = int(connection.revision)
        if connection.state == LLMConnectionState.DRAFT.value:
            connection = connections.transition_state(
                user_id=current_user.id,
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                target_state=LLMConnectionState.DISABLED,
            )
        if connection.state == LLMConnectionState.DISABLED.value:
            connection = connections.transition_state(
                user_id=current_user.id,
                connection_id=connection.id,
                expected_revision=int(connection.revision),
                target_state=LLMConnectionState.ENABLED,
            )
        elif connection.state != LLMConnectionState.ENABLED.value:
            raise HTTPException(
                status_code=400,
                detail="Proving connection is not enableable",
            )
        _refresh_proving_observation_revision(
            db,
            deployment=deployment,
            route=route,
            connection=connection,
            previous_connection_revision=verified_connection_revision,
        )
        response = _proving_status_response(
            db,
            user_id=current_user.id,
            connection=connection,
            deployment=deployment,
            verification={
                "status": "passed",
                "code": "verified",
                "message": "GPT-OSS proving endpoint verified",
                "retryable": False,
                "observed_at": None,
                "expires_at": None,
                "model_present": True,
                "usage": None,
            },
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except LLMProviderServiceError as exc:
        db.rollback()
        raise _provider_configuration_exception(exc) from exc


@router.get("/tasks/{task_id}/conversation", response_model=LLMConversationResponse)
async def get_task_conversation(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Return conversation state for a task (OpenAI provider for now)."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    # Fetch conversation row if present
    row = (
        db.query(LLMConversation)
        .filter(
            LLMConversation.task_id == task_id,
            LLMConversation.tenant_id == int(task.tenant_id),
            LLMConversation.provider == OPENAI_PROVIDER_ID,
        )
        .order_by(LLMConversation.updated_at.desc())
        .first()
    )
    if row and (row.remote_resource_id or row.conversation_id):
        lifecycle_service = LLMConversationLifecycleService(db)
        try:
            if lifecycle_service.backfill_remote_conversation_origin(row):
                db.commit()
                lifecycle_service.validate_remote_conversation_origin(
                    origin=_remote_origin_from_row(row),
                    runtime_user_id=current_user.id,
                    task_id=task_id,
                    tenant_id=int(task.tenant_id),
                )
        except LLMProviderServiceError as exc:
            raise _provider_configuration_exception(exc) from exc

    # Fall back to user's selected model when row is absent
    model = None
    try:
        model = LLMProviderSelectionService(db).get_openai_model_compat(current_user.id)
        db.commit()
    except Exception:
        model = None

    return {
        "id": (row.id if row else None),
        "provider": OPENAI_PROVIDER_ID,
        "model": (row.model if row and row.model else model),
        "conversation_id": (row.conversation_id if row else None),
        "title": (row.title if row else None),
        "status": (row.status if row else None),
        "is_active": (row.is_active if row else None),
    }


class ConversationResetResponse(BaseModel):
    success: bool
    signal_sent: bool


@router.post("/tasks/{task_id}/conversation/reset", response_model=ConversationResetResponse)
async def reset_task_conversation(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Reset stored conversation id for a task and signal running agent to reset."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_WRITE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    try:
        LLMConversationLifecycleService(db).require_remote_conversation_lifecycle(OPENAI_PROVIDER_ID)
    except ProviderConfigurationError as exc:
        raise _provider_configuration_exception(exc) from exc

    # Delete/clear any existing conversation rows (soft reset via status)
    try:
        rows = (
            db.query(LLMConversation)
            .filter(
                LLMConversation.task_id == task_id,
                LLMConversation.tenant_id == int(task.tenant_id),
                LLMConversation.provider == OPENAI_PROVIDER_ID,
            )
            .all()
        )
        for r in rows:
            r.conversation_id = None
            r.status = "reset"
        if not rows:
            # Create placeholder row for future writes
            seed = LLMConversation(
                task_id=task_id,
                tenant_id=task.tenant_id,
                user_id=current_user.id,
                provider=OPENAI_PROVIDER_ID,
                model=None,
                conversation_id=None,
                status="reset",
            )
            db.add(seed)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Failed to reset conversation in DB: %s", e)

    # Mirror workspace via ConversationManager (best-effort)
    try:
        from agent.chat.conversation_manager import ConversationManager  # lazy import
        ConversationManager(task_id).reset_openai_conversation()
    except Exception:
        logger.debug("Failed to reset conversation mirror for task %s", task_id, exc_info=True)

    result = await _runtime_input_service.append_and_signal(
        task_id,
        message="__reset_conversation",
        strict_persistence=False,
        user_id=current_user.id,
    )
    if not result.signal_sent:
        return {"success": True, "signal_sent": False}
    return {"success": True, "signal_sent": True}


# -------------------------------
# Robust Conversation Lifecycle
# -------------------------------

class ConversationCreateBody(BaseModel):
    title: str | None = None
    model: str | None = None


def _remote_origin_from_row(row: LLMConversation) -> RemoteConversationOrigin:
    """Build a lifecycle origin only from persisted row snapshots."""

    required = (
        row.connection_id,
        row.deployment_id,
        row.route_id,
        row.origin_revision,
        row.origin_deployment_revision,
        row.remote_resource_id,
        row.provider,
        row.model,
    )
    if any(value is None or value == "" for value in required):
        raise ProviderConfigurationError("Remote conversation origin is unmapped")
    return RemoteConversationOrigin(
        connection_id=str(row.connection_id),
        deployment_id=str(row.deployment_id),
        route_id=str(row.route_id),
        origin_revision=int(row.origin_revision),
        deployment_revision=int(row.origin_deployment_revision),
        provider=str(row.provider),
        model=str(row.model),
        remote_resource_id=str(row.remote_resource_id),
    )


@router.get("/tasks/{task_id}/conversations")
async def list_task_conversations(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    rows = (
        db.query(LLMConversation)
        .filter(
            LLMConversation.task_id == task_id,
            LLMConversation.tenant_id == int(task.tenant_id),
        )
        .order_by(LLMConversation.updated_at.desc())
        .all()
    )
    lifecycle_service = LLMConversationLifecycleService(db)
    try:
        for row in rows:
            if row.remote_resource_id or row.conversation_id:
                if lifecycle_service.backfill_remote_conversation_origin(row):
                    db.commit()
                    lifecycle_service.validate_remote_conversation_origin(
                        origin=_remote_origin_from_row(row),
                        runtime_user_id=current_user.id,
                        task_id=task_id,
                        tenant_id=int(task.tenant_id),
                    )
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc
    return [
        {
            "id": r.id,
            "provider": r.provider,
            "model": r.model,
            "conversation_id": r.conversation_id,
            "title": r.title,
            "status": r.status,
            "is_active": r.is_active,
        }
        for r in rows
    ]


@router.post("/tasks/{task_id}/conversations", response_model=LLMConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_task_conversation(
    task_id: int,
    body: ConversationCreateBody,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_WRITE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    lifecycle_service = LLMConversationLifecycleService(db)
    try:
        origin = lifecycle_service.create_remote_conversation(
            runtime_user_id=current_user.id,
            task_id=task_id,
            tenant_id=int(task.tenant_id),
        )
    except CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail="OpenAI API key not configured") from exc
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc

    # Deactivate existing ones and create new active row
    try:
        db.query(LLMConversation).filter(
            LLMConversation.task_id == task_id,
            LLMConversation.tenant_id == int(task.tenant_id),
        ).update({LLMConversation.is_active: False})
        row = LLMConversation(
            task_id=task_id,
            tenant_id=task.tenant_id,
            user_id=current_user.id,
            provider=origin.provider,
            model=origin.model,
            connection_id=origin.connection_id,
            deployment_id=origin.deployment_id,
            route_id=origin.route_id,
            origin_revision=origin.origin_revision,
            origin_deployment_revision=origin.deployment_revision,
            remote_resource_id=origin.remote_resource_id,
            conversation_id=origin.remote_resource_id,
            title=body.title,
            status="active",
            is_active=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist conversation: {e}")

    # Mirror to workspace
    try:
        from agent.chat.conversation_manager import ConversationManager  # lazy import
        cm = ConversationManager(task_id)
        local_id = cm.get_active_conversation_id() or cm.create_conversation(title=body.title or "Conversation")
        cm.set_openai_conversation_id(local_id, origin.remote_resource_id)
    except Exception:
        logger.debug("Failed to mirror conversation to workspace for task %s", task_id, exc_info=True)

    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "conversation_id": row.conversation_id,
        "title": row.title,
        "status": row.status,
        "is_active": row.is_active,
    }


@router.put("/tasks/{task_id}/conversations/{row_id}/activate", response_model=LLMConversationResponse)
async def activate_task_conversation(
    task_id: int,
    row_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_WRITE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    row = db.query(LLMConversation).filter(
        LLMConversation.id == row_id,
        LLMConversation.task_id == task_id,
        LLMConversation.tenant_id == int(task.tenant_id),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        lifecycle_service = LLMConversationLifecycleService(db)
        lifecycle_service.backfill_remote_conversation_origin(row)
        lifecycle_service.validate_remote_conversation_origin(
            origin=_remote_origin_from_row(row),
            runtime_user_id=current_user.id,
            task_id=task_id,
            tenant_id=int(task.tenant_id),
        )
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc
    try:
        db.query(LLMConversation).filter(
            LLMConversation.task_id == task_id,
            LLMConversation.tenant_id == int(task.tenant_id),
        ).update({LLMConversation.is_active: False})
        row.is_active = True
        row.status = "active"
        db.commit()
        db.refresh(row)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to activate conversation: {e}")

    # Mirror to workspace
    try:
        from agent.chat.conversation_manager import ConversationManager
        cm = ConversationManager(task_id)
        local_id = cm.get_active_conversation_id() or cm.create_conversation(title=row.title or "Conversation")
        cm.set_openai_conversation_id(local_id, row.conversation_id or "")
    except Exception:
        logger.debug("Failed to mirror active conversation for task %s", task_id, exc_info=True)

    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "conversation_id": row.conversation_id,
        "title": row.title,
        "status": row.status,
        "is_active": row.is_active,
    }


@router.delete("/tasks/{task_id}/conversations/{row_id}")
async def delete_task_conversation(
    task_id: int,
    row_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_WRITE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    row = db.query(LLMConversation).filter(
        LLMConversation.id == row_id,
        LLMConversation.task_id == task_id,
        LLMConversation.tenant_id == int(task.tenant_id),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    lifecycle_service = LLMConversationLifecycleService(db)
    try:
        lifecycle_service.backfill_remote_conversation_origin(row)
        origin = _remote_origin_from_row(row)
        if origin.remote_resource_id:
            lifecycle_service.delete_remote_conversation(
                origin=origin,
                runtime_user_id=current_user.id,
                task_id=task_id,
                tenant_id=int(task.tenant_id),
            )
    except LLMProviderServiceError as exc:
        raise _provider_configuration_exception(exc) from exc

    # Delete local row
    was_active = bool(row.is_active)
    try:
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete conversation: {e}")

    # If the deleted row was active, clear workspace mirror
    try:
        if was_active:
            from agent.chat.conversation_manager import ConversationManager
            cm = ConversationManager(task_id)
            cm.reset_openai_conversation()
    except Exception:
        logger.debug("Failed to clear workspace mirror after delete for task %s", task_id, exc_info=True)

    return {"success": True}
