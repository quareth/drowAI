"""Data-plane feature and storage configuration.

This module owns environment parsing and fail-closed validation for artifact
upload/data-plane controls so storage services do not parse raw env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backend.config.workspace_config import WorkspaceConfig

_DEFAULT_SIGNED_TTL_SECONDS = 900
_DEFAULT_MAX_ARTIFACT_SIZE_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_MANIFEST_ITEMS = 256
_DEFAULT_MAX_ZIP_DOWNLOAD_SIZE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class DataPlaneConfig:
    """Typed data-plane configuration values."""

    object_store_backend: str
    local_object_store_root: Path
    object_store_bucket: str | None
    object_store_prefix: str
    signed_upload_ttl_seconds: int
    signed_download_ttl_seconds: int
    max_artifact_size_bytes: int
    max_manifest_items: int
    max_zip_download_size_bytes: int

    def to_log_fields(self) -> dict[str, object]:
        """Return sanitized config fields safe for logs."""

        return {
            "object_store_backend": self.object_store_backend,
            "local_object_store_root": "<configured>",
            "object_store_bucket": "<SET>" if self.object_store_bucket else "<UNSET>",
            "object_store_prefix": self.object_store_prefix,
            "signed_upload_ttl_seconds": self.signed_upload_ttl_seconds,
            "signed_download_ttl_seconds": self.signed_download_ttl_seconds,
            "max_artifact_size_bytes": self.max_artifact_size_bytes,
            "max_manifest_items": self.max_manifest_items,
            "max_zip_download_size_bytes": self.max_zip_download_size_bytes,
        }


def get_data_plane_config() -> DataPlaneConfig:
    """Build and validate the current data-plane configuration."""

    object_store_backend = str(os.getenv("DATA_PLANE_OBJECT_STORE_BACKEND", "local") or "local").strip().lower()
    legacy_ttl = _read_positive_int_env(
        "DATA_PLANE_SIGNED_URL_TTL_SECONDS",
        default=_DEFAULT_SIGNED_TTL_SECONDS,
    )
    config = DataPlaneConfig(
        object_store_backend=object_store_backend,
        local_object_store_root=_default_local_object_store_root(),
        object_store_bucket=_read_optional_text_env("DATA_PLANE_OBJECT_STORE_BUCKET"),
        object_store_prefix=_normalize_key_prefix(
            str(os.getenv("DATA_PLANE_OBJECT_STORE_PREFIX", "") or "")
        ),
        signed_upload_ttl_seconds=_read_positive_int_env(
            "DATA_PLANE_SIGNED_UPLOAD_TTL_SECONDS",
            default=legacy_ttl,
        ),
        signed_download_ttl_seconds=_read_positive_int_env(
            "DATA_PLANE_SIGNED_DOWNLOAD_TTL_SECONDS",
            default=legacy_ttl,
        ),
        max_artifact_size_bytes=_read_positive_int_env(
            "DATA_PLANE_MAX_ARTIFACT_SIZE_BYTES",
            default=_DEFAULT_MAX_ARTIFACT_SIZE_BYTES,
        ),
        max_manifest_items=_read_positive_int_env(
            "DATA_PLANE_MAX_MANIFEST_ITEMS",
            default=_DEFAULT_MAX_MANIFEST_ITEMS,
        ),
        max_zip_download_size_bytes=_read_positive_int_env(
            "DATA_PLANE_MAX_ZIP_DOWNLOAD_SIZE_BYTES",
            default=_DEFAULT_MAX_ZIP_DOWNLOAD_SIZE_BYTES,
        ),
    )
    return _validate_data_plane_config(config)


def is_non_local_object_store_backend(config: DataPlaneConfig) -> bool:
    """Return True when object-store backend is remote and needs bucket validation."""

    return config.object_store_backend != "local"


def has_object_store_bucket(config: DataPlaneConfig) -> bool:
    """Return True when object-store bucket configuration is present."""

    return bool(config.object_store_bucket)


def _validate_data_plane_config(config: DataPlaneConfig) -> DataPlaneConfig:
    """Validate invariants and fail fast for unsafe/incomplete upload config."""

    if config.object_store_backend != "local" and not config.object_store_bucket:
        raise ValueError(
            "DATA_PLANE_OBJECT_STORE_BUCKET must be configured when "
            "DATA_PLANE_OBJECT_STORE_BACKEND is not local."
        )

    return config


def _default_local_object_store_root() -> Path:
    raw = _read_optional_text_env("DATA_PLANE_LOCAL_OBJECT_STORE_ROOT")
    if raw:
        return Path(raw).expanduser()
    return WorkspaceConfig.get_project_root() / "agent" / "object_store"


def _read_positive_int_env(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _read_optional_text_env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def _normalize_key_prefix(prefix: str) -> str:
    """Normalize object-key prefixes to a safe slash-delimited relative value."""

    raw = prefix.replace("\\", "/").strip("/")
    if not raw:
        return ""

    parts: list[str] = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if cleaned in {"", "."}:
            continue
        if cleaned == "..":
            raise ValueError("DATA_PLANE_OBJECT_STORE_PREFIX must not include path traversal segments.")
        if any(ord(char) < 32 for char in cleaned):
            raise ValueError("DATA_PLANE_OBJECT_STORE_PREFIX must not include control characters.")
        parts.append(cleaned)

    return "/".join(parts)
