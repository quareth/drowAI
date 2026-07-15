"""Canonical prompt-facing capability families for visible tools.

This module defines stable capability-family names and compact descriptions.
It intentionally does not inspect the runtime tool registry; callers derive
availability elsewhere and use this taxonomy only to label what is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass(frozen=True)
class CapabilityFamily:
    """Prompt-facing capability family metadata."""

    name: str
    description: str


CAPABILITY_FAMILIES: Sequence[CapabilityFamily] = (
    CapabilityFamily(
        "workspace_file_access",
        "Read, search, create, edit, and manage task-workspace files.",
    ),
    CapabilityFamily(
        "network_discovery",
        "Discover hosts, ports, routes, and network reachability.",
    ),
    CapabilityFamily(
        "service_enumeration",
        "Enumerate exposed services, banners, protocols, and versions.",
    ),
    CapabilityFamily(
        "http_web_testing",
        "Request, crawl, fingerprint, fuzz, and test HTTP/web application surfaces.",
    ),
    CapabilityFamily(
        "dns_reconnaissance",
        "Resolve names and enumerate DNS records or DNS-related target data.",
    ),
    CapabilityFamily(
        "exploitation_framework",
        "Use exploit frameworks, exploit modules, payloads, and handlers.",
    ),
    CapabilityFamily(
        "session_interaction",
        "Interact with opened exploitation sessions and run session commands.",
    ),
    CapabilityFamily(
        "credential_attack",
        "Test, crack, brute force, or relay credentials and hashes.",
    ),
    CapabilityFamily(
        "knowledge_lookup",
        "Look up vulnerability, CVE, product, or evidence enrichment knowledge.",
    ),
    CapabilityFamily(
        "reporting",
        "Generate reports, summaries, or structured documentation artifacts.",
    ),
    CapabilityFamily(
        "shell_execution",
        "Execute generic shell commands or scripts.",
    ),
)

CAPABILITY_FAMILY_DESCRIPTIONS: Dict[str, str] = {
    family.name: family.description for family in CAPABILITY_FAMILIES
}

CAPABILITY_FAMILY_ORDER: Sequence[str] = tuple(
    family.name for family in CAPABILITY_FAMILIES
)


def ordered_capability_families(families: Sequence[str]) -> List[str]:
    """Return capability families in canonical order with unknowns last."""

    unique = {str(family) for family in families if str(family).strip()}
    ordered = [family for family in CAPABILITY_FAMILY_ORDER if family in unique]
    ordered.extend(sorted(unique.difference(ordered)))
    return ordered


__all__ = [
    "CAPABILITY_FAMILIES",
    "CAPABILITY_FAMILY_DESCRIPTIONS",
    "CAPABILITY_FAMILY_ORDER",
    "CapabilityFamily",
    "ordered_capability_families",
]
