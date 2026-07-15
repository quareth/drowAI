"""YAML rule loader and lightweight DSL helpers for tool-specific profiles.

Provides a minimal rule DSL parser and utilities to apply:
- detect: simple hints (currently not enforced here; used by caller)
- extract: list of {field, regex} applied to text to enrich metadata
- chunk: grouping hints (group_by) and limits (max_tokens)
- summarize: simple string template rendered per group

This module avoids heavy dependencies and sticks to simple regex compilation
and string formatting to keep determinism and testability."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple
import re

from agent.context.index.schemas import IngestionError


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise IngestionError(f"YAML profiles require PyYAML: {e}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise IngestionError("Invalid profile format: expected mapping at root")
    return data


def load_profile_for_tool(profiles_dir: str, tool_name: str) -> Dict[str, Any] | None:
    """Load YAML profile if present for `tool_name`.

    Returns None when profile not found; caller can fallback to universal heuristics.
    """
    candidates = [
        os.path.join(profiles_dir, f"{tool_name.lower()}.yaml"),
        os.path.join(profiles_dir, f"{tool_name.lower()}.yml"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return _load_yaml(p)
    return None


def validate_profile(profile: Dict[str, Any]) -> None:
    # Minimal validation for required top-level keys
    allowed = {"detect", "extract", "chunk", "summarize"}
    unknown = set(profile.keys()) - allowed
    if unknown:
        # allow extra keys but warn through exception message (caught by caller if needed)
        # Not raising hard error to keep forward-compatibility.
        pass
    # Basic structure checks
    if "detect" in profile and not isinstance(profile["detect"], dict):
        raise IngestionError("profile.detect must be a mapping")
    if "extract" in profile and not isinstance(profile["extract"], list):
        raise IngestionError("profile.extract must be a list of mappings")
    if "chunk" in profile and not isinstance(profile["chunk"], list):
        raise IngestionError("profile.chunk must be a list")
    if "summarize" in profile and not isinstance(profile["summarize"], dict):
        raise IngestionError("profile.summarize must be a mapping")


# -----------------------------
# DSL helpers (minimal, typed)
# -----------------------------

class CompiledRuleSet:
    def __init__(self, *, group_by: List[str] | None, summarize_template: str | None, extract_rules: List[Tuple[str, re.Pattern]]):
        self.group_by = group_by or []
        self.summarize_template = summarize_template
        self.extract_rules = extract_rules


def compile_profile(profile: Dict[str, Any]) -> CompiledRuleSet:
    """Compile regex extractors and normalize chunk/summarize rules."""
    validate_profile(profile)
    # extract rules
    extract_rules: List[Tuple[str, re.Pattern]] = []
    for rule in profile.get("extract", []) or []:
        if not isinstance(rule, dict):
            continue
        field = rule.get("field")
        rgx = rule.get("regex")
        if not field or not rgx:
            continue
        try:
            extract_rules.append((str(field), re.compile(str(rgx), re.MULTILINE)))
        except Exception:
            # Skip invalid patterns instead of failing ingestion
            continue
    # chunk rules
    group_by: List[str] | None = None
    for rule in profile.get("chunk", []) or []:
        if isinstance(rule, dict):
            if "group_by" in rule and isinstance(rule["group_by"], list):
                group_by = [str(x) for x in rule["group_by"]]
    # summarize
    summarize_template = None
    if isinstance(profile.get("summarize"), dict):
        tmpl = profile["summarize"].get("template")
        if isinstance(tmpl, str) and tmpl.strip():
            summarize_template = tmpl
    return CompiledRuleSet(group_by=group_by, summarize_template=summarize_template, extract_rules=extract_rules)


def apply_extractors(rules: CompiledRuleSet, text: str) -> Dict[str, Any]:
    """Run compiled extract regexes against text and return extracted fields.

    Uses the first matching group if present; otherwise the full match.
    """
    out: Dict[str, Any] = {}
    for field, pat in rules.extract_rules:
        try:
            m = pat.search(text)
            if m:
                out[field] = m.group(1) if m.groups() else m.group(0)
        except Exception:
            continue
    return out


def group_key_for(rules: CompiledRuleSet, meta: Dict[str, Any]) -> Tuple:
    """Compute a deterministic group key tuple per chunk based on group_by fields."""
    if not rules.group_by:
        return tuple()
    vals = []
    for k in rules.group_by:
        vals.append(str(meta.get(k, "")))
    return tuple(vals)


def render_group_summary(template: str, chunks_meta: List[Dict[str, Any]]) -> str:
    """Render a minimal group summary using a template.

    Supported keys in the template (if present):
    - count: number of items
    - c2xx/c3xx/c4xx/c5xx: counts by status_class
    - top_paths: comma-joined top 3 url_path values
    """
    try:
        count = len(chunks_meta)
        # status_class counts
        classes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        paths: Dict[str, int] = {}
        for m in chunks_meta:
            sc = str(m.get("status_class") or "")
            if sc in classes:
                classes[sc] += 1
            up = m.get("url_path") or m.get("path")
            if up:
                paths[str(up)] = paths.get(str(up), 0) + 1
        top_paths = ", ".join(x for x, _ in sorted(paths.items(), key=lambda x: (-x[1], x[0]))[:3])
        return template.format(
            count=count,
            c2xx=classes["2xx"],
            c3xx=classes["3xx"],
            c4xx=classes["4xx"],
            c5xx=classes["5xx"],
            top_paths=top_paths,
            entity="",
        )
    except Exception:
        # Fallback to template verbatim if formatting fails
        return template
