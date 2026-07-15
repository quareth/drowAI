"""Shared policy helpers for deprecated websocket alias endpoints.

Responsibilities:
- Provide canonical deprecation handshake headers for alias websocket routes.
- Emit consistent deprecation telemetry records for alias usage.
- Apply shared origin validation for alias endpoints that require origin checks.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import WebSocket

logger = logging.getLogger("backend.services.ws_alias_policy")

ALIAS_WS_DEPRECATION_HEADERS: list[tuple[bytes, bytes]] = [
    (b"deprecation", b"true"),
    (b"x-drowai-ws-deprecated", b"true"),
]


def log_alias_ws_deprecation(
    *,
    endpoint: str,
    canonical: str,
    task_id: int,
    user_id: int,
    websocket: WebSocket,
) -> None:
    """Emit a structured deprecation warning for websocket alias usage."""
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.warning(
        "deprecated websocket alias used endpoint=%s canonical=%s task_id=%s user_id=%s client_ip=%s",
        endpoint,
        canonical,
        task_id,
        user_id,
        client_ip,
    )


def _get_base_domain(hostname: str) -> str:
    if not hostname:
        return ""
    parts = hostname.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname


async def validate_alias_origin(websocket: WebSocket) -> bool:
    """Validate Origin/Host compatibility for alias websocket endpoints."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        logger.warning("websocket alias rejected: missing origin/host header")
        return False
    try:
        parsed = urlparse(origin)
        origin_host = parsed.hostname
        host_clean = host.split(":")[0] if ":" in host else host

        if origin_host in {"localhost", "127.0.0.1"} and host_clean in {"localhost", "127.0.0.1"}:
            return True

        origin_domain = _get_base_domain(origin_host or "")
        host_domain = _get_base_domain(host_clean)
        if origin_domain == host_domain:
            return True

        logger.warning(
            "websocket alias rejected: origin_domain_mismatch origin=%s host=%s",
            origin_domain,
            host_domain,
        )
        return False
    except Exception:
        logger.error("websocket alias origin validation error", exc_info=True)
        return False
