"""Unit tests for centralized todo progression authority helpers."""

from agent.graph.state import CompletionType, TodoItem, TodoStatus
from agent.graph.utils.plan_progress_authority import (
    apply_llm_updates,
    build_todo_stream_updates,
    ensure_initial_in_progress,
    resolve_active_todo,
    to_prompt_status,
    to_stream_status,
)


def _make_todos() -> list[TodoItem]:
    return [
        TodoItem(description="Step 1"),
        TodoItem(description="Step 2"),
        TodoItem(description="Step 3"),
    ]


def test_ensure_initial_in_progress_activates_first_pending() -> None:
    todos = _make_todos()

    changed = ensure_initial_in_progress(todos)

    assert changed is True
    assert todos[0].status == TodoStatus.IN_PROGRESS
    assert todos[0].started_at is not None
    assert todos[1].status == TodoStatus.PENDING


def test_ensure_initial_in_progress_does_not_duplicate_active_step() -> None:
    todos = _make_todos()
    todos[1].status = TodoStatus.IN_PROGRESS

    changed = ensure_initial_in_progress(todos)

    assert changed is False
    assert sum(todo.status == TodoStatus.IN_PROGRESS for todo in todos) == 1
    assert todos[1].status == TodoStatus.IN_PROGRESS


def test_apply_llm_updates_maps_completed_and_skipped_statuses() -> None:
    todos = _make_todos()
    changed = apply_llm_updates(
        todos,
        [
            {
                "index": 0,
                "status": "completed",
                "completion_type": "positive",
                "completion_reason": "Found target",
            },
            {
                "index": 1,
                "status": "skipped",
                "completion_reason": "Used fallback host",
            },
        ],
    )

    assert changed == {0, 1}
    assert todos[0].status == TodoStatus.COMPLETE_POSITIVE
    assert todos[0].completion_type == CompletionType.POSITIVE
    assert todos[1].status == TodoStatus.COMPLETE_NEGATIVE
    assert todos[1].completion_type == CompletionType.NEGATIVE
    assert todos[1].completion_reasoning == "Skipped: Used fallback host"
    assert to_prompt_status(todos[0]) == "completed"
    assert to_prompt_status(todos[1]) == "skipped"
    assert to_stream_status(todos[1]) == "skipped"


def test_apply_llm_updates_handles_multiple_completions_in_one_call() -> None:
    todos = _make_todos()

    changed = apply_llm_updates(
        todos,
        [
            {"index": 0, "status": "completed", "completion_type": "positive"},
            {"index": 1, "status": "completed", "completion_type": "negative"},
        ],
    )

    assert changed == {0, 1}
    assert todos[0].status == TodoStatus.COMPLETE_POSITIVE
    assert todos[1].status == TodoStatus.COMPLETE_NEGATIVE


def test_apply_llm_updates_ignores_invalid_index_and_continues() -> None:
    todos = _make_todos()

    changed = apply_llm_updates(
        todos,
        [
            {"index": 99, "status": "completed"},
            {"index": 2, "status": "in_progress"},
        ],
    )

    assert changed == {2}
    assert todos[0].status == TodoStatus.PENDING
    assert todos[2].status == TodoStatus.IN_PROGRESS
    assert todos[2].started_at is not None


def test_build_todo_stream_updates_is_deterministic() -> None:
    before = _make_todos()
    after = _make_todos()
    after[0].status = TodoStatus.IN_PROGRESS

    first = build_todo_stream_updates(before, after, ["a", "b", "c"])
    second = build_todo_stream_updates(before, after, ["a", "b", "c"])

    assert first == second
    assert first == [
        {
            "id": "a",
            "text": "Step 1",
            "status": "in_progress",
            "index": 0,
        }
    ]


# ---------------------------------------------------------------------------
# resolve_active_todo — "current in-progress item only" authority used by
# the context bundle to inject the active plan step into tool-selection
# layers (category selector, planner/tool-plan, articulation).
# ---------------------------------------------------------------------------


def test_resolve_active_todo_returns_first_in_progress_with_index() -> None:
    todos = _make_todos()
    todos[1].status = TodoStatus.IN_PROGRESS

    active = resolve_active_todo(todos)

    assert active == {"index": 1, "description": "Step 2"}


def test_resolve_active_todo_returns_none_when_no_in_progress() -> None:
    todos = _make_todos()
    # All PENDING by default — nothing is active.
    assert resolve_active_todo(todos) is None


def test_resolve_active_todo_returns_none_for_terminal_only_list() -> None:
    todos = _make_todos()
    for todo in todos:
        todo.status = TodoStatus.COMPLETE_POSITIVE
        todo.completion_type = CompletionType.POSITIVE

    assert resolve_active_todo(todos) is None


def test_resolve_active_todo_returns_none_for_legacy_list_of_strings() -> None:
    """Legacy ``List[str]`` carries no status signal; resolver returns None."""
    assert resolve_active_todo(["Step 1", "Step 2"]) is None


def test_resolve_active_todo_returns_none_for_empty_or_missing_list() -> None:
    assert resolve_active_todo([]) is None
    assert resolve_active_todo(None) is None


def test_resolve_active_todo_picks_first_in_progress_when_multiple_set() -> None:
    todos = _make_todos()
    todos[0].status = TodoStatus.IN_PROGRESS
    todos[2].status = TodoStatus.IN_PROGRESS

    active = resolve_active_todo(todos)

    assert active == {"index": 0, "description": "Step 1"}


def test_resolve_active_todo_skips_item_with_blank_description() -> None:
    todos = [
        TodoItem(description="   "),
        TodoItem(description="Step 2"),
    ]
    todos[0].status = TodoStatus.IN_PROGRESS

    # Blank description cannot be surfaced as an actionable goal.
    assert resolve_active_todo(todos) is None
