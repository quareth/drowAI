"""Setup wizard completion orchestration for control-plane installs."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

import backend.database as database_module
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.auth import get_password_hash
from backend.config import ACCESS_TOKEN_EXPIRE_MINUTES
from backend.config.feature_flags import get_deployment_profile
from backend.config.generated_config import update_generated_database_config
from backend.models import ExecutionSite, RunnerInstallToken, User, UserSettings
from backend.services.platform.generated_artifacts import (
    GeneratedArtifactPublisher,
    RunnerConfigArtifact,
    default_generated_artifact_publisher,
)
from backend.services.platform.installation_service import PlatformInstallationService
from backend.services.platform.setup_env import (
    build_database_url,
    resolve_database_host,
    test_database_connection,
)
from backend.services.runner_control.readiness_service import (
    RUNNER_READINESS_READY,
    RUNNER_READINESS_WAITING_FOR_RUNNER,
)
from backend.services.runner_control.registry_service import RunnerRegistryService
from backend.services.tenant.context import TenantContextResolutionError, TenantContextService
from backend.services.tenant.rls import privileged_rls_bypass

logger = logging.getLogger(__name__)


class SetupCompletionError(RuntimeError):
    """Raised when setup completion fails with a stable message."""


@dataclass(frozen=True, slots=True)
class SetupCompletionResult:
    admin_username: str
    redirect_path: str
    runner_site_created: bool
    runner_enrollment_published: bool
    runner_readiness: str


@dataclass(frozen=True, slots=True)
class _ProvisionedSetup:
    admin_username: str
    admin_user_id: int
    tenant_id: int
    install_token: str | None
    execution_site_id: UUID | None
    runner_artifact: RunnerConfigArtifact | None


class SetupProvisioningService:
    """Provision durable setup state before publishing generated artifacts."""

    def __init__(
        self,
        db: Session,
        *,
        artifact_publisher: GeneratedArtifactPublisher | None = None,
    ) -> None:
        self._db = db
        self._installation = PlatformInstallationService(db)
        self._artifact_publisher = artifact_publisher or default_generated_artifact_publisher(
            deployment_profile=str(get_deployment_profile())
        )

    def complete(
        self,
        *,
        database: Mapping[str, Any],
        security: Mapping[str, Any],
        display: Mapping[str, Any],
        network: Mapping[str, Any],
        runner: Mapping[str, Any],
    ) -> SetupCompletionResult:
        """Complete setup by committing DB state before generated files appear."""
        if self._installation.is_complete():
            raise SetupCompletionError("Setup has already been completed.")

        provisioned = self._commit_provisioning_state(
            security=security,
            display=display,
            network=network,
            runner=runner,
        )

        try:
            self._rotate_database_credentials(
                db_name=str(database["db_name"]).strip(),
                db_user=str(database["db_user"]).strip(),
                db_password=str(database["db_password"]),
                db_host=str(database.get("db_host") or resolve_database_host()).strip(),
                db_port=int(database.get("db_port") or 5432),
            )
            runner_enrollment_published = False
            if provisioned.runner_artifact is not None:
                runner_enrollment_published = (
                    self._artifact_publisher.publish_runner_config(provisioned.runner_artifact)
                    is not None
                )
        except Exception as exc:
            self._mark_failed("Setup artifact publication failed.")
            raise SetupCompletionError("Setup artifact publication failed.") from exc

        self._installation.mark_complete(
            network_config=network,
            display_defaults=display,
        )
        self._db.commit()

        logger.info("Control-plane setup completed for admin user %s", provisioned.admin_username)
        return SetupCompletionResult(
            admin_username=provisioned.admin_username,
            redirect_path="/auth",
            runner_site_created=provisioned.execution_site_id is not None,
            runner_enrollment_published=runner_enrollment_published,
            runner_readiness=self._runner_readiness_for_setup(
                tenant_id=provisioned.tenant_id,
            ),
        )

    def create_runner_provisioning(
        self,
        *,
        admin_user_id: int,
        runner: Mapping[str, Any],
        network: Mapping[str, Any],
    ) -> dict[str, str]:
        """Provision execution site and install token during the runner wizard step."""
        if self._installation.is_complete():
            raise SetupCompletionError("Setup has already been completed.")

        admin_tenant_id = self._resolve_admin_tenant_id(admin_user_id=int(admin_user_id))
        registry = RunnerRegistryService(self._db)
        site = self._find_or_create_execution_site(
            registry=registry,
            tenant_id=admin_tenant_id,
            name=str(runner.get("site_name") or "Default Site"),
            slug=str(runner.get("site_slug") or "default-site"),
            network_label=str(network.get("kali_docker_network") or "") or None,
        )
        self._revoke_existing_setup_tokens(tenant_id=admin_tenant_id, execution_site_id=site.id)
        issued = registry.issue_install_token(
            tenant_id=admin_tenant_id,
            execution_site_id=site.id,
            created_by_user_id=int(admin_user_id),
        )
        self._db.commit()
        return {
            "execution_site_id": str(site.id),
            "install_token": issued.plaintext_token,
        }

    def _commit_provisioning_state(
        self,
        *,
        security: Mapping[str, Any],
        display: Mapping[str, Any],
        network: Mapping[str, Any],
        runner: Mapping[str, Any],
    ) -> _ProvisionedSetup:
        admin_username = str(security["admin_username"]).strip()
        admin_email = str(security.get("admin_email") or "").strip() or None
        admin_password = str(security["admin_password"])
        session_timeout = int(security.get("session_timeout") or ACCESS_TOKEN_EXPIRE_MINUTES)

        with privileged_rls_bypass(self._db, scope="repair", actor_type="system"):
            admin_user = self._get_or_update_admin_user(
                username=admin_username,
                email=admin_email,
                password=admin_password,
            )
            admin_tenant_id = self._ensure_admin_tenant(user_id=int(admin_user.id))
            self._upsert_admin_settings(
                user_id=int(admin_user.id),
                session_timeout=session_timeout,
                display=display,
            )

            install_token: str | None = None
            execution_site_id: UUID | None = None
            if bool(runner.get("create_site", True)):
                registry = RunnerRegistryService(self._db)
                site = self._find_or_create_execution_site(
                    registry=registry,
                    tenant_id=admin_tenant_id,
                    name=str(runner.get("site_name") or "Default Site"),
                    slug=str(runner.get("site_slug") or "default-site"),
                    network_label=str(network.get("kali_docker_network") or "") or None,
                )
                self._revoke_existing_setup_tokens(
                    tenant_id=admin_tenant_id,
                    execution_site_id=site.id,
                )
                issued = registry.issue_install_token(
                    tenant_id=admin_tenant_id,
                    execution_site_id=site.id,
                    created_by_user_id=int(admin_user.id),
                )
                install_token = issued.plaintext_token
                execution_site_id = site.id

            self._installation.mark_provisioning(
                provisioning_metadata={
                    "admin_user_id": int(admin_user.id),
                    "tenant_id": admin_tenant_id,
                    "execution_site_id": str(execution_site_id) if execution_site_id else None,
                }
            )
            self._db.commit()

        runner_artifact = (
            RunnerConfigArtifact(
                install_token=install_token,
                network=network,
            )
            if install_token
            else None
        )
        return _ProvisionedSetup(
            admin_username=admin_username,
            admin_user_id=int(admin_user.id),
            tenant_id=admin_tenant_id,
            install_token=install_token,
            execution_site_id=execution_site_id,
            runner_artifact=runner_artifact,
        )

    def _get_or_update_admin_user(self, *, username: str, email: str | None, password: str) -> User:
        admin_user = self._db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if admin_user is None:
            admin_user = User(
                username=username,
                password=get_password_hash(password),
                email=email,
            )
            self._db.add(admin_user)
        else:
            admin_user.password = get_password_hash(password)
            admin_user.email = email
        self._db.flush()
        return admin_user

    def _ensure_admin_tenant(self, *, user_id: int) -> int:
        tenant_service = TenantContextService(self._db)
        admin_membership = tenant_service.ensure_default_membership(user_id=int(user_id))
        return int(admin_membership.tenant_id)

    def _upsert_admin_settings(
        self,
        *,
        user_id: int,
        session_timeout: int,
        display: Mapping[str, Any],
    ) -> None:
        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == int(user_id))
        ).scalar_one_or_none()
        if settings is None:
            settings = UserSettings(user_id=int(user_id))
            self._db.add(settings)
        settings.session_timeout = session_timeout
        settings.timezone = str(display.get("timezone") or "UTC")
        settings.enable_ai = True
        self._db.flush()

    def _find_or_create_execution_site(
        self,
        *,
        registry: RunnerRegistryService,
        tenant_id: int,
        name: str,
        slug: str,
        network_label: str | None,
    ) -> ExecutionSite:
        existing = self._db.execute(
            select(ExecutionSite).where(
                ExecutionSite.tenant_id == tenant_id,
                ExecutionSite.slug == slug,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.name = name
            existing.network_label = network_label
            existing.status = "active"
            self._db.flush()
            return existing
        return registry.create_execution_site(
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            network_label=network_label,
        )

    def _revoke_existing_setup_tokens(self, *, tenant_id: int, execution_site_id: UUID) -> None:
        self._db.execute(
            update(RunnerInstallToken)
            .where(
                RunnerInstallToken.tenant_id == tenant_id,
                RunnerInstallToken.execution_site_id == execution_site_id,
                RunnerInstallToken.status == "issued",
                RunnerInstallToken.used_at.is_(None),
            )
            .values(status="revoked")
            .execution_options(synchronize_session=False)
        )
        self._db.flush()

    def _mark_failed(self, message: str) -> None:
        self._db.rollback()
        self._installation.mark_failed(setup_error=message)
        self._db.commit()

    def _resolve_admin_tenant_id(self, *, admin_user_id: int) -> int:
        tenant_service = TenantContextService(self._db)
        try:
            context = tenant_service.resolve_for_user(
                user_id=int(admin_user_id),
                allow_ambiguous=False,
            )
        except TenantContextResolutionError as exc:
            if exc.code != "no_active_membership":
                raise SetupCompletionError(str(exc)) from exc
            membership = tenant_service.ensure_default_membership(user_id=int(admin_user_id))
            return int(membership.tenant_id)
        if context is None:
            raise SetupCompletionError("Admin tenant context could not be resolved.")
        return int(context.tenant_id)

    def _runner_readiness_for_setup(self, *, tenant_id: int) -> str:
        connectivity = RunnerRegistryService(self._db).list_runner_site_connectivity(tenant_id=tenant_id)
        if any(summary.connectivity_status == "connected" for summary in connectivity.values()):
            return RUNNER_READINESS_READY
        return RUNNER_READINESS_WAITING_FOR_RUNNER

    def _rotate_database_credentials(
        self,
        *,
        db_name: str,
        db_user: str,
        db_password: str,
        db_host: str,
        db_port: int,
    ) -> None:
        """Apply the wizard DB password and rebind future DB sessions."""
        database_url = build_database_url(
            db_name=db_name,
            db_user=db_user,
            db_password=db_password,
            db_host=db_host,
            db_port=db_port,
        )
        bind = self._db.get_bind()
        dialect_name = str(getattr(getattr(bind, "dialect", None), "name", "") or "")
        if dialect_name != "postgresql":
            logger.info("Skipping database password rotation for non-PostgreSQL setup database.")
            return

        escaped_user = db_user.replace('"', '""')
        try:
            with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(
                    f'ALTER USER "{escaped_user}" WITH PASSWORD %s',
                    (db_password,),
                )
            test_database_connection(
                db_name=db_name,
                db_user=db_user,
                db_password=db_password,
                db_host=db_host,
                db_port=db_port,
            )
        except Exception as exc:
            raise SetupCompletionError("Database password rotation failed.") from exc

        env = update_generated_database_config(
            postgres_password=db_password,
            postgres_user=db_user,
            postgres_db=db_name,
            postgres_host=db_host,
            postgres_port=db_port,
        )
        os.environ.update(env)
        database_module.reconfigure_database(env.get("DATABASE_URL") or database_url)


class SetupCompletionService(SetupProvisioningService):
    """Backward-compatible name for setup provisioning orchestration."""
