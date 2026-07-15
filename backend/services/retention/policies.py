"""Effective tenant retention policy resolution.

This module converts tenant data-management settings plus named defaults into
immutable policy objects for retention orchestrators and module executors. It
only reads tenant policy settings and does not inspect cleanup candidate tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.orm import Session

from backend.config.retention import (
    DEFAULT_REPORT_RETENTION_ENABLED,
    RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS,
    RETENTION_DAY_FIELD_BOUNDS,
    RETENTION_POLICY_DEFAULTS,
)
from backend.models.data_management import TenantDataManagementSettings


class RetentionPolicyValidationError(ValueError):
    """Raised when an effective retention policy value is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectiveRetentionPolicy:
    """Immutable tenant-scoped retention policy consumed by executors."""

    tenant_id: int
    report_retention_enabled: bool
    operational_log_retention_days: int
    runner_control_retention_days: int
    checkpoint_retention_days_after_terminal: int
    task_retention_days_after_terminal: int
    chat_transcript_retention_days_after_terminal: int
    artifact_payload_retention_days: int
    artifact_metadata_retention_days_after_terminal: int
    report_history_retention_days: int
    report_job_retention_days: int
    task_memo_history_retention_days: int
    semantic_memory_stale_retention_days: int
    usage_record_retention_days: int
    retention_batch_size_per_tenant: int

    def __post_init__(self) -> None:
        if self.tenant_id < 1:
            raise RetentionPolicyValidationError("tenant_id must be positive")
        if not isinstance(self.report_retention_enabled, bool):
            raise RetentionPolicyValidationError(
                "report_retention_enabled must be a boolean"
            )
        for field_name in RETENTION_POLICY_DEFAULTS:
            bounds = (
                RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS
                if field_name == "retention_batch_size_per_tenant"
                else RETENTION_DAY_FIELD_BOUNDS
            )
            _normalize_bounded_integer(
                field_name=field_name,
                value=getattr(self, field_name),
                bounds=bounds,
            )


def resolve_effective_retention_policy(
    *,
    tenant_id: int,
    settings: TenantDataManagementSettings | None = None,
    defaults: Mapping[str, int] = RETENTION_POLICY_DEFAULTS,
    default_report_retention_enabled: bool = DEFAULT_REPORT_RETENTION_ENABLED,
) -> EffectiveRetentionPolicy:
    """Return one immutable effective retention policy for a tenant."""

    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    if settings is not None and int(settings.tenant_id) != normalized_tenant_id:
        raise RetentionPolicyValidationError("settings tenant_id does not match tenant")

    values = {
        field_name: _resolve_bounded_policy_value(
            field_name=field_name,
            raw_value=(
                getattr(settings, field_name)
                if settings is not None
                else defaults[field_name]
            ),
            defaults=defaults,
        )
        for field_name in RETENTION_POLICY_DEFAULTS
    }
    report_retention_enabled = (
        settings.report_retention_enabled
        if settings is not None and settings.report_retention_enabled is not None
        else default_report_retention_enabled
    )

    return EffectiveRetentionPolicy(
        tenant_id=normalized_tenant_id,
        report_retention_enabled=_normalize_bool(
            field_name="report_retention_enabled",
            value=report_retention_enabled,
        ),
        **values,
    )


def resolve_effective_retention_policy_for_tenant(
    db: Session,
    *,
    tenant_id: int,
    defaults: Mapping[str, int] = RETENTION_POLICY_DEFAULTS,
    default_report_retention_enabled: bool = DEFAULT_REPORT_RETENTION_ENABLED,
) -> EffectiveRetentionPolicy:
    """Resolve policy from the tenant settings row, falling back to defaults."""

    normalized_tenant_id = _normalize_tenant_id(tenant_id)
    settings = (
        db.query(TenantDataManagementSettings)
        .filter(TenantDataManagementSettings.tenant_id == normalized_tenant_id)
        .one_or_none()
    )
    return resolve_effective_retention_policy(
        tenant_id=normalized_tenant_id,
        settings=settings,
        defaults=defaults,
        default_report_retention_enabled=default_report_retention_enabled,
    )


def _resolve_bounded_policy_value(
    *,
    field_name: str,
    raw_value: object,
    defaults: Mapping[str, int],
) -> int:
    fallback_value = defaults.get(field_name)
    value = fallback_value if raw_value is None else raw_value
    bounds = (
        RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS
        if field_name == "retention_batch_size_per_tenant"
        else RETENTION_DAY_FIELD_BOUNDS
    )
    return _normalize_bounded_integer(
        field_name=field_name,
        value=value,
        bounds=bounds,
    )


def _normalize_bounded_integer(
    *,
    field_name: str,
    value: object,
    bounds: tuple[int, int],
) -> int:
    if value is None or isinstance(value, bool):
        raise RetentionPolicyValidationError(f"{field_name} must be an integer")

    try:
        normalized_value = int(value)
    except (TypeError, ValueError) as exc:
        raise RetentionPolicyValidationError(
            f"{field_name} must be an integer"
        ) from exc

    minimum, maximum = bounds
    if not minimum <= normalized_value <= maximum:
        raise RetentionPolicyValidationError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return normalized_value


def _normalize_bool(*, field_name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise RetentionPolicyValidationError(f"{field_name} must be a boolean")
    return value


def _normalize_tenant_id(tenant_id: object) -> int:
    if tenant_id is None or isinstance(tenant_id, bool):
        raise RetentionPolicyValidationError("tenant_id must be a positive integer")
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError) as exc:
        raise RetentionPolicyValidationError(
            "tenant_id must be a positive integer"
        ) from exc
    if normalized_tenant_id < 1:
        raise RetentionPolicyValidationError("tenant_id must be positive")
    return normalized_tenant_id


__all__ = [
    "EffectiveRetentionPolicy",
    "RetentionPolicyValidationError",
    "resolve_effective_retention_policy",
    "resolve_effective_retention_policy_for_tenant",
]
