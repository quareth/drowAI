"""Shared helpers for assembling tool catalogs from intent metadata."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

resolve_tools_for_capability = None  # type: ignore[assignment]
filter_visible_tool_ids = None  # type: ignore[assignment]
get_tool_metadata = None  # type: ignore[assignment]
CapabilityType = None  # type: ignore[assignment, misc]
available_tools_fn = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


HINT_TO_CAPABILITY = {
    "network_scan": "scan_ports",
    "web_enum": "scan_web",
    "web_proxy": "scan_web",
    "injection": "test_exploit",
    "cms_audit": "scan_web",
    "password_attack": "test_exploit",
    "http_probe": "http_request",
    "http_fetch": "http",
    "file_download": "http_download",
    "download": "download",
    "curl": "curl",
    "http_trace": "http_debug",
    "http_debug": "http_debug",
    "http_headers": "http_request",
    "http_session": "http_session",
    "cookies": "http_session",
    "multipart_upload": "http_upload",
    "file_upload": "http_upload",
    "mtls": "http_tls",
    "tls_client_cert": "http_tls",
    "http_retry": "http_resilient",
    "rate_limit": "http_resilient",
    "http_binary": "http_binary",
    "http_protocol": "http_protocol",
    "http2": "http_protocol",
    "http3": "http_protocol",
}

_FALLBACK_CAPABILITY_TOOLS: Dict[str, List[str]] = {
    "simple_tool_execution": ["shell.exec"],
    "scan_ports": ["information_gathering.network_discovery.nmap"],
    "scan_web": ["information_gathering.web_enumeration.http_request"],
    "http_request": ["information_gathering.web_enumeration.http_request"],
    "http_download": ["information_gathering.web_enumeration.http_download"],
    "http": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "curl": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "download": ["information_gathering.web_enumeration.http_download"],
    "http_debug": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http_session": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http_upload": ["information_gathering.web_enumeration.http_request"],
    "http_tls": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http_resilient": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http_binary": ["information_gathering.web_enumeration.http_request"],
    "http_protocol": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http2": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
    "http3": [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ],
}
def _resolve_catalog_dependencies() -> None:
    """Resolve optional catalog dependencies lazily and independently."""
    global resolve_tools_for_capability
    global filter_visible_tool_ids
    global get_tool_metadata
    global CapabilityType
    global available_tools_fn

    if resolve_tools_for_capability is None:
        try:
            from agent.tools.resolve_tools import resolve_tools_for_capability as resolver

            resolve_tools_for_capability = resolver  # type: ignore[assignment]
        except Exception as exc:
            logger.warning("[catalog] resolve_tools_for_capability unavailable: %s", exc)

    if filter_visible_tool_ids is None:
        try:
            from agent.tools.catalog_visibility import (
                filter_visible_tool_ids as visible_filter_fn,
            )

            filter_visible_tool_ids = visible_filter_fn  # type: ignore[assignment]
        except Exception as exc:
            logger.warning("[catalog] catalog visibility policy unavailable: %s", exc)

    if get_tool_metadata is None:
        try:
            from agent.tools.tool_registry import available_tools as available_tools_resolver
            from agent.tools.tool_registry import get_tool_metadata as metadata_fn

            get_tool_metadata = metadata_fn  # type: ignore[assignment]
            available_tools_fn = available_tools_resolver  # type: ignore[assignment]
        except Exception as exc:
            logger.warning("[catalog] get_tool_metadata unavailable: %s", exc)

    if CapabilityType is None:
        try:
            from agent.graph.infrastructure.state_models import CapabilityType as capability_enum

            CapabilityType = capability_enum  # type: ignore[assignment]
        except Exception as exc:
            logger.warning("[catalog] CapabilityType unavailable: %s", exc)


def _fallback_resolve_tools_for_capability(capability: str) -> List[str]:
    """Resolve tools using local static capability map when resolver is unavailable."""
    normalized = str(capability or "").strip().lower()
    candidates = list(_FALLBACK_CAPABILITY_TOOLS.get(normalized, []))
    if not candidates:
        return []
    try:
        if callable(available_tools_fn):
            existing = set(str(tool_id) for tool_id in (available_tools_fn() or []))
            return [tool_id for tool_id in candidates if tool_id in existing]
    except Exception:
        logger.warning("[catalog] available_tools lookup failed; using static fallback candidates")
    return candidates


def _resolve_tools_with_resilience(
    capability_option: str,
    tool_context: Dict[str, Any],
    config: Optional[Any],
) -> List[str]:
    """Resolve candidates with fallback map when dependency resolver is missing/broken."""
    if callable(resolve_tools_for_capability):
        try:
            return list(resolve_tools_for_capability(capability_option, tool_context, config=config) or [])
        except Exception as exc:
            logger.warning("[catalog] resolver failed for capability '%s': %s", capability_option, exc)
    return _fallback_resolve_tools_for_capability(capability_option)


def _filter_visible_catalog_tools(tool_ids: Iterable[Any]) -> List[str]:
    """Filter candidate tools through the shared visibility policy when available."""
    raw_tool_ids = [str(tool_id) for tool_id in tool_ids if tool_id]
    if callable(filter_visible_tool_ids):
        try:
            return list(filter_visible_tool_ids(raw_tool_ids) or [])
        except Exception as exc:
            logger.warning("[catalog] visibility filter failed: %s", exc)

    logger.warning("[catalog] no shared visibility policy available; returning empty catalog")
    return []


@dataclass(slots=True)
class ToolCatalogEntry:
    """Lightweight metadata describing a tool for prompt construction and ranking."""

    tool_id: str
    name: str
    category: str
    description: str
    # Fields for ranking engine
    disqualification_reason: Optional[str] = None
    capability_aliases: List[str] = field(default_factory=list)
    risk_level: int = 3  # 1-5 scale, default medium risk
    required_privileges: List[str] = field(default_factory=list)
    minimum_budget_minutes: int = 5  # Default 5 minutes
    execution_priority: int = 5  # 1-10 scale, default medium priority


@dataclass(slots=True)
class ToolCatalogResult:
    """Aggregated catalog data derived from intent and capability signals."""

    capability: Optional[str]
    candidates: List[str]
    entries: List[ToolCatalogEntry]
    hints: List[str]
    targets: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "candidates": list(self.candidates),
            "entries": [
                {
                    "tool_id": entry.tool_id,
                    "name": entry.name,
                    "category": entry.category,
                    "description": entry.description,
                }
                for entry in self.entries
            ],
            "hints": list(self.hints),
            "targets": list(self.targets),
        }


def _normalize_targets(hints: Dict[str, Any]) -> List[str]:
    targets = hints.get("targets")
    if not targets:
        return []
    if isinstance(targets, str):
        return [targets]
    if isinstance(targets, Iterable):
        return [str(value) for value in targets if value]
    return []


def _candidate_capabilities(
    explicit_capability: Optional[str],
    intent_hints: Dict[str, Any],
) -> Sequence[str]:
    candidates: List[str] = []
    if explicit_capability:
        candidates.append(str(explicit_capability))

    for hint in intent_hints.get("tool_hints", []) or []:
        mapped = HINT_TO_CAPABILITY.get(str(hint))
        if mapped and mapped not in candidates:
            candidates.append(mapped)

    if not candidates:
        candidates.append("simple_tool_execution")
    return candidates


def build_tool_catalog(
    *,
    capability: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    config: Optional[Any] = None,
) -> ToolCatalogResult:
    """Return candidate tool identifiers and metadata for LangGraph prompts."""
    _resolve_catalog_dependencies()

    metadata = dict(metadata or {})
    intent_hints: Dict[str, Any] = dict(metadata.get("intent_hints") or {})
    tool_context: Dict[str, Any] = dict(metadata.get("tool_context") or {})

    # Normalize capability to CapabilityType if available
    normalized_capability = capability
    if CapabilityType and capability:
        try:
            normalized = CapabilityType.from_intent(capability)
            normalized_capability = normalized.value
            logger.info(
                f"[CAPABILITY] Normalized capability '{capability}' to '{normalized_capability}' "
                "for tool catalog"
            )
        except Exception as exc:
            logger.debug(
                f"[CAPABILITY] Failed to normalize capability '{capability}', using as-is: {exc}"
            )

    targets = _normalize_targets(intent_hints)
    if targets:
        tool_context.setdefault("targets", targets)

    if "current_phase" not in tool_context and metadata.get("current_phase"):
        tool_context["current_phase"] = metadata["current_phase"]

    max_tools = limit
    if max_tools is None:
        try:
            if config and hasattr(config, "max_tools_exposed"):
                max_tools = int(getattr(config, "max_tools_exposed"))
            else:
                max_tools = int(metadata.get("max_tools_exposed", 3))
        except Exception:
            max_tools = 3

    candidate_ids: List[str] = []
    primary_capability: Optional[str] = None

    for capability_option in _candidate_capabilities(normalized_capability, intent_hints):
        resolved = _resolve_tools_with_resilience(
            capability_option=capability_option,
            tool_context=tool_context,
            config=config,
        )
        for tool_id in _filter_visible_catalog_tools(resolved):
            if tool_id not in candidate_ids:
                candidate_ids.append(tool_id)
        if resolved and primary_capability is None:
            primary_capability = capability_option
        if max_tools and len(candidate_ids) >= max_tools:
            break

    if max_tools:
        candidate_ids = candidate_ids[: max(1, max_tools)]

    catalog_entries: List[ToolCatalogEntry] = []
    for tool_id in candidate_ids:
        metadata_payload: Dict[str, Any] = {}
        if callable(get_tool_metadata):
            try:
                metadata_payload = get_tool_metadata(tool_id)
            except Exception:  # pragma: no cover - defensive fallback
                metadata_payload = {}
        entry = ToolCatalogEntry(
            tool_id=tool_id,
            name=str(metadata_payload.get("name") or tool_id),
            category=str(metadata_payload.get("category") or ""),
            description=str(metadata_payload.get("description") or ""),
            # Populate ranking fields from metadata if available
            risk_level=metadata_payload.get("risk_level", 3),
            required_privileges=metadata_payload.get("required_privileges", []),
            minimum_budget_minutes=metadata_payload.get("estimated_runtime_minutes", 5),
            execution_priority=metadata_payload.get("execution_priority", 5),
        )
        catalog_entries.append(entry)

    return ToolCatalogResult(
        capability=primary_capability or normalized_capability,
        candidates=candidate_ids,
        entries=catalog_entries,
        hints=list(intent_hints.get("tool_hints") or []),
        targets=targets,
    )


__all__ = ["ToolCatalogEntry", "ToolCatalogResult", "build_tool_catalog"]
