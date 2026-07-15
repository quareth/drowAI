"""Runtime curl HTTP capability detection and version flag mapping.

This module centralizes protocol capability probing and requested HTTP version
to curl-argument translation so request/download tools can keep orchestration
logic focused and deterministic.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Literal, Optional, Tuple

HttpVersion = Literal["auto", "1.1", "2", "3"]

_CAPABILITIES_CACHE: Optional[Dict[str, Any]] = None


class UnsupportedHttpVersionError(ValueError):
    """Raised when requested HTTP version is unsupported by runtime curl."""

    def __init__(self, *, requested: HttpVersion, capabilities: Dict[str, Any]) -> None:
        self.requested = requested
        self.capabilities = dict(capabilities)
        super().__init__(
            f"requested http_version={requested} is not supported by runtime curl capabilities"
        )


def reset_curl_http_capabilities_cache() -> None:
    """Clear cached curl capability profile (used by tests)."""
    global _CAPABILITIES_CACHE
    _CAPABILITIES_CACHE = None


def _parse_curl_version_capabilities(version_output: str) -> Dict[str, Any]:
    """Parse curl --version output into HTTP capability flags."""
    features_tokens: List[str] = []
    protocol_tokens: List[str] = []
    for line in (version_output or "").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("features:"):
            features_tokens.extend(token.strip().upper() for token in stripped.split(":", 1)[1].split())
        elif lower.startswith("protocols:"):
            protocol_tokens.extend(token.strip().lower() for token in stripped.split(":", 1)[1].split())

    http2_supported = ("HTTP2" in features_tokens) or ("h2" in protocol_tokens) or ("http2" in protocol_tokens)
    http3_supported = ("HTTP3" in features_tokens) or ("h3" in protocol_tokens) or ("http3" in protocol_tokens)
    return {
        "http2": bool(http2_supported),
        "http3": bool(http3_supported),
    }


def detect_curl_http_capabilities(force_refresh: bool = False) -> Dict[str, Any]:
    """Detect runtime curl HTTP protocol capabilities with lightweight caching."""
    global _CAPABILITIES_CACHE
    if _CAPABILITIES_CACHE is not None and not force_refresh:
        return dict(_CAPABILITIES_CACHE)

    output_text = ""
    try:
        proc = subprocess.run(
            ["curl", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output_text = proc.stdout or ""
    except Exception:
        output_text = ""

    parsed = _parse_curl_version_capabilities(output_text)
    parsed["source"] = "curl --version"
    _CAPABILITIES_CACHE = dict(parsed)
    return dict(parsed)


def build_http_version_curl_args(
    *,
    http_version: HttpVersion = "auto",
    capabilities: Optional[Dict[str, Any]] = None,
    enforce_capability_checks: bool = True,
) -> Tuple[List[str], str]:
    """Build curl HTTP version flags and applied version label."""
    runtime_caps = dict(capabilities or detect_curl_http_capabilities())
    if http_version == "auto":
        return [], "auto"
    if http_version == "1.1":
        return ["--http1.1"], "1.1"
    if http_version == "2":
        if enforce_capability_checks and not runtime_caps.get("http2", False):
            raise UnsupportedHttpVersionError(requested="2", capabilities=runtime_caps)
        return ["--http2"], "2"
    if http_version == "3":
        if enforce_capability_checks and not runtime_caps.get("http3", False):
            raise UnsupportedHttpVersionError(requested="3", capabilities=runtime_caps)
        return ["--http3"], "3"
    raise ValueError(f"unsupported http_version value: {http_version}")
