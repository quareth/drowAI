"""Canonical top-level tool-category taxonomy shared by runtime and prompts.

This module is the single source of truth for selectable tool-category keys,
their descriptions, and selector guidance text.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence


CANONICAL_TOOL_CATEGORY_DESCRIPTIONS: Dict[str, str] = {
    "artifact": "Artifact discovery, retrieval, and evidence file operations",
    "database_assessment": "Database scanning, enumeration, and security testing",
    "exploitation_tools": "Exploit frameworks, payload delivery, and remote code execution",
    "filesystem": "Workspace-safe file inspection, editing, and navigation tools",
    "forensics": "Forensic analysis and evidence correlation tooling",
    "information_gathering": "Network discovery, port scanning, service enumeration, and reconnaissance",
    "knowledge": "Knowledge enrichment and evidence lookups, including CVE matching from known product/service fingerprints",
    "service_access": "Headless authenticated service access checks and single-file transfers for supplied FTP/SSH credentials",
    "maintaining_access": "Persistence, backdoors, and access maintenance tooling",
    "networking_utilities": "Bounded network utility checks such as DNS, WHOIS, reachability, and route inspection",
    "password_attacks": "Password cracking, brute force, and credential testing",
    "reporting_tools": "Report generation and structured documentation tooling",
    "reverse_engineering": "Binary analysis and reverse engineering tooling",
    "shell": "Direct command execution and shell scripting tools",
    "sniffing_spoofing": "Packet capture, sniffing, spoofing, and traffic manipulation tools",
    "stress_testing": "Load, stress, and denial-of-service style testing tools",
    "system_services": "Service interaction and system utility tooling",
    "vulnerability_analysis": "Automated vulnerability scanning and assessment tools",
    "web_applications": "Web application testing, crawling, fuzzing, and web vulnerability analysis",
}


CANONICAL_TOOL_CATEGORIES: Sequence[str] = tuple(CANONICAL_TOOL_CATEGORY_DESCRIPTIONS.keys())

TOOL_CATEGORY_SELECTION_GUIDELINES: Sequence[str] = (
    "Select 1-3 categories that are most relevant",
    'For network scanning/host discovery: select "information_gathering"',
    'For web testing: select "web_applications" and possibly "information_gathering"',
    'For database testing (PostgreSQL, MySQL, etc.): select "database_assessment"',
    'For exploitation: select "exploitation_tools" and possibly "information_gathering"',
    'For vulnerability scanning: select "vulnerability_analysis"',
    "For multi-phase requests, select only categories required by the current action",
)

TOOL_CATEGORY_SELECTION_GUIDANCE_TEXT = "\n".join(
    f"- {line}" for line in TOOL_CATEGORY_SELECTION_GUIDELINES
)


def get_category_descriptions() -> Dict[str, str]:
    """Return a copy of canonical category descriptions."""

    return dict(CANONICAL_TOOL_CATEGORY_DESCRIPTIONS)


def get_canonical_categories() -> List[str]:
    """Return canonical category keys in deterministic order."""

    return list(CANONICAL_TOOL_CATEGORIES)


def find_missing_descriptions(
    categories: Iterable[str],
    *,
    descriptions: Mapping[str, str] | None = None,
) -> List[str]:
    """Return category keys that do not have a description mapping."""

    catalog = descriptions or CANONICAL_TOOL_CATEGORY_DESCRIPTIONS
    missing: List[str] = []
    for category in categories:
        if category not in catalog:
            missing.append(category)
    return missing


__all__ = [
    "CANONICAL_TOOL_CATEGORIES",
    "CANONICAL_TOOL_CATEGORY_DESCRIPTIONS",
    "TOOL_CATEGORY_SELECTION_GUIDANCE_TEXT",
    "TOOL_CATEGORY_SELECTION_GUIDELINES",
    "find_missing_descriptions",
    "get_canonical_categories",
    "get_category_descriptions",
]
