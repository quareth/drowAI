"""Installation state service for standalone setup wizard gating.

Responsibilities:
- Resolve whether first-run setup is required from PostgreSQL state.
- Persist placeholder networking/display defaults during wizard completion.
- Repair legacy installs that predate the platform_installations table.
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config.feature_flags import get_deployment_profile
from backend.core.time_utils import utc_now
from backend.models import User
from backend.models.platform_installation import (
    PLATFORM_INSTALLATION_SINGLETON_ID,
    PlatformInstallation,
)

_CONTROL_PLANE_SETUP_PROFILES = frozenset({"dev_local", "single_host", "distributed"})
INSTALLATION_STATUS_PENDING = "pending"
INSTALLATION_STATUS_PROVISIONING = "provisioning"
INSTALLATION_STATUS_COMPLETE = "complete"
INSTALLATION_STATUS_FAILED = "failed"
_INSTALLATION_STATUSES = frozenset(
    {
        INSTALLATION_STATUS_PENDING,
        INSTALLATION_STATUS_PROVISIONING,
        INSTALLATION_STATUS_COMPLETE,
        INSTALLATION_STATUS_FAILED,
    }
)


class PlatformInstallationService:
    """Read and update singleton platform installation state."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def is_wizard_enabled(self) -> bool:
        """Return True when the setup wizard applies to the active deployment profile."""
        return str(get_deployment_profile()) in _CONTROL_PLANE_SETUP_PROFILES

    def get_record(self) -> PlatformInstallation:
        """Return the singleton installation row, creating it when missing."""
        record = self._db.get(PlatformInstallation, PLATFORM_INSTALLATION_SINGLETON_ID)
        if record is not None:
            return record

        record = PlatformInstallation(
            id=PLATFORM_INSTALLATION_SINGLETON_ID,
            deployment_profile=str(get_deployment_profile()),
            network_config={},
            display_defaults={},
        )
        self._db.add(record)
        self._db.flush()
        return record

    def get_status(self) -> str:
        """Return the normalized setup state for the singleton row."""
        record = self._db.get(PlatformInstallation, PLATFORM_INSTALLATION_SINGLETON_ID)
        if record is None:
            return INSTALLATION_STATUS_PENDING
        status = str(record.status or "").strip().lower()
        if status in _INSTALLATION_STATUSES:
            return status
        if record.completed_at is not None:
            return INSTALLATION_STATUS_COMPLETE
        return INSTALLATION_STATUS_PENDING

    def get_setup_error(self) -> str | None:
        """Return the sanitized setup error recorded by failed provisioning."""
        record = self._db.get(PlatformInstallation, PLATFORM_INSTALLATION_SINGLETON_ID)
        if record is None:
            return None
        value = str(record.setup_error or "").strip()
        return value or None

    def is_complete(self) -> bool:
        """Return True when installation has been marked complete."""
        record = self._db.get(PlatformInstallation, PLATFORM_INSTALLATION_SINGLETON_ID)
        return bool(record and record.completed_at is not None)

    def is_setup_required(self) -> bool:
        """Return True when standalone wizard must run before normal app use."""
        if not self.is_wizard_enabled():
            return False
        if self.is_complete():
            return False
        return True

    def repair_legacy_installation_if_needed(self) -> bool:
        """Mark installation complete when users exist but no installation row yet.

        Pre-wizard deployments already had users in Postgres without a
        ``platform_installations`` row. Do not re-complete when a row exists
        with ``completed_at`` cleared (e.g. dev reset of wizard state).
        """
        if self.is_complete():
            return False
        existing = self._db.get(PlatformInstallation, PLATFORM_INSTALLATION_SINGLETON_ID)
        if existing is not None:
            return False
        user_count = self._db.execute(select(func.count()).select_from(User)).scalar_one()
        if int(user_count or 0) <= 0:
            return False

        record = self.get_record()
        record.completed_at = utc_now()
        record.status = INSTALLATION_STATUS_COMPLETE
        record.setup_error = None
        record.deployment_profile = str(get_deployment_profile())
        self._db.flush()
        return True

    def update_network_config(self, network_config: Mapping[str, Any]) -> PlatformInstallation:
        """Persist placeholder networking configuration from the wizard."""
        record = self.get_record()
        record.network_config = dict(network_config)
        self._db.flush()
        return record

    def update_display_defaults(self, display_defaults: Mapping[str, Any]) -> PlatformInstallation:
        """Persist display defaults to apply to the first admin account."""
        record = self.get_record()
        record.display_defaults = dict(display_defaults)
        self._db.flush()
        return record

    def mark_provisioning(
        self,
        *,
        provisioning_metadata: Mapping[str, Any] | None = None,
    ) -> PlatformInstallation:
        """Mark setup provisioning as committed but not externally published."""
        record = self.get_record()
        record.status = INSTALLATION_STATUS_PROVISIONING
        record.setup_error = None
        record.deployment_profile = str(get_deployment_profile())
        if provisioning_metadata is not None:
            record.provisioning_metadata = dict(provisioning_metadata)
        self._db.flush()
        return record

    def mark_failed(self, *, setup_error: str) -> PlatformInstallation:
        """Record a retryable setup provisioning failure without secrets."""
        record = self.get_record()
        record.status = INSTALLATION_STATUS_FAILED
        record.setup_error = _sanitize_setup_error(setup_error)
        record.deployment_profile = str(get_deployment_profile())
        self._db.flush()
        return record

    def mark_complete(
        self,
        *,
        network_config: Mapping[str, Any] | None = None,
        display_defaults: Mapping[str, Any] | None = None,
    ) -> PlatformInstallation:
        """Mark standalone installation complete."""
        record = self.get_record()
        if network_config is not None:
            record.network_config = dict(network_config)
        if display_defaults is not None:
            record.display_defaults = dict(display_defaults)
        record.deployment_profile = str(get_deployment_profile())
        record.status = INSTALLATION_STATUS_COMPLETE
        record.setup_error = None
        record.completed_at = utc_now()
        self._db.flush()
        return record


def _sanitize_setup_error(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Setup provisioning failed."
    return text[:500]
