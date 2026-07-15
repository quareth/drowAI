"""Local filesystem implementation of the data-plane object-store port.

This module is for dev/standalone compatibility. It persists bytes under a
configured root using sanitized object keys and never returns host filesystem
paths in signed URL payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from typing import Callable, Mapping
from urllib.parse import quote

from .object_store import ObjectHead, SignedDownloadTarget, SignedUploadTarget


class LocalObjectStore:
    """Object-store implementation backed by a local directory root."""

    def __init__(
        self,
        *,
        root_path: Path,
        signed_url_ttl_seconds: int = 900,
        signed_upload_ttl_seconds: int | None = None,
        signed_download_ttl_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root_path = root_path.resolve()
        self._root_path.mkdir(parents=True, exist_ok=True)
        base_ttl_seconds = max(1, int(signed_url_ttl_seconds))
        self._signed_upload_ttl_seconds = max(
            1,
            int(signed_upload_ttl_seconds)
            if signed_upload_ttl_seconds is not None
            else base_ttl_seconds,
        )
        self._signed_download_ttl_seconds = max(
            1,
            int(signed_download_ttl_seconds)
            if signed_download_ttl_seconds is not None
            else base_ttl_seconds,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def put_bytes(
        self,
        object_key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        object_path, normalized_key = self._resolve_object_path(object_key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(data)
        return self._build_head(
            object_path=object_path,
            object_key=normalized_key,
            content_type=content_type,
            metadata=metadata,
            content_sha256=sha256(data).hexdigest(),
        )

    def read_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        object_path, _ = self._resolve_object_path(object_key)
        with object_path.open("rb") as handle:
            if max_bytes is None:
                return handle.read()
            if max_bytes < 0:
                raise ValueError("max_bytes must be non-negative when provided")
            return handle.read(max_bytes)

    def delete_object(self, object_key: str) -> bool:
        object_path, _ = self._resolve_object_path(object_key)
        if not object_path.exists():
            return False

        object_path.unlink()
        current = object_path.parent
        while current != self._root_path:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
        return True

    def create_signed_upload(
        self,
        object_key: str,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> SignedUploadTarget:
        normalized_key = self._normalize_object_key(object_key)
        expires_at = self._clock() + timedelta(seconds=self._signed_upload_ttl_seconds)
        encoded_key = quote(normalized_key, safe="/")
        token = token_urlsafe(24)

        headers: dict[str, str] = {}
        if content_type:
            headers["content-type"] = str(content_type)
        if metadata:
            for key, value in metadata.items():
                headers[f"x-meta-{key}"] = str(value)

        return SignedUploadTarget(
            object_key=normalized_key,
            method="PUT",
            url=f"local-object://upload/{encoded_key}?token={token}",
            expires_at=expires_at,
            headers=headers,
        )

    def create_signed_download(
        self,
        object_key: str,
        *,
        response_filename: str | None = None,
    ) -> SignedDownloadTarget:
        normalized_key = self._normalize_object_key(object_key)
        expires_at = self._clock() + timedelta(seconds=self._signed_download_ttl_seconds)
        encoded_key = quote(normalized_key, safe="/")
        token = token_urlsafe(24)
        headers: dict[str, str] = {}
        if response_filename:
            headers["content-disposition"] = f'attachment; filename="{response_filename}"'

        return SignedDownloadTarget(
            object_key=normalized_key,
            method="GET",
            url=f"local-object://download/{encoded_key}?token={token}",
            expires_at=expires_at,
            headers=headers,
        )

    def head_object(self, object_key: str) -> ObjectHead | None:
        object_path, normalized_key = self._resolve_object_path(object_key)
        if not object_path.exists():
            return None
        return self._build_head(object_path=object_path, object_key=normalized_key)

    def _build_head(
        self,
        *,
        object_path: Path,
        object_key: str,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        content_sha256: str | None = None,
    ) -> ObjectHead:
        stat_result = object_path.stat()
        return ObjectHead(
            object_key=object_key,
            byte_size=stat_result.st_size,
            content_type=content_type,
            content_sha256=content_sha256,
            last_modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
            metadata=dict(metadata or {}),
        )

    def _resolve_object_path(self, object_key: str) -> tuple[Path, str]:
        normalized_key = self._normalize_object_key(object_key)
        candidate = (self._root_path / normalized_key).resolve()
        if candidate != self._root_path and self._root_path not in candidate.parents:
            raise ValueError("Object key escapes configured object-store root")
        return candidate, normalized_key

    @staticmethod
    def _normalize_object_key(object_key: str) -> str:
        if not isinstance(object_key, str):
            raise ValueError("Object key must be a string")
        raw = object_key.replace("\\", "/").strip()
        if not raw:
            raise ValueError("Object key must not be empty")
        if raw.startswith("/"):
            raise ValueError("Object key must be relative")

        parts: list[str] = []
        for part in raw.split("/"):
            cleaned = part.strip()
            if cleaned in {"", "."}:
                continue
            if cleaned == "..":
                raise ValueError("Object key traversal is not allowed")
            if any(ord(char) < 32 for char in cleaned):
                raise ValueError("Object key contains control characters")
            parts.append(cleaned)

        if not parts:
            raise ValueError("Object key must include at least one non-empty part")
        return "/".join(parts)
