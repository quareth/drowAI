"""Translate internal tool identifiers into customer-safe reporting labels."""

from __future__ import annotations

from typing import Any

_CATEGORY_LABELS = {
    "network_discovery": "Network discovery",
    "service_discovery": "Service discovery",
    "vulnerability_scanning": "Vulnerability scanning",
    "web_discovery": "Web discovery",
    "exploitation": "Exploitation attempt",
    "post_exploitation": "Post-exploitation activity",
    "credential_access": "Credential testing",
}
_TOOL_LABELS = {
    "fping": "fping",
    "ping": "ping",
    "nmap": "Nmap",
    "httpx": "HTTPX",
    "http_probe": "HTTP probe",
    "ffuf": "ffuf",
    "nikto": "Nikto",
    "sqlmap": "sqlmap",
    "metasploit": "Metasploit",
    "msfconsole": "Metasploit",
}


def report_tool_display_name(value: Any) -> str:
    """Return a broad customer-facing label for a tool identifier."""

    text = str(value or "").strip()
    if not text:
        return "Tool execution"

    lowered = text.lower()
    parts = [part for part in lowered.replace(":", ".").split(".") if part]
    for part in reversed(parts):
        label = _TOOL_LABELS.get(part)
        if label:
            return label
    for part in parts:
        label = _CATEGORY_LABELS.get(part)
        if label:
            return label
    return _humanize(parts[-1] if parts else lowered)


def _humanize(value: str) -> str:
    words = [word for word in value.replace("-", "_").split("_") if word]
    if not words:
        return "Tool execution"
    return " ".join(words).capitalize()


__all__ = ["report_tool_display_name"]
