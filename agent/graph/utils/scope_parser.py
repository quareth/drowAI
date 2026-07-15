"""Scope parser for extracting goals, boundaries, and constraints from user requests."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class UserScope:
    """Parsed user scope with goals and boundaries."""

    goals: List[str]
    boundaries: List[str]
    conditional_targets: Dict[str, str]
    explicit_tools: List[str]

    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for storage in metadata."""
        return {
            "goals": self.goals,
            "boundaries": self.boundaries,
            "conditional_targets": self.conditional_targets,
            "explicit_tools": self.explicit_tools,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, any]) -> "UserScope":
        """Create from dictionary stored in metadata."""
        return cls(
            goals=data.get("goals", []),
            boundaries=data.get("boundaries", []),
            conditional_targets=data.get("conditional_targets", {}),
            explicit_tools=data.get("explicit_tools", []),
        )


def parse_user_scope(user_request: str) -> UserScope:
    """
    Parse user request to extract scope, goals, and boundaries.

    Args:
        user_request: Original user message

    Returns:
        UserScope with extracted information
    """
    goals = []
    boundaries = []
    conditional_targets = {}
    explicit_tools = []

    request_lower = user_request.lower()

    # Extract goals
    goals = _extract_goals(request_lower)

    # Extract boundaries (negative constraints)
    boundaries = _extract_boundaries(request_lower)

    # Extract conditional targets
    conditional_targets = _extract_conditional_targets(request_lower, user_request)

    # Extract explicit tools
    explicit_tools = _extract_explicit_tools(request_lower)

    scope = UserScope(
        goals=goals,
        boundaries=boundaries,
        conditional_targets=conditional_targets,
        explicit_tools=explicit_tools,
    )

    logger.info(f"[SCOPE] Parsed user scope: {len(goals)} goals, {len(boundaries)} boundaries")
    return scope


def _extract_goals(request_lower: str) -> List[str]:
    """Extract goal patterns from user request."""
    goals = []

    # Find vulnerable services
    if any(kw in request_lower for kw in ["find", "identify", "discover"]) and any(
        kw in request_lower for kw in ["vulnerable", "vulnerability", "vuln", "security issue"]
    ):
        goals.append("find_vulnerable_services")

    # Identify hosts
    if any(kw in request_lower for kw in ["scan", "find", "identify", "discover"]) and any(
        kw in request_lower for kw in ["host", "hosts", "machine", "machines", "device", "devices"]
    ):
        goals.append("identify_hosts")

    # Scan network
    if any(kw in request_lower for kw in ["scan", "discover"]) and any(
        kw in request_lower for kw in ["network", "subnet", "cidr"]
    ):
        goals.append("identify_hosts")

    # Identify open ports
    if any(kw in request_lower for kw in ["scan", "find", "identify"]) and any(
        kw in request_lower for kw in ["port", "ports", "open port"]
    ):
        goals.append("identify_open_ports")

    # Identify services
    if any(kw in request_lower for kw in ["identify", "find", "detect", "enumerate"]) and any(
        kw in request_lower for kw in ["service", "services", "service version", "banner"]
    ):
        goals.append("identify_services")

    # Remove duplicates while preserving order
    seen = set()
    unique_goals = []
    for goal in goals:
        if goal not in seen:
            seen.add(goal)
            unique_goals.append(goal)

    return unique_goals


def _extract_boundaries(request_lower: str) -> List[str]:
    """Extract negative constraints from user request."""
    boundaries = []

    # Check for negative phrases
    negative_phrases = ["do not", "don't", "without", "no", "avoid", "skip", "exclude"]

    # Exploitation boundary
    if any(neg in request_lower for neg in negative_phrases) and any(
        kw in request_lower for kw in ["exploit", "exploitation", "attack", "penetrate", "gain access"]
    ):
        boundaries.append("no_exploitation")

    # Brute force boundary
    if any(neg in request_lower for neg in negative_phrases) and any(
        kw in request_lower for kw in ["brute", "brute force", "crack", "password"]
    ):
        boundaries.append("no_brute_force")

    # DoS boundary
    if any(neg in request_lower for neg in negative_phrases) and any(
        kw in request_lower for kw in ["dos", "denial of service", "flood", "overload"]
    ):
        boundaries.append("no_dos")

    # Data modification boundary
    if any(neg in request_lower for neg in negative_phrases) and any(
        kw in request_lower for kw in ["modify", "change", "delete", "modify", "write"]
    ):
        boundaries.append("no_data_modification")

    return boundaries


def _extract_conditional_targets(request_lower: str, original_request: str) -> Dict[str, str]:
    """Extract fallback targets from conditional statements."""
    conditional_targets = {}

    # Pattern: "if [condition] use [target]"
    if "if" in request_lower and "use" in request_lower:
        # Try to extract IP address or hostname after "use"
        use_match = re.search(r"use\s+([0-9.]+|[\w.-]+)", request_lower, re.IGNORECASE)
        if use_match:
            target = use_match.group(1).strip()

            # Validate it looks like an IP or hostname
            if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", target) or re.match(
                r"^[\w.-]+\.[\w.-]+", target
            ):
                conditional_targets["fallback_host"] = target
                logger.info(f"[SCOPE] Found conditional target: {target}")

    # Pattern: "if [condition] [target]" (without "use")
    if "if" in request_lower and not conditional_targets:
        # Look for IP addresses or hostnames after "if" clause
        ip_pattern = r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        ip_matches = re.findall(ip_pattern, original_request)
        if ip_matches:
            # Use the last IP found (likely the fallback)
            conditional_targets["fallback_host"] = ip_matches[-1]

    return conditional_targets


def _extract_explicit_tools(request_lower: str) -> List[str]:
    """Extract explicitly mentioned tools from user request."""
    explicit_tools = []

    # Common tool keywords
    tool_keywords = {
        "nmap": "nmap",
        "masscan": "masscan",
        "metasploit": "metasploit",
        "msf": "metasploit",
        "nikto": "nikto",
        "sqlmap": "sqlmap",
        "burp": "burp",
        "burpsuite": "burp",
        "wireshark": "wireshark",
        "tcpdump": "tcpdump",
    }

    for keyword, tool_name in tool_keywords.items():
        if keyword in request_lower:
            if tool_name not in explicit_tools:
                explicit_tools.append(tool_name)

    return explicit_tools


__all__ = ["UserScope", "parse_user_scope"]
