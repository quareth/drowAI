"""Tests for active-todo stall guard behavior."""

from __future__ import annotations

from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningOutput,
    TodoProgress,
    ToolIntent,
)
from agent.graph.state import (
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
)
from agent.graph.utils.todo_stall_guard import (
    TODO_STALL_METADATA_KEY,
    apply_active_todo_stall_guard,
    render_todo_stall_prompt_section,
)


def _state(
    *,
    metadata: dict | None = None,
    todo: TodoItem | None = None,
) -> InteractiveState:
    facts = FactsState(
        task_id=123,
        message="Resolve target",
        conversation_id="conv-123",
        capability="deep_reasoning",
        todo_list=[todo] if todo is not None else [],
        metadata=metadata or {},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def _active_todo(description: str = "Resolve target hostname") -> TodoItem:
    return TodoItem(description=description, status=TodoStatus.IN_PROGRESS)


def _call_tool_output() -> PostToolReasoningOutput:
    return PostToolReasoningOutput(
        observation="The prior tool did not resolve the active objective.",
        next_action="call_tool",
        action_reasoning="Need another attempt",
        tool_intent=ToolIntent(
            description="Try another resolver",
            target="cve-2018-7600-web-1",
            focus="hostname resolution",
        ),
        user_goal_achieved=False,
        todo_progress=[],
    )


def test_no_active_todo_is_noop() -> None:
    state = _state(metadata={TODO_STALL_METADATA_KEY: {"count": 2}})
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is False
    assert output.next_action == "call_tool"
    assert TODO_STALL_METADATA_KEY not in state.facts.metadata


def test_first_no_progress_call_increments_without_reflect() -> None:
    state = _state(todo=_active_todo())
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is False
    assert output.next_action == "call_tool"
    assert state.facts.metadata[TODO_STALL_METADATA_KEY]["count"] == 1


def test_progress_update_resets_counter() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
            }
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()
    output.todo_progress = [
        TodoProgress(
            index=0,
            status="completed",
            completion_type="negative",
            completion_reason="NXDOMAIN",
        )
    ]

    changed = apply_active_todo_stall_guard(
        state,
        output,
        todo_updates=[{"index": 0, "status": "completed"}],
    )

    assert changed is False
    assert output.next_action == "call_tool"
    assert TODO_STALL_METADATA_KEY not in state.facts.metadata


def test_unrelated_todo_progress_does_not_reset_active_stall() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 1,
            }
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(
        state,
        output,
        todo_updates=[{"index": 1, "status": "completed"}],
    )

    assert changed is False
    assert output.next_action == "call_tool"
    assert state.facts.metadata[TODO_STALL_METADATA_KEY]["count"] == 2


def test_active_todo_change_resets_streak_to_one() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Old todo",
                "count": 2,
            }
        },
        todo=_active_todo("New todo"),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is False
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["count"] == 1
    assert tracking["description"] == "New todo"


def test_third_no_progress_call_forces_reflect() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
                "threshold": 3,
            }
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is True
    assert output.next_action == "reflect"
    assert output.retry_suggested is False
    assert output.tool_intent is None
    assert "Override: active todo stalled without progress" in output.action_reasoning
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["count"] == 3
    assert tracking["forced_action"] == "reflect"
    assert tracking["post_reflect_awaiting_progress"] is True


def test_ptr_reflect_choice_is_preserved_and_marks_post_reflect_wait() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
            }
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()
    output.next_action = "reflect"
    output.tool_intent = None

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is False
    assert output.next_action == "reflect"
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["forced_action"] == "reflect"
    assert tracking["post_reflect_awaiting_progress"] is True


def test_post_reflect_no_progress_call_forces_synthesis() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 3,
                "threshold": 3,
                "forced_action": "reflect",
                "post_reflect_awaiting_progress": True,
            }
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is True
    assert output.next_action == "synthesis"
    assert output.retry_suggested is False
    assert output.tool_intent is None
    assert (
        "Override: active todo still stalled after reflection"
        in output.action_reasoning
    )
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["forced_action"] == "synthesis"
    assert tracking["post_reflect_awaiting_progress"] is False
    assert tracking["last_reason"] == "call_tool_without_progress_after_reflect"


def test_three_no_progress_tool_phases_force_reflect() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
                "threshold": 3,
            },
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is True
    assert output.next_action == "reflect"
    assert output.tool_intent is None
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["forced_action"] == "reflect"
    assert tracking["post_reflect_awaiting_progress"] is True


def test_post_reflect_no_progress_forces_synthesis() -> None:
    state = _state(
        metadata={
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 3,
                "threshold": 3,
                "forced_action": "reflect",
                "post_reflect_awaiting_progress": True,
            },
        },
        todo=_active_todo(),
    )
    output = _call_tool_output()

    changed = apply_active_todo_stall_guard(state, output)

    assert changed is True
    assert output.next_action == "synthesis"
    assert output.tool_intent is None
    tracking = state.facts.metadata[TODO_STALL_METADATA_KEY]
    assert tracking["forced_action"] == "synthesis"
    assert tracking["post_reflect_awaiting_progress"] is False


def test_prompt_section_renders_count_and_active_todo() -> None:
    section = render_todo_stall_prompt_section(
        {
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 2,
                "threshold": 3,
            }
        }
    )

    assert "Active todo [0] `Resolve target hostname`" in section
    assert "2 consecutive no-progress tool phases" in section
    assert "Prefer reflect or finalize" in section


def test_prompt_section_renders_post_reflect_warning() -> None:
    section = render_todo_stall_prompt_section(
        {
            TODO_STALL_METADATA_KEY: {
                "index": 0,
                "description": "Resolve target hostname",
                "count": 3,
                "threshold": 3,
                "post_reflect_awaiting_progress": True,
            }
        }
    )

    assert "A reflection was already attempted" in section
    assert "synthesize or finalize" in section


def test_prompt_section_omits_empty_tracking() -> None:
    assert render_todo_stall_prompt_section({}) == ""
