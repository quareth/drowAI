"""Guardrail tests: PTR node module is not a transcript consumer."""

from __future__ import annotations

import pytest

from agent.graph.nodes.post_tool_reasoning import node


class TestPTRModuleHasNoTranscriptSymbols:
    """Lock module-level transcript isolation for PTR."""

    _FORBIDDEN_MODULE_ATTRS = (
        "build_conversation_history_from_state",
        "METADATA_CONTEXT_BUNDLE_KEY",
        "SECTION_RECENT_TRANSCRIPT",
        "project_for_articulation",
    )

    @pytest.mark.parametrize("attr_name", _FORBIDDEN_MODULE_ATTRS)
    def test_ptr_node_module_does_not_expose_transcript_symbol(
        self, attr_name: str
    ) -> None:
        assert not hasattr(node, attr_name), (
            f"PTR node module must not expose transcript symbol {attr_name!r}."
        )
