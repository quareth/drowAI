"""Parse and extract tools, targets, and ports from user messages and execution parameters for intent contract validation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .....state import InteractiveState

_TARGET_TOKEN_PATTERN = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,}\b"
)
_RELATIONAL_TARGET_PATTERN = re.compile(
    r"\b(?:against|on|to|at)\s+([a-zA-Z0-9_.:-]+)",
    re.IGNORECASE,
)
_PORT_FLAG_PATTERN = re.compile(r"(?:^|\s)-p\s*([0-9,\-\s]+)", re.IGNORECASE)
_PORT_WORD_PATTERN = re.compile(
    r"\bports?\s+(?:for\s+)?([0-9,\-\sand]+)",
    re.IGNORECASE,
)

_TOOL_ALIAS_NORMALIZATION: Dict[str, str] = {
    "nmap": "nmap",
    "masscan": "masscan",
    "nikto": "nikto",
    "sqlmap": "sqlmap",
    "gobuster": "gobuster",
    "dirb": "dirb",
    "dirbuster": "dirbuster",
    "metasploit": "metasploit",
    "msf": "metasploit",
    "burp": "burp",
    "burpsuite": "burp",
}


def _dedupe_preserve(values: List[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for value in values:
        token = value.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _normalize_tool_alias(value: Optional[str]) -> str:
    if not value:
        return ""
    token = str(value).strip().lower()
    if "." in token:
        token = token.split(".")[-1]
    return _TOOL_ALIAS_NORMALIZATION.get(token, token)


def _normalize_target_token(value: str) -> str:
    token = value.strip().lower()
    if "://" in token:
        token = token.split("://", 1)[1]
    token = token.split("/", 1)[0].split("?", 1)[0]
    token = token.strip("[]")
    if token.count(":") == 1:
        host, port = token.rsplit(":", 1)
        if port.isdigit():
            token = host
    return token


def _extract_target_port(value: str) -> Optional[int]:
    token = value.strip().lower()
    if "://" in token:
        token = token.split("://", 1)[1]
    token = token.split("/", 1)[0].split("?", 1)[0]
    if token.count(":") != 1:
        return None
    _, port_text = token.rsplit(":", 1)
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if 1 <= port <= 65535:
        return port
    return None


def _parse_port_tokens(raw: str) -> List[str]:
    normalized = re.sub(r"\s*-\s*", "-", str(raw))
    return _dedupe_preserve(re.findall(r"\d{1,5}(?:-\d{1,5})?", normalized))


def _parse_port_range(token: str) -> Optional[Tuple[int, int]]:
    token = token.strip()
    if not token:
        return None
    if "-" in token:
        left, right = token.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return None
        start = int(left)
        end = int(right)
    else:
        if not token.isdigit():
            return None
        start = end = int(token)
    if start < 1 or end > 65535:
        return None
    if start > end:
        start, end = end, start
    return (start, end)


def _extract_expected_tools(user_message: str) -> List[str]:
    tools: List[str] = []
    try:
        from .....utils.scope_parser import parse_user_scope

        parsed_scope = parse_user_scope(user_message)
        for tool in parsed_scope.explicit_tools:
            normalized = _normalize_tool_alias(tool)
            if normalized:
                tools.append(normalized)
    except Exception:
        pass

    if tools:
        return _dedupe_preserve(tools)

    lowered = user_message.lower()
    for alias in _TOOL_ALIAS_NORMALIZATION:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            tools.append(_normalize_tool_alias(alias))

    return _dedupe_preserve(tools)


def _extract_expected_targets(interactive: InteractiveState) -> List[str]:
    expected: List[str] = []

    intent_hints = interactive.facts.intent_hints or {}
    hinted_targets = intent_hints.get("targets") or []
    if isinstance(hinted_targets, str):
        hinted_targets = [hinted_targets]
    if isinstance(hinted_targets, list):
        expected.extend(str(value) for value in hinted_targets if value)

    message = interactive.facts.message or ""
    expected.extend(_TARGET_TOKEN_PATTERN.findall(message))
    for candidate in _RELATIONAL_TARGET_PATTERN.findall(message):
        token = candidate.strip().lower()
        if token in {"the", "a", "an", "tool", "port", "ports"}:
            continue
        if token == "localhost" or "." in token or ":" in token:
            expected.append(token)
    return _dedupe_preserve([_normalize_target_token(value) for value in expected if value])


def _extract_expected_ports(user_message: str) -> List[str]:
    expected: List[str] = []
    for match in _PORT_FLAG_PATTERN.findall(user_message):
        expected.extend(_parse_port_tokens(match))
    for match in _PORT_WORD_PATTERN.findall(user_message):
        expected.extend(_parse_port_tokens(match))
    return _dedupe_preserve(expected)


def _extract_executed_targets(parameters: Mapping[str, Any]) -> List[str]:
    raw_target = parameters.get("target")
    if raw_target in (None, "", [], {}):
        return []

    parts: List[str] = []
    if isinstance(raw_target, (list, tuple, set)):
        candidates = [str(value) for value in raw_target if value]
    else:
        candidates = [str(raw_target)]

    for candidate in candidates:
        for token in re.split(r"[\s,]+", candidate):
            token = token.strip()
            if token:
                parts.append(token)
    return _dedupe_preserve(parts)


def _extract_executed_ports(
    parameters: Mapping[str, Any],
    executed_targets: List[str],
) -> List[str]:
    ports: List[str] = []
    for key in ("ports", "port"):
        value = parameters.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                ports.extend(_parse_port_tokens(str(item)))
        else:
            ports.extend(_parse_port_tokens(str(value)))

    for target in executed_targets:
        target_port = _extract_target_port(target)
        if target_port is not None:
            ports.append(str(target_port))

    return _dedupe_preserve(ports)

