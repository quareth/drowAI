"""Guardrail test that locks the web-surface consumption layer as tool-agnostic.

The web-surface projection was designed to consume `web.path_discovered`
observations uniformly, regardless of which producer emitted them. Any
branching on producer identity (ffuf / gobuster / nuclei / sqlmap / ...) in
the projector, query, router, or UI panel would silently reintroduce the
per-tool coupling that the adapter layer already encapsulates. This test
scans the pinned consumption-layer files and fails if any tool-name token
appears in them.

Scope (scan targets are exactly the files the plan pinned):
- `backend/services/knowledge/projection/web_path_projector.py`
- `backend/services/knowledge/query/selectors.py`
- `backend/services/knowledge/query/mappers.py`
- `backend/routers/engagement_knowledge.py` (web-surface routes only)
- `client/src/components/engagements/territory/web-surface-panel.tsx`
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_CONSUMPTION_LAYER_FILES: tuple[str, ...] = (
    "backend/services/knowledge/projection/web_path_projector.py",
    "backend/services/knowledge/query/selectors.py",
    "backend/services/knowledge/query/mappers.py",
    "backend/routers/engagement_knowledge.py",
    "client/src/components/engagements/territory/web-surface-panel.tsx",
)

_TOOL_NAME_TOKENS: tuple[str, ...] = (
    "ffuf",
    "gobuster",
    "nuclei",
    "sqlmap",
    "nmap",
    "masscan",
    "msfconsole",
)

_TOOL_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(token) for token in _TOOL_NAME_TOKENS) + r")\b",
    re.IGNORECASE,
)


def _line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def test_web_surface_consumption_layer_is_tool_agnostic() -> None:
    violations: list[str] = []
    for relative in _CONSUMPTION_LAYER_FILES:
        path = REPO_ROOT / relative
        assert path.is_file(), f"Expected consumption-layer file missing: {relative}"
        text = path.read_text(encoding="utf-8")
        for match in _TOOL_NAME_PATTERN.finditer(text):
            line_no = _line_number_for_offset(text, match.start())
            violations.append(f"{relative}:{line_no} matched `{match.group(0)}`")

    assert not violations, (
        "Web-surface consumption layer must stay tool-agnostic. Observations arrive "
        "through `web.path_discovered` and must be consumed uniformly. Route producer-"
        "specific logic through tool-local adapters instead of the projector, query, "
        "router, or panel.\n" + "\n".join(violations)
    )


def test_tool_name_pattern_catches_common_branching_forms() -> None:
    # The pattern must catch common branching shapes so the guardrail cannot be
    # bypassed by stylistic variation.
    assert _TOOL_NAME_PATTERN.search('if source == "ffuf":')
    assert _TOOL_NAME_PATTERN.search("if tool_name == 'gobuster':")
    assert _TOOL_NAME_PATTERN.search('const isNuclei = producer === "nuclei";')
    assert _TOOL_NAME_PATTERN.search("NMAP_PRODUCER = 'nmap'")
    # Case-insensitive match.
    assert _TOOL_NAME_PATTERN.search('if "FFUF" in producers:')
