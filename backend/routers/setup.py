"""Setup wizard API endpoints for control-plane first-run configuration."""

import logging
import secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from backend.config import DEBUG
from backend.config.feature_flags import get_deployment_profile
from backend.core.rate_limiter import rate_limit
from backend.database import engine, get_db
from backend.services.platform.installation_service import PlatformInstallationService
from backend.services.platform.management_url import ManagementUrlError, ManagementUrlService
from backend.services.platform.setup_completion_service import (
    SetupCompletionError,
    SetupCompletionService,
)
from backend.services.platform.background_services import start_background_services
from backend.services.platform.setup_env import (
    ping_configured_database,
    resolve_configured_database_identity,
    resolve_database_host,
)
from backend.services.runner_control.registry_service import RunnerRegistryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


class DatabaseConfig(BaseModel):
    db_name: str = "drowai"
    db_user: str = "drowai_user"
    db_password: str
    db_host: Optional[str] = None
    db_port: int = 5432

    @field_validator("db_password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Database password must be at least 8 characters long")
        return value


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_timeout: int = 30
    admin_username: str = "admin"
    admin_email: str = "admin@drowai.local"
    admin_password: str

    @field_validator("admin_password")
    @classmethod
    def validate_admin_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Admin password must be at least 8 characters long")
        return value


class DisplayConfig(BaseModel):
    timezone: str = "UTC"


class NetworkConfig(BaseModel):
    management_ip: Optional[str] = None
    management_url: Optional[str] = None
    gateway: Optional[str] = None
    dns_servers: Optional[str] = None
    domain: Optional[str] = None
    kali_docker_network: Optional[str] = None


class RunnerConfig(BaseModel):
    create_site: bool = True
    site_name: str = "Default Site"
    site_slug: str = "default-site"


class SetupConfig(BaseModel):
    database: DatabaseConfig
    security: SecurityConfig
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)


class SetupStatus(BaseModel):
    setup_required: bool
    wizard_enabled: bool
    installation_complete: bool
    installation_status: str
    setup_error: str | None
    deployment_profile: str
    database_accessible: bool
    runner_connected: bool


class SetupCompleteResponse(BaseModel):
    """Primary setup completion response without enrollment internals."""

    model_config = ConfigDict(extra="forbid")

    status: str
    message: str
    redirect: str
    admin_username: str
    runner_site_created: bool
    runner_enrollment_published: bool
    runner_readiness: str
    runtime_services_started: bool
    restart_required: bool


def _runner_connected(db: Session, *, now: datetime | None = None) -> bool:
    return RunnerRegistryService(db).has_connected_runner_site(now=now)


@router.get("/status", response_model=SetupStatus)
async def get_setup_status(db: Session = Depends(get_db)) -> SetupStatus:
    """Return DB-backed setup status for control-plane installs."""
    installation = PlatformInstallationService(db)
    return SetupStatus(
        setup_required=installation.is_setup_required(),
        wizard_enabled=installation.is_wizard_enabled(),
        installation_complete=installation.is_complete(),
        installation_status=installation.get_status(),
        setup_error=installation.get_setup_error(),
        deployment_profile=str(get_deployment_profile()),
        database_accessible=ping_configured_database(engine),
        runner_connected=_runner_connected(db),
    )


@router.post("/validate-database")
@rate_limit(max_calls=20, window=60)
async def validate_database(config: DatabaseConfig = Body()) -> dict[str, str]:
    """Validate database input and current generated DB connectivity."""
    _ensure_configured_database_identity(config)
    if not ping_configured_database(engine):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured database is not reachable yet",
        )
    return {"status": "valid", "message": "Database configuration can be applied"}


@router.post("/test-connection")
@rate_limit(max_calls=20, window=60)
async def test_database_connection_endpoint(config: DatabaseConfig = Body()) -> dict[str, str]:
    """Alias for validate-database used by older wizard clients."""
    return await validate_database(config)


@router.post("/generate-secrets")
@rate_limit(max_calls=10, window=60)
async def generate_secrets() -> dict[str, str]:
    """Generate user-facing wizard passwords."""
    return {
        "db_password": secrets.token_urlsafe(32),
        "admin_password": secrets.token_urlsafe(16),
    }


@router.post("/complete", response_model=SetupCompleteResponse)
@rate_limit(max_calls=5, window=300)
async def complete_setup(
    request: Request,
    config: SetupConfig = Body(),
    db: Session = Depends(get_db),
) -> SetupCompleteResponse:
    """Complete setup and persist generated configuration."""
    installation = PlatformInstallationService(db)
    if not installation.is_wizard_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Setup wizard is disabled")
    if installation.is_complete():
        runtime_services_started = await _reconcile_background_services()
        return SetupCompleteResponse(
            status="success",
            message="Setup has already been completed",
            redirect="/auth",
            admin_username=config.security.admin_username,
            runner_site_created=False,
            runner_enrollment_published=False,
            runner_readiness="ready" if _runner_connected(db) else "waiting_for_runner",
            runtime_services_started=runtime_services_started,
            restart_required=not runtime_services_started,
        )

    _ensure_configured_database_identity(config.database)

    try:
        management_url_service = ManagementUrlService()
        configured_management_url = str(config.network.management_url or "").strip()
        if configured_management_url:
            management_url_service.set_url(configured_management_url)
        else:
            management_url_service.set_url(
                management_url_service.resolve(request=request).management_url
            )

        service = SetupCompletionService(db)
        result = service.complete(
            database=config.database.model_dump(),
            security=config.security.model_dump(),
            display=config.display.model_dump(),
            network=config.network.model_dump(),
            runner=config.runner.model_dump(),
        )
    except (SetupCompletionError, ManagementUrlError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Setup completion failed")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Setup failed",
        ) from exc

    runtime_services_started = await _reconcile_background_services()

    return SetupCompleteResponse(
        status="success",
        message="Setup completed successfully",
        redirect=result.redirect_path,
        admin_username=result.admin_username,
        runner_site_created=result.runner_site_created,
        runner_enrollment_published=result.runner_enrollment_published,
        runner_readiness=result.runner_readiness,
        runtime_services_started=runtime_services_started,
        restart_required=not runtime_services_started,
    )


def _ensure_configured_database_identity(config: DatabaseConfig) -> None:
    """Reject database identities that setup cannot provision or apply."""
    configured_db_name, configured_db_user = resolve_configured_database_identity(engine)
    if config.db_name.strip() != configured_db_name or config.db_user.strip() != configured_db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database name and username must match the configured database",
        )


async def _reconcile_background_services() -> bool:
    """Ensure process-local background services are running after setup."""
    try:
        return await start_background_services()
    except Exception:
        logger.exception("Setup completed, but background services failed to start")
        return False


@router.post("/skip-wizard", response_model=SetupCompleteResponse)
@rate_limit(max_calls=3, window=300)
async def skip_setup_wizard(request: Request, db: Session = Depends(get_db)) -> SetupCompleteResponse:
    """Skip wizard with generated defaults in debug mode only."""
    if not DEBUG:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Skip wizard is only available when DEBUG=true",
        )

    default_config = SetupConfig(
        database=DatabaseConfig(
            db_name="drowai",
            db_user="drowai_user",
            db_password=secrets.token_urlsafe(32),
            db_host=resolve_database_host(),
        ),
        security=SecurityConfig(
            session_timeout=30,
            admin_username="admin",
            admin_email="admin@drowai.local",
            admin_password=secrets.token_urlsafe(16),
        ),
    )
    return await complete_setup(request, default_config, db)


@router.get("/health")
async def setup_health_check() -> dict[str, Any]:
    """Health check endpoint for setup wizard."""
    return {
        "status": "healthy",
        "message": "Setup wizard API is running",
        "endpoints": [
            "/api/setup/status",
            "/api/setup/validate-database",
            "/api/setup/test-connection",
            "/api/setup/generate-secrets",
            "/api/setup/complete",
            "/api/setup/skip-wizard",
        ],
    }
