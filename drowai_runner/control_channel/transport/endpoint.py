"""Channel endpoint URL/SSL helpers (path join, ws/wss coercion, SSL context).

No network I/O; pure URL/SSL shaping. Raises ``RunnerCloudClientError`` on
missing/invalid endpoints. Imports the error type from ``errors``.
"""

from __future__ import annotations

import ssl
from urllib import parse as urllib_parse

from drowai_runner.control_channel.errors import RunnerCloudClientError


def _build_ssl_context(*, verify: bool) -> ssl.SSLContext | None:
    if verify:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def _join_url_path(base_url: str | None, suffix: str) -> str:
    if not base_url:
        raise RunnerCloudClientError(
            error_code="RUNNER_CLOUD_BASE_URL_MISSING",
            message="cloud mode requires cloud_base_url.",
        )
    parsed = urllib_parse.urlparse(base_url)
    merged = parsed.path.rstrip("/") + suffix
    return urllib_parse.urlunparse(parsed._replace(path=merged))


def _to_websocket_url(url: str) -> str:
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in {"wss", "ws"}:
        scheme = parsed.scheme
    else:
        raise RunnerCloudClientError(
            error_code="RUNNER_CHANNEL_ENDPOINT_INVALID",
            message=f"Unsupported channel endpoint scheme: {parsed.scheme or '<missing>'}.",
        )
    if not parsed.netloc:
        raise RunnerCloudClientError(
            error_code="RUNNER_CHANNEL_ENDPOINT_INVALID",
            message="Channel endpoint must include host.",
        )
    return urllib_parse.urlunparse(parsed._replace(scheme=scheme))
