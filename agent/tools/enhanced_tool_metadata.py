"""Legacy rich tool description helpers (compatibility layer).

This file originally implemented a second "enhanced metadata" registry to build
LLM-friendly tool descriptions (purpose, use-cases, critical notes, examples).

The codebase now uses **per-tool co-located metadata** registered via
`agent.tools.enhanced_metadata_registry.register_enhanced_tool_metadata`.

To keep **one source of truth** without breaking existing imports, this module
is now a thin adapter that derives rich descriptions from the new registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ToolCapability:
    """A specific capability of a tool."""
    
    name: str
    description: str
    when_to_use: str
    parameters: str
    example: str


@dataclass
class EnhancedToolMetadata:
    """Rich metadata for a tool that enables LLM reasoning."""
    
    tool_id: str
    purpose: str
    capabilities: List[ToolCapability]
    critical_notes: List[str]
    typical_use_cases: List[str]


def register_enhanced_metadata(metadata: EnhancedToolMetadata) -> None:
    """Deprecated: kept for backward compatibility.

    The platform now uses the per-tool registry in `enhanced_metadata_registry`.
    This function is intentionally a no-op to avoid introducing a second source
    of truth.
    """
    _ = metadata


def get_enhanced_metadata(tool_id: str) -> Optional[EnhancedToolMetadata]:
    """Return legacy-shaped rich metadata derived from the new per-tool registry."""
    try:
        from .enhanced_metadata_registry import get_enhanced_tool_metadata
    except Exception:
        return None

    new_meta = get_enhanced_tool_metadata(tool_id)
    if new_meta is None:
        return None

    # Derive a legacy "purpose" string.
    purpose = new_meta.display_name
    if new_meta.category:
        purpose = f"{new_meta.display_name} ({new_meta.category.value})"

    # Derive capabilities with best-effort extraction for legacy fields.
    # Try to extract "when to use" hints from the description if it contains guidance.
    legacy_caps: List[ToolCapability] = []
    for cap in (new_meta.capabilities or []):
        # Extract "when to use" hint from description if present
        # Look for patterns like "use this to", "use when", "use for"
        desc = cap.description or ""
        when_to_use = ""
        desc_lower = desc.lower()
        
        # Try to extract usage guidance from description
        for marker in ["use this to", "use when", "use for:", "- use "]:
            if marker in desc_lower:
                # Use the part of description after the marker as "when_to_use"
                idx = desc_lower.find(marker)
                when_to_use = desc[idx:].strip()
                break
        
        # If no explicit guidance, use the full description as hint
        if not when_to_use and desc:
            when_to_use = f"When you need to: {desc}"
        
        legacy_caps.append(
            ToolCapability(
                name=cap.name,
                description=desc,
                when_to_use=when_to_use,
                parameters="",
                example="",
            )
        )

    # Phases and services are useful as "critical notes" context.
    critical_notes: List[str] = []
    if new_meta.required_services:
        critical_notes.append(f"Requires services: {', '.join(new_meta.required_services)}")
    if new_meta.target_protocols:
        critical_notes.append(f"Target protocols: {', '.join(new_meta.target_protocols)}")
    if new_meta.applicable_phases:
        phases = ", ".join(p.value for p in new_meta.applicable_phases)
        critical_notes.append(f"Applicable phases: {phases}")

    typical_use_cases: List[str] = []
    for cap in (new_meta.capabilities or []):
        typical_use_cases.append(f"{cap.name}: {cap.description}")

    return EnhancedToolMetadata(
        tool_id=tool_id,
        purpose=purpose,
        capabilities=legacy_caps,
        critical_notes=critical_notes,
        typical_use_cases=typical_use_cases,
    )


def build_rich_tool_description(tool_id: str) -> str:
    """Build a rich, LLM-friendly description of a tool.
    
    This description enables the LLM to reason about when and how to use the tool.
    """
    metadata = get_enhanced_metadata(tool_id)
    if not metadata:
        # Fallback to basic description from tool registry
        try:
            from .tool_registry import get_tool_metadata

            basic_meta = get_tool_metadata(tool_id)
            if basic_meta:
                return f"{tool_id}: {basic_meta.get('description', 'No description')}"
        except Exception:
            pass  # Tool doesn't exist or registry failed
        return f"{tool_id}: No metadata available"
    
    # Build rich description
    lines = [
        f"Tool: {tool_id}",
        f"Purpose: {metadata.purpose}",
        "",
        "Capabilities:",
    ]
    
    for cap in metadata.capabilities:
        lines.extend([
            f"  * {cap.name}",
            f"    Description: {cap.description}",
            f"    When to use: {cap.when_to_use}",
            f"    Parameters: {cap.parameters}",
            f"    Example: {cap.example}",
            "",
        ])
    
    if metadata.critical_notes:
        lines.append("CRITICAL NOTES:")
        for note in metadata.critical_notes:
            lines.append(f"  [!] {note}")
        lines.append("")
    
    if metadata.typical_use_cases:
        lines.append("Typical Use Cases:")
        for use_case in metadata.typical_use_cases:
            lines.append(f"  * {use_case}")
        lines.append("")
    
    return "\n".join(lines)


def build_rich_tool_catalog(tool_ids: List[str]) -> str:
    """Build a rich catalog of tools for LLM reasoning.
    
    This catalog provides detailed information about each tool, enabling
    the LLM to select the appropriate tool and parameters without hardcoded mappings.
    """
    if not tool_ids:
        return "No tools available"
    
    descriptions = []
    for tool_id in tool_ids:
        desc = build_rich_tool_description(tool_id)
        descriptions.append(desc)
    
    catalog = "\n" + "="*80 + "\n\n"
    catalog += "\n\n".join(descriptions)
    catalog += "\n" + "="*80 + "\n"
    
    return catalog


def build_tool_catalog_entries(tool_ids: List[str]) -> List[Dict[str, str]]:
    """Build planner-facing catalog entries from tool IDs.

    Returns lightweight dict entries used by LLM tool selection:
    `{"id", "name", "category", "description"}`.
    """
    try:
        from .tool_registry import get_tool_metadata
    except Exception:
        get_tool_metadata = None  # type: ignore

    try:
        from .enhanced_metadata_registry import get_enhanced_tool_metadata
    except Exception:
        get_enhanced_tool_metadata = None  # type: ignore

    def _compact_tool_description(value: object, *, max_chars: int = 200) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        sentence = text.split(". ", 1)[0].strip()
        compact = sentence or text
        if len(compact) <= max_chars:
            return compact.rstrip(".")
        return compact[: max_chars - 3].rstrip() + "..."

    def _enhanced_tool_description(tool_id: str) -> str:
        if get_enhanced_tool_metadata is None:
            return ""
        try:
            enhanced = get_enhanced_tool_metadata(tool_id)
        except Exception:
            return ""
        if enhanced is None:
            return ""
        descriptions = [
            str(cap.description or "").strip().rstrip(".")
            for cap in (enhanced.capabilities or [])
            if str(cap.description or "").strip()
        ]
        return _compact_tool_description("; ".join(descriptions))

    def _enhanced_tool_category(tool_id: str) -> str:
        if get_enhanced_tool_metadata is None:
            return ""
        try:
            enhanced = get_enhanced_tool_metadata(tool_id)
        except Exception:
            return ""
        if enhanced is None:
            return ""
        category = getattr(enhanced, "category", None)
        value = getattr(category, "value", None)
        return value if isinstance(value, str) else ""

    catalog: List[Dict[str, str]] = []
    for tool_id in tool_ids:
        name = tool_id.split(".")[-1]
        category = tool_id.split(".")[0] if "." in tool_id else _enhanced_tool_category(tool_id)
        description = _enhanced_tool_description(tool_id)
        if get_tool_metadata is not None:
            try:
                meta = get_tool_metadata(tool_id)
                if isinstance(meta, dict):
                    raw_name = str(meta.get("name", "") or "").strip()
                    if raw_name and raw_name != tool_id:
                        name = raw_name
                    if not description:
                        description = _compact_tool_description(meta.get("description", "") or "")
            except Exception:
                pass

        catalog.append(
            {
                "id": tool_id,
                "name": name,
                "category": category,
                "description": description,
            }
        )

    return catalog


__all__ = [
    "ToolCapability",
    "EnhancedToolMetadata",
    "register_enhanced_metadata",
    "get_enhanced_metadata",
    "build_rich_tool_description",
    "build_rich_tool_catalog",
    "build_tool_catalog_entries",
]
