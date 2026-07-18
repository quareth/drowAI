"""LLM selection and task runtime control routes.

This router exposes user-facing model selection along with task-scoped runtime
controls such as model switching and conversation reset.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Dict, Any
import logging
from pydantic import BaseModel

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef

from ..auth import get_current_user
from ..config.feature_flags import is_semantic_memory_runtime_enabled
from ..database import get_db
from ..models import User, LLMConversation, LLMConversationResponse
from ..schemas.llm import (
    LLMModelCatalogResponse,
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
    LLMConnectionService,
    LLMConversationLifecycleService,
    LLMDeploymentService,
    LLMProviderCatalogService,
    LLMProviderHealthService,
    LLMProviderMigrationService,
    LLMProviderServiceError,
    LLMProviderSelectionService,
    ProviderConfigurationError,
    ReportingLLMSelectionService,
)
from ..services.tenant.authorization import ACTION_CHAT_READ, ACTION_CHAT_WRITE, ACTION_TASK_CONTROL
from ..services.tenant.context import TenantRequestContext
from ..services.tenant.dependencies import get_tenant_request_context
from ..services.task.runtime_input_service import TaskRuntimeInputService
from ..services.usage_tracking.pricing_registry import get_pricing_quote
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
        return {"deployment_ref": None, "runnable": False}
    connection, deployment = target
    return {
        "deployment_ref": {
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
    response = {
        "providers": [
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
                        "pricingStatus": get_pricing_quote(
                            ProviderModelRef(provider.id, model.id),
                            api_surface=model.api_surface,
                        ).status,
                        **_catalog_deployment_fields(
                            provider=provider.id,
                            model=model.id,
                            deployment_map=deployment_map,
                            credential_runnable=bool(
                                credential_statuses[provider.id].enabled
                                and credential_statuses[provider.id].has_api_key
                            ),
                        ),
                    }
                    for model in provider.models
                ],
                "defaultModel": provider.default_model,
            }
            for provider in providers
        ]
    }
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


class TaskSwitchRequest(BaseModel):
    provider: str | None = None
    model: str


@router.post("/tasks/{task_id}/switch", deprecated=True)
async def switch_task_model(
    task_id: int,
    body: TaskSwitchRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Persist next-turn user selection for deprecated task-switch callers."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    if not body.model or not isinstance(body.model, str):
        raise HTTPException(status_code=400, detail="Model is required")
    requested_provider: str | None = (
        body.provider.strip()
        if isinstance(body.provider, str) and body.provider.strip()
        else None
    )
    requested_model = body.model.strip()
    try:
        selection_service = LLMProviderSelectionService(db)
        if requested_provider is None:
            requested_provider = selection_service.get_selection(current_user.id).provider
        selection = selection_service.set_selection(
            user_id=current_user.id,
            provider=requested_provider,
            model=requested_model,
            require_enabled_credential=False,
        )
        db.commit()
        db.refresh(selection)
    except LLMProviderServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "User %s updated conversation selection via deprecated task %s switch facade",
        current_user.id,
        task_id,
    )
    return {
        "success": True,
        "deprecated": True,
        "effective_from": "next_submitted_turn",
        "provider": selection.provider,
        "model": selection.model,
        "signal_sent": False,
    }


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
        try:
            LLMConversationLifecycleService(db).validate_remote_conversation_origin(
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
        LLMConversationLifecycleService(db).validate_remote_conversation_origin(
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
