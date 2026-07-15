"""Tenant data management settings read/write service.

This service owns lazy initialization, validation, and response shaping for
tenant-scoped lifecycle policy settings.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.config.retention import (
    DEFAULT_REPORT_RETENTION_ENABLED,
    RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS,
    RETENTION_DAY_FIELD_BOUNDS,
    RETENTION_POLICY_DEFAULTS,
)
from backend.models.data_management import TenantDataManagementSettings
from backend.schemas.data_management import (
    TenantDataManagementSettingsResponse,
    TenantDataManagementSettingsUpdateRequest,
)

_RETENTION_POLICY_FIELDS = tuple(RETENTION_POLICY_DEFAULTS)
_RETENTION_DAY_FIELDS = tuple(
    field_name
    for field_name in _RETENTION_POLICY_FIELDS
    if field_name != "retention_batch_size_per_tenant"
)
_RETENTION_MUTABLE_FIELDS = {"report_retention_enabled", *_RETENTION_POLICY_FIELDS}


class DataManagementSettingsValidationError(ValueError):
    """Raised when a tenant data management settings update is invalid."""


class DataManagementSettingsService:
    """Read and update tenant-scoped data management settings."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def get_settings_response(self, *, tenant_id: int) -> TenantDataManagementSettingsResponse:
        """Return current settings, creating tenant defaults when absent."""

        return TenantDataManagementSettingsResponse.model_validate(
            self._get_or_create_settings(tenant_id=tenant_id)
        )

    def update_settings(
        self,
        *,
        tenant_id: int,
        payload: TenantDataManagementSettingsUpdateRequest,
    ) -> TenantDataManagementSettingsResponse:
        """Apply a validated settings update and return the saved settings."""

        settings = self._get_or_create_settings(tenant_id=tenant_id)
        update_data = payload.model_dump(exclude_unset=True)
        normalized_update = self._validate_update(update_data)

        for field_name, value in normalized_update.items():
            setattr(settings, field_name, value)

        self._db.add(settings)
        self._db.commit()
        self._db.refresh(settings)
        return TenantDataManagementSettingsResponse.model_validate(settings)

    def get_settings(self, *, tenant_id: int) -> TenantDataManagementSettings:
        """Return an ORM settings row for internal policy consumers."""

        return self._get_or_create_settings(tenant_id=tenant_id)

    def _get_or_create_settings(self, *, tenant_id: int) -> TenantDataManagementSettings:
        settings = (
            self._db.query(TenantDataManagementSettings)
            .filter(TenantDataManagementSettings.tenant_id == int(tenant_id))
            .one_or_none()
        )
        if settings is not None:
            if self._apply_missing_defaults(settings):
                self._db.add(settings)
                self._db.commit()
                self._db.refresh(settings)
            return settings

        settings = TenantDataManagementSettings(
            tenant_id=int(tenant_id),
            report_retention_enabled=DEFAULT_REPORT_RETENTION_ENABLED,
            **RETENTION_POLICY_DEFAULTS,
        )
        self._db.add(settings)
        self._db.commit()
        self._db.refresh(settings)
        return settings

    @staticmethod
    def _apply_missing_defaults(settings: TenantDataManagementSettings) -> bool:
        changed = False
        if settings.report_retention_enabled is None:
            settings.report_retention_enabled = DEFAULT_REPORT_RETENTION_ENABLED
            changed = True

        for field_name, default_value in RETENTION_POLICY_DEFAULTS.items():
            if getattr(settings, field_name) is None:
                setattr(settings, field_name, default_value)
                changed = True

        return changed

    @staticmethod
    def _validate_update(update_data: dict[str, Any]) -> dict[str, Any]:
        unknown_fields = sorted(set(update_data) - _RETENTION_MUTABLE_FIELDS)
        if unknown_fields:
            joined_fields = ", ".join(unknown_fields)
            raise DataManagementSettingsValidationError(
                f"Unsupported data management setting: {joined_fields}."
            )

        normalized_update: dict[str, Any] = {}
        if "report_retention_enabled" in update_data:
            report_retention_enabled = update_data["report_retention_enabled"]
            if report_retention_enabled is None or not isinstance(
                report_retention_enabled,
                bool,
            ):
                raise DataManagementSettingsValidationError(
                    "report_retention_enabled must be a boolean."
                )
            normalized_update["report_retention_enabled"] = report_retention_enabled

        for field_name in _RETENTION_DAY_FIELDS:
            if field_name in update_data:
                normalized_update[field_name] = _normalize_bounded_integer(
                    field_name=field_name,
                    value=update_data[field_name],
                    bounds=RETENTION_DAY_FIELD_BOUNDS,
                )

        batch_field_name = "retention_batch_size_per_tenant"
        if batch_field_name in update_data:
            normalized_update[batch_field_name] = _normalize_bounded_integer(
                field_name=batch_field_name,
                value=update_data[batch_field_name],
                bounds=RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS,
            )

        return normalized_update


def _normalize_bounded_integer(
    *,
    field_name: str,
    value: Any,
    bounds: tuple[int, int],
) -> int:
    minimum, maximum = bounds
    if value is None or isinstance(value, bool):
        raise DataManagementSettingsValidationError(f"{field_name} must be an integer.")

    try:
        normalized_value = int(value)
    except (TypeError, ValueError) as exc:
        raise DataManagementSettingsValidationError(
            f"{field_name} must be an integer."
        ) from exc

    if not minimum <= normalized_value <= maximum:
        raise DataManagementSettingsValidationError(
            f"{field_name} must be between {minimum} and {maximum}."
        )

    return normalized_value


__all__ = [
    "DataManagementSettingsService",
    "DataManagementSettingsValidationError",
]
