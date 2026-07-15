"""Phase 3 Task 3.1 cutover: category selector no longer reads the bundle.

Before Task 3.1, this module asserted that the node raised
``RuntimeError`` when ``metadata[context_bundle]`` was missing because
the node still resolved a transcript ``history_text`` value from the
bundle for the transitional builder signature. The cutover deletes
that transcript resolver entirely — the node now consumes only the
classifier-derived ``working_memory.intent_brief`` and is no
longer a bundle consumer on the category-selection hot path.

This test locks that new invariant: with no bundle installed, the
node proceeds to select categories from the brief (or falls back to
``information_gathering`` via the taxonomy path), and a missing
bundle is NOT a hard error on this node anymore.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.nodes.select_tool_categories import select_tool_categories_node


@pytest.mark.asyncio
async def test_category_selector_runs_without_bundle_after_cutover() -> None:
    """After Task 3.1 the category selector does not require the bundle.

    The node reads ``metadata["working_memory"]["intent_brief"]`` only; no bundle
    is installed here and no ``RuntimeError`` is raised.
    """
    state: Dict[str, Any] = {
        "facts": {
            "task_id": 1,
            "message": "scan the host",
            "selected_tool": None,
            "tool_parameters": {},
            "metadata": {
                "api_key": "test-key",
                # Populated brief; no context_bundle present.
                "working_memory": {
                    "intent_brief": {
                        "resolved_user_intent": "Scan the host for open ports",
                        "overall_goal": "Map exposed services",
                        "next_operational_goal": "Run TCP scan",
                        "success_condition": "Open ports enumerated",
                        "execution_readiness": "ready",
                        "resolved_target": "10.0.0.5",
                        "target_status": "resolved",
                        "target_source": "explicit_current_message",
                        "explicit_constraints": [],
                        "suggested_category_focus": ["information_gathering"],
                        "retrieval_hints": [],
                        "relevant_memory_fragments": [],
                        "request_contract": {
                            "question_type": "multi_step",
                            "answer_style": "normal",
                            "terminal_when": "all_steps_done",
                        },
                    },
                },
            },
        },
        "trace": {
            "history": [],
            "reasoning": [],
        },
    }

    with patch(
        "agent.tools.category_utils.get_tool_categories",
        return_value=["information_gathering", "web_applications"],
    ), patch(
        "agent.tools.category_utils.get_category_descriptions",
        return_value={
            "information_gathering": "Network recon",
            "web_applications": "Web testing",
        },
    ), patch(
        "agent.graph.nodes.select_tool_categories._call_llm_for_categories",
        new=AsyncMock(return_value=["information_gathering"]),
    ):
        result = await select_tool_categories_node(state)

    assert "facts" in result
    metadata = result["facts"]["metadata"]
    assert metadata["selected_categories"] == ["information_gathering"]
