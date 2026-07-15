"""Canonical Management URL resolution for Runner enrollment.

This module owns validation and persistence of the URL Runners use to reach
Management. It keeps deployment-specific callers from inventing URL rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from fastapi import Request

from backend.config.generated_config import (
    MANAGEMENT_URL_ENV,
    read_backend_env,
    update_generated_management_url,
)


class ManagementUrlError(ValueError):
    """Raised when a Management URL is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ManagementUrlResolution:
    """Resolved Management URL plus its source for UI display."""

    management_url: str
    source: str


class ManagementUrlService:
    """Resolve, validate, and persist the Runner-facing Management URL."""

    def resolve(self, *, request: Request | None = None) -> ManagementUrlResolution:
        """Resolve the canonical URL from generated config or request origin."""
        stored_url = self._stored_url()
        if stored_url:
            return ManagementUrlResolution(
                management_url=normalize_management_url(stored_url),
                source="generated_config",
            )
        if request is not None:
            return ManagementUrlResolution(
                management_url=normalize_management_url(_origin_from_request(request)),
                source="request_origin",
            )
        dev_override = str(os.getenv(MANAGEMENT_URL_ENV) or "").strip()
        if dev_override:
            return ManagementUrlResolution(
                management_url=normalize_management_url(dev_override),
                source="dev_override",
            )
        raise ManagementUrlError("Management URL has not been configured.")

    def set_url(self, management_url: str) -> ManagementUrlResolution:
        """Validate and persist the canonical URL."""
        normalized = normalize_management_url(management_url)
        update_generated_management_url(normalized)
        return ManagementUrlResolution(management_url=normalized, source="generated_config")

    def _stored_url(self) -> str | None:
        file_env = read_backend_env()
        file_value = str(file_env.get(MANAGEMENT_URL_ENV) or "").strip()
        if file_value:
            return file_value
        return None


def normalize_management_url(value: str) -> str:
    """Return a normalized origin-only Management URL."""
    candidate = str(value or "").strip()
    if not candidate:
        raise ManagementUrlError("Management URL must not be empty.")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ManagementUrlError("Management URL must start with http:// or https://.")
    if not parsed.netloc:
        raise ManagementUrlError("Management URL must include a host.")
    if parsed.username or parsed.password:
        raise ManagementUrlError("Management URL must not include credentials.")
    path = parsed.path.rstrip("/")
    if path:
        raise ManagementUrlError("Management URL must be an origin only, without a path.")
    if parsed.query or parsed.fragment:
        raise ManagementUrlError("Management URL must not include query string or fragment.")
    return urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")


def _origin_from_request(request: Request) -> str:
    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    scheme = forwarded_proto or request.url.scheme
    host = _first_header_value(request.headers.get("x-forwarded-host")) or request.headers.get("host")
    if not host:
        host = request.url.netloc
    return f"{scheme}://{host}"


def _first_header_value(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None
