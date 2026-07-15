"""Object-store backend registry and construction helpers.

This module centralizes object-store backend selection so callers depend on one
application boundary and do not import backend-specific storage SDKs.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from backend.config.data_plane import get_data_plane_config

from .local_object_store import LocalObjectStore
from .object_store import ObjectStore

logger = logging.getLogger(__name__)

def build_object_store(*, backend: str | None = None, root_path: Path | None = None) -> ObjectStore:
    """Build and return an object-store implementation for the configured backend."""

    data_plane_config = get_data_plane_config()
    selected_backend = str(backend or data_plane_config.object_store_backend).strip().lower()
    logger.info(
        "data_plane object-store config loaded: %s",
        data_plane_config.to_log_fields(),
    )

    if selected_backend == "local":
        return LocalObjectStore(
            root_path=(root_path or data_plane_config.local_object_store_root),
            signed_upload_ttl_seconds=data_plane_config.signed_upload_ttl_seconds,
            signed_download_ttl_seconds=data_plane_config.signed_download_ttl_seconds,
        )
    raise ValueError(
        "Unsupported DATA_PLANE object-store backend "
        f"`{selected_backend}`. Currently supported backends: `local`."
    )


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return a process-level cached object-store instance."""

    return build_object_store()


def reset_object_store_cache() -> None:
    """Clear the process-level object-store cache for tests or runtime reconfigure."""

    get_object_store.cache_clear()
