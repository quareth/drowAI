"""
User Settings API Router
Handles user settings including OpenAI API key configuration
"""
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import Optional
import logging
from pydantic import BaseModel

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from agent.providers.llm.profiles import OPENAI_DEFAULT_MODEL_ID

from ..database import get_db
from ..auth import get_current_user
from ..models import User, UserSettings, UserSettingsUpdate, UserSettingsResponse
from ..services.llm_provider import (
    CredentialEncryptionError,
    CredentialNotFoundError,
    LLMProviderHealthService,
    LLMProviderCatalogService,
    LLMCredentialService,
    LLMProviderSelectionService,
    ProviderConfigurationError,
)
from ..services.llm_provider.credential_service import (
    decrypt_api_key as decrypt_provider_api_key,
    encrypt_api_key as encrypt_provider_api_key,
    get_encryption_key as get_provider_encryption_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])
GPT5_DEFAULT_MODEL = OPENAI_DEFAULT_MODEL_ID

def get_encryption_key() -> bytes:
    """Compatibility wrapper for the provider credential encryption key."""

    return get_provider_encryption_key()

def encrypt_api_key(api_key: str) -> str:
    """Compatibility wrapper for provider credential encryption."""

    try:
        return encrypt_provider_api_key(api_key)
    except CredentialEncryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encrypt API key",
        ) from exc

def decrypt_api_key(encrypted_key: str) -> str:
    """Compatibility wrapper for provider credential decryption."""

    return decrypt_provider_api_key(encrypted_key)


def normalize_openai_model_identifier(model: Optional[str]) -> Optional[str]:
    """Normalize user-supplied model identifiers for policy checks/storage."""
    if model is None:
        return None
    normalized = model.strip().lower()
    return normalized or None


def is_supported_openai_model(model: Optional[str]) -> bool:
    """Return True for current settings-selectable OpenAI models."""

    normalized = normalize_openai_model_identifier(model)
    if not normalized:
        return False
    try:
        LLMProviderCatalogService().require_selectable_model(OPENAI_PROVIDER_ID, normalized)
    except ProviderConfigurationError:
        return False
    return True


def _normalize_openai_model(model: Optional[str]) -> str:
    """Normalize null/legacy model values to the enforced GPT-5 default."""
    normalized = normalize_openai_model_identifier(model)
    if not is_supported_openai_model(normalized):
        return GPT5_DEFAULT_MODEL
    return normalized or GPT5_DEFAULT_MODEL


def _reconcile_legacy_openai_model(settings: UserSettings, db: Session) -> None:
    """Idempotently migrate legacy per-user model values during settings reads."""
    normalized_model = _normalize_openai_model(getattr(settings, "openai_model", None))
    changed = False
    if settings.openai_model != normalized_model:
        settings.openai_model = normalized_model
        changed = True
    if not settings.enable_ai:
        settings.enable_ai = True
        changed = True
    if not changed:
        return
    db.add(settings)
    db.commit()
    db.refresh(settings)

@router.get("/", response_model=UserSettingsResponse)
async def get_user_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user's settings"""
    try:
        result = db.execute(
            select(UserSettings).where(UserSettings.user_id == current_user.id)
        )
        settings = result.scalar_one_or_none()
        
        if not settings:
            # Create default settings for user
            settings = UserSettings(
                user_id=current_user.id,
                openai_model=GPT5_DEFAULT_MODEL,
                enable_ai=True,
                session_timeout=1800,
                theme="dark",
                timezone="UTC",
            )
            db.add(settings)
            db.commit()
            db.refresh(settings)
        else:
            _reconcile_legacy_openai_model(settings, db)
        
        # Create response with masked API keys for security
        response_data = {
            "id": settings.id,
            "user_id": settings.user_id,
            "openai_api_key": "***" if getattr(settings, 'openai_api_key', None) else None,
            "openai_model": settings.openai_model,
            "enable_ai": True,
            "shodan_api_key": "***" if getattr(settings, 'shodan_api_key', None) else None,
            "session_timeout": settings.session_timeout,
            "theme": settings.theme,
            "timezone": settings.timezone,
            "created_at": settings.created_at,
            "updated_at": settings.updated_at
        }
        
        return UserSettingsResponse(**response_data)
        
    except Exception as e:
        logger.error(f"Failed to get user settings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user settings"
        )

@router.put("/", response_model=UserSettingsResponse)
async def update_user_settings(
    settings_update: UserSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update user settings"""
    try:
        result = db.execute(
            select(UserSettings).where(UserSettings.user_id == current_user.id)
        )
        settings = result.scalar_one_or_none()
        
        if not settings:
            # Create new settings
            settings = UserSettings(user_id=current_user.id)
            db.add(settings)
        
        # Update fields if provided
        update_data = settings_update.dict(exclude_unset=True)
        credential_service = LLMCredentialService(db)
        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        
        for field, value in update_data.items():
            if field == "enable_ai":
                continue
            if field == "openai_api_key":
                if value:
                    credential_service.upsert_api_key(
                        user_id=current_user.id,
                        provider=OPENAI_PROVIDER_ID,
                        api_key=value,
                    )
                else:
                    credential_service.disable(
                        user_id=current_user.id,
                        provider=OPENAI_PROVIDER_ID,
                    )
                db.refresh(settings)
            elif field == "shodan_api_key" and value:
                # Encrypt API key before storage
                setattr(settings, field, encrypt_api_key(value))
            elif field == "openai_model" and value is not None:
                if not is_supported_openai_model(value):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Invalid OpenAI model. Only GPT-5 family models are supported.",
                    )
                selection_service.set_selection(
                    user_id=current_user.id,
                    provider=OPENAI_PROVIDER_ID,
                    model=value,
                    require_enabled_credential=False,
                )
                db.refresh(settings)
            else:
                setattr(settings, field, value)
        
        db.commit()
        db.refresh(settings)
        if not settings.enable_ai:
            settings.enable_ai = True
            db.add(settings)
            db.commit()
            db.refresh(settings)
        
        # Return response without actual API keys
        response_data = {
            "id": settings.id,
            "user_id": settings.user_id,
            "openai_api_key": "***" if getattr(settings, 'openai_api_key', None) else None,
            "openai_model": settings.openai_model,
            "enable_ai": True,
            "shodan_api_key": "***" if getattr(settings, 'shodan_api_key', None) else None,
            "session_timeout": settings.session_timeout,
            "theme": settings.theme,
            "timezone": settings.timezone,
            "created_at": settings.created_at,
            "updated_at": settings.updated_at
        }
        
        return UserSettingsResponse(**response_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update user settings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user settings"
        )

class TestOpenAIRequest(BaseModel):
    openai_api_key: Optional[str] = None

@router.post("/test-openai")
async def test_openai_connection(
    request: TestOpenAIRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Test OpenAI API connection with provided API key or stored key"""
    try:
        logger.info(f"Testing OpenAI connection for user {current_user.id}")
        logger.info(f"Request data: openai_api_key={'provided' if request.openai_api_key else 'null'}")

        api_key = request.openai_api_key.strip() if isinstance(request.openai_api_key, str) else None
        result = LLMProviderHealthService(db).test_credential(
            user_id=current_user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key=api_key,
        )
        logger.info("OpenAI API test successful: %s models found", result.model_count or 0)
        return {
            "status": result.status,
            "message": result.message,
            "model_count": result.model_count or 0,
        }
        
    except HTTPException:
        raise
    except CredentialNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OpenAI API key found. Please enter an API key to test.",
        )
    except ProviderConfigurationError as exc:
        detail = str(exc)
        status_code = status.HTTP_429_TOO_MANY_REQUESTS if "rate limit" in detail.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail)
    except Exception as e:
        logger.error(f"Failed to test OpenAI connection: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to test OpenAI connection"
        )

def get_user_openai_key(user_id: int, db: Session) -> str:
    """Get decrypted OpenAI API key for a user via provider-neutral storage."""

    return LLMCredentialService(db).get_openai_api_key_compat(user_id)

def get_user_openai_model(user_id: int, db: Session) -> str:
    """Get user's selected OpenAI model via provider-neutral selection."""

    return LLMProviderSelectionService(db).get_openai_model_compat(user_id)
