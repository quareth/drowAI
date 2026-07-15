from __future__ import annotations

"""Guard tests to ensure referenced tools exist and have enhanced metadata.

These tests only assert for tools referenced by current selectors and
compatibility logic to allow gradual metadata rollout for the rest of the
tooling catalog.
"""

from typing import List, Set

from agent.tools.tool_registry import available_tools
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.action_mapper import ContextualToolSelector
from agent.tools.compatibility import ToolCompatibilityAnalyzer
from agent.models import ActionType


def _collect_referenced_tools() -> Set[str]:
    selector = ContextualToolSelector()
    referenced: Set[str] = set()

    # Action map
    for action in ActionType:
        referenced.update(selector._get_base_tools_for_action(action))

    # Phase map
    for tools in selector._build_phase_tool_map().values():
        referenced.update(tools)

    # Service map (enumeration-focused)
    for tools in selector._build_service_tool_map().values():
        referenced.update(tools)

    # Compatibility matrix participants
    compat = ToolCompatibilityAnalyzer()
    for (t1, t2) in compat.compatibility_matrix.keys():
        referenced.add(t1)
        referenced.add(t2)

    return referenced


def test_referenced_tools_exist_and_have_metadata():
    available = set(available_tools())
    referenced = _collect_referenced_tools()

    missing_modules = sorted(t for t in referenced if t not in available)
    assert not missing_modules, f"Referenced tool modules missing: {missing_modules}"

    missing_metadata = sorted(t for t in referenced if get_enhanced_tool_metadata(t) is None)
    assert not missing_metadata, f"Referenced tools missing enhanced metadata: {missing_metadata}"


