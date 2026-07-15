"""Data-plane object store port and typed operation contracts.

This module defines a small storage abstraction used by artifact and evidence
services. Implementations must operate on object keys and must not expose host
filesystem paths to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol


@dataclass(frozen=True)
class ObjectHead:
    """Metadata returned by object-store write/head operations."""

    object_key: str
    byte_size: int
    content_type: str | None = None
    content_sha256: str | None = None
    last_modified: datetime | None = None
    etag: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SignedUploadTarget:
    """Just-in-time signed upload instruction for one object key."""

    object_key: str
    method: str
    url: str
    expires_at: datetime
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SignedDownloadTarget:
    """Just-in-time signed download instruction for one object key."""

    object_key: str
    method: str
    url: str
    expires_at: datetime
    headers: Mapping[str, str] = field(default_factory=dict)


class ObjectStore(Protocol):
    """Port for tenant/task-scoped object storage interactions."""

    def put_bytes(
        self,
        object_key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        """Write bytes for one object key and return stored metadata."""

    def read_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        """Read object bytes, optionally bounded to at most `max_bytes` bytes."""

    def delete_object(self, object_key: str) -> bool:
        """Delete one object key. Return True when an object was deleted."""

    def create_signed_upload(
        self,
        object_key: str,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> SignedUploadTarget:
        """Generate a just-in-time signed upload target for one object key."""

    def create_signed_download(
        self,
        object_key: str,
        *,
        response_filename: str | None = None,
    ) -> SignedDownloadTarget:
        """Generate a just-in-time signed download target for one object key."""

    def head_object(self, object_key: str) -> ObjectHead | None:
        """Return metadata for one object key, or None when not found."""
