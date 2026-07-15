"""Shared CVSS extraction helpers for CVE indexing/read-model consumers.

Scope:
- Extracts deterministic CVSS base score values from supported metrics payloads.
- Supports canonical top-level `cvss_score` and nested metrics entry structures.

Boundary:
- Contains no DB access and no ranking/orchestration behavior.
"""

from __future__ import annotations

from typing import Any


def extract_cvss_score(metrics: Any) -> float | None:
    """Return best-available CVSS base score from known metrics shapes."""
    if isinstance(metrics, dict):
        direct = _to_float(metrics.get("cvss_score"))
        if direct is not None:
            return direct

        entries = metrics.get("entries")
        if isinstance(entries, list):
            from_entries = _extract_from_entries(entries)
            if from_entries is not None:
                return from_entries

        from_tree = _extract_from_tree(metrics)
        if from_tree is not None:
            return from_tree
        return None

    if isinstance(metrics, list):
        return _extract_from_entries(metrics)
    return None


def _extract_from_entries(entries: list[Any]) -> float | None:
    scores: list[float] = []
    for item in entries:
        score = _extract_from_tree(item)
        if score is not None:
            scores.append(score)
    if not scores:
        return None
    return max(scores)


def _extract_from_tree(value: Any, *, depth: int = 0) -> float | None:
    if depth > 6:
        return None

    found_scores: list[float] = []

    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() == "basescore":
                score = _to_float(nested)
                if score is not None:
                    found_scores.append(score)
                continue
            nested_score = _extract_from_tree(nested, depth=depth + 1)
            if nested_score is not None:
                found_scores.append(nested_score)
    elif isinstance(value, list):
        for item in value:
            nested_score = _extract_from_tree(item, depth=depth + 1)
            if nested_score is not None:
                found_scores.append(nested_score)

    if not found_scores:
        return None
    return max(found_scores)


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


__all__ = ["extract_cvss_score"]
