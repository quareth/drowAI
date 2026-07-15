"""Runner control-plane configuration helpers.

This module validates managed-runner control-plane endpoint and credential
settings without introducing runtime-mode forks in runner configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse


@dataclass(frozen=True)
class RunnerCloudConfig:
    """Managed-runner control-plane fields parsed from config sources."""

    cloud_base_url: str | None
    registration_token: str | None
    runner_id: str | None
    credential_secret_path: Path | None
    heartbeat_interval_seconds: int
    tls_verify: bool
    allow_insecure_cloud_endpoint: bool
    labels: dict[str, str]
    capabilities: tuple[str, ...]

    def validate(self) -> "RunnerCloudConfig":
        """Validate control-plane fields used by managed runner startup."""
        if self.heartbeat_interval_seconds < 1:
            raise ValueError("heartbeat_interval_seconds must be >= 1.")

        if not self.cloud_base_url:
            return self

        validate_cloud_base_url(
            self.cloud_base_url,
            allow_insecure_cloud_endpoint=self.allow_insecure_cloud_endpoint,
        )

        if self.credential_secret_path is not None:
            validate_credential_secret_path(self.credential_secret_path)
        return self


def validate_cloud_base_url(
    cloud_base_url: str,
    *,
    allow_insecure_cloud_endpoint: bool,
) -> str:
    """Validate cloud control-plane URL and enforce HTTPS by default."""
    candidate = cloud_base_url.strip()
    if not candidate:
        raise ValueError("cloud_base_url must not be empty.")

    parsed = urlparse(candidate)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("cloud_base_url must include https:// (or http:// in dev override).")
    if parsed.scheme == "http" and not allow_insecure_cloud_endpoint:
        raise ValueError(
            "cloud_base_url must use https:// unless DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT is set."
        )
    if not parsed.netloc:
        raise ValueError("cloud_base_url must include a valid host.")
    if parsed.username or parsed.password:
        raise ValueError("cloud_base_url must not embed credentials.")

    normalized_path = parsed.path.rstrip("/")
    if normalized_path == "/":
        normalized_path = ""
    return urlunparse(parsed._replace(path=normalized_path))


def validate_credential_secret_path(path: Path) -> Path:
    """Reject unsafe credential-file path values in managed-runner mode."""
    candidate = path.expanduser()
    if candidate.is_absolute() and candidate == Path(candidate.anchor):
        raise ValueError("credential_secret_path must not be filesystem root.")
    if any(part == ".." for part in candidate.parts):
        raise ValueError("credential_secret_path must not contain parent traversal.")
    if not candidate.name.strip():
        raise ValueError("credential_secret_path must include a file name.")
    return candidate
