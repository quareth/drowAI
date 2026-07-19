"""Tests for deployment identity handoff at successful turn finalization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.langgraph_chat.execution.turn_service import TurnExecutionService


@pytest.mark.asyncio
async def test_finalizer_uses_authoritative_runtime_selection_for_usage() -> None:
    """Usage keeps deployment identity even when graph result metadata omits it."""

    runtime_selection = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": "00000000-0000-0000-0000-000000000001",
            "expected_revision": 1,
        },
        "preferred_route_id": "00000000-0000-0000-0000-000000000002",
    }
    service = SimpleNamespace(
        _publish_boundary_completion_events=AsyncMock(),
        _context_window_handoff_fields=MagicMock(return_value={}),
        _compression_handoff_fields=MagicMock(return_value={}),
    )
    completed = MagicMock()
    result = SimpleNamespace(metadata={}, usage=[MagicMock()])

    with patch(
        "backend.services.langgraph_chat.execution.turn_service.record_usage_list_best_effort"
    ) as record_usage:
        await TurnExecutionService._finalize_successful_turn_result(
            service,
            task_id=2,
            user_id=1,
            hub=MagicMock(),
            final_content="done",
            result=result,
            conversation_id="conversation",
            turn_id="turn",
            turn_sequence=1,
            workflow_id=3,
            mark_turn_workflow_completed=completed,
            completion_source="initial_generation",
            context_window_metadata=None,
            runtime_selection=runtime_selection,
        )

    assert record_usage.call_args.kwargs["runtime_selection"] == runtime_selection
