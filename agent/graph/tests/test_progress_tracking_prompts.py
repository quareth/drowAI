"""Tests for: Progress Tracking Prompt Updates.

Tests the prompt changes for LLM-driven progress tracking:
- System prompt includes progress tracking section
- Todo list formatted with indices
- User prompt includes progress instructions"""

import pytest

from core.prompts.builders.post_tool import (
    PostToolReasoningPromptBuilder,
    SYSTEM_PROMPT,
    MAX_TODOS_IN_PROMPT,
)
from agent.graph.state import FactsState, InteractiveState, TraceState, TodoItem, TodoStatus
from agent.graph.utils.todo_stall_guard import TODO_STALL_METADATA_KEY


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def prompt_builder() -> PostToolReasoningPromptBuilder:
    """Create a prompt builder instance."""
    return PostToolReasoningPromptBuilder()


@pytest.fixture
def sample_state_with_todos() -> InteractiveState:
    """Create a sample state with todo list."""
    facts = FactsState(
        task_id=123,
        message="Scan the network for hosts, then scan their ports. If no hosts found, use 127.0.0.1",
        conversation_id="conv-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        tool_parameters={"target": "192.168.1.0/24"},
        current_goal="Discover live hosts on the network",
        plan=["Host discovery", "Port scan hosts", "Service enumeration"],
        todo_list=[
            "Discover live hosts on network",
            "Port scan discovered hosts",
            "Enumerate services on open ports",
        ],
        metadata={},
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def sample_state_with_todo_items() -> InteractiveState:
    """Create a sample state with TodoItem objects."""
    facts = FactsState(
        task_id=123,
        message="Scan for vulnerabilities",
        conversation_id="conv-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        todo_list=[
            TodoItem(description="Discover live hosts", status=TodoStatus.COMPLETE_POSITIVE),
            TodoItem(description="Port scan hosts", status=TodoStatus.IN_PROGRESS),
            TodoItem(description="Check for vulnerabilities", status=TodoStatus.PENDING),
        ],
        metadata={},
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def sample_synthesized() -> dict:
    """Create sample synthesized tool output."""
    return {
        "tool": "nmap",
        "summary": "Found 2 hosts on the network.",
        "key_findings": ["192.168.1.1 is up", "192.168.1.5 is up"],
        "vulnerabilities": [],
        "next_actions": ["Port scan discovered hosts"],
    }


# -----------------------------------------------------------------------------
# System Prompt Tests
# -----------------------------------------------------------------------------


class TestSystemPromptProgressTracking:
    """Tests for progress tracking section in system prompt."""
    
    def test_system_prompt_contains_progress_tracking_section(self):
        """System prompt should contain progress tracking section."""
        assert "## Progress Tracking (CRITICAL)" in SYSTEM_PROMPT
    
    def test_system_prompt_describes_user_goal_achieved(self):
        """System prompt should explain user_goal_achieved field."""
        assert "user_goal_achieved" in SYSTEM_PROMPT
        assert "true if the user's ORIGINAL request is fully satisfied" in SYSTEM_PROMPT
    
    def test_system_prompt_describes_todo_progress(self):
        """System prompt should explain todo_progress field."""
        assert "todo_progress" in SYSTEM_PROMPT
        assert "CHANGED STATUS this iteration" in SYSTEM_PROMPT
    
    def test_system_prompt_describes_effective_next_goal(self):
        """System prompt should explain effective_next_goal field."""
        assert "effective_next_goal" in SYSTEM_PROMPT
    
    def test_system_prompt_explains_fallback_terminal(self):
        """System prompt should explain that fallback paths are terminal."""
        assert "Fallback Paths are TERMINAL" in SYSTEM_PROMPT
        assert "Do NOT go back to try X after completing Y" in SYSTEM_PROMPT
    
    def test_system_prompt_explains_alternative_means(self):
        """System prompt should explain todo completion by alternative means."""
        assert "ALTERNATIVE MEANS" in SYSTEM_PROMPT
    
    def test_system_prompt_lists_valid_statuses(self):
        """System prompt should list valid todo statuses."""
        assert '"pending"' in SYSTEM_PROMPT
        assert '"in_progress"' in SYSTEM_PROMPT
        assert '"completed"' in SYSTEM_PROMPT
        assert '"skipped"' in SYSTEM_PROMPT
    
    def test_system_prompt_has_progress_example(self):
        """System prompt should include example with progress fields.

        Asserts on JSON-without-spaces (matching the canonical examples in
        ``versions/post_tool/v2/system.txt``) so a future format-with-spaces
        rewrite of the example block surfaces the change explicitly.
        """
        assert '"user_goal_achieved":true' in SYSTEM_PROMPT
        assert '"todo_progress"' in SYSTEM_PROMPT
        assert '"completion_reason"' in SYSTEM_PROMPT
    
    def test_system_prompt_has_finalize_rule(self):
        """System prompt should instruct to finalize when goal achieved."""
        assert 'user_goal_achieved=true' in SYSTEM_PROMPT or '"user_goal_achieved": true' in SYSTEM_PROMPT


# NOTE: ``TestSystemPromptProgressiveReading`` previously asserted that the
# system prompt contained an inline ``## File Reading (When Needed)`` block
# with per-file-type advice (``Logs:``, ``Scan results:``), explicit Python-
# literal read-mode examples (``read_mode="grep"``), and metadata-fields
# guidance (``chars omitted``, ``Output Info section``). The post-tool prompt
# was reorganized in v2 (``versions/post_tool/v2/system.txt``) to a concise
# ``## Artifact Retrieval Policy (CRITICAL)`` section that delegates the
# fine-grained per-file-type and metadata details to runtime context. The
# class was removed because every assertion in it pinned old wording with no
# behavioral contract; runtime-context tests cover the actual contract that
# bounded reads are preferred over full reads.


# -----------------------------------------------------------------------------
# Todo Formatting Tests
# -----------------------------------------------------------------------------


class TestTodoFormattingWithIndices:
    """Tests for todo list formatting with indices."""
    
    def test_todos_include_indices(self, prompt_builder: PostToolReasoningPromptBuilder):
        """Todo items should include indices [0], [1], etc."""
        todos = ["First task", "Second task", "Third task"]
        result = prompt_builder._format_todos(todos)
        
        assert "[0]" in result
        assert "[1]" in result
        assert "[2]" in result
    
    def test_todos_include_status_icons(self, prompt_builder: PostToolReasoningPromptBuilder):
        """Todo items should include status icons."""
        todos = ["Pending task"]
        result = prompt_builder._format_todos(todos)
        
        # Pending tasks get checkbox icon
        assert "☐" in result
    
    def test_todo_items_with_status_icons(
        self, 
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_state_with_todo_items: InteractiveState,
    ):
        """TodoItem objects should show appropriate status icons."""
        todo_list = sample_state_with_todo_items.facts.todo_list
        result = prompt_builder._format_todos(todo_list)
        
        # First item is complete
        assert "✅" in result
        assert "(done)" in result
        
        # Second item is in progress
        assert "▶" in result
        assert "(in progress)" in result
        
        # Third item is pending
        assert "☐" in result
    
    def test_todos_respect_max_limit(self, prompt_builder: PostToolReasoningPromptBuilder):
        """Should only include MAX_TODOS_IN_PROMPT items."""
        # Create more todos than the limit
        todos = [f"Task {i}" for i in range(MAX_TODOS_IN_PROMPT + 5)]
        result = prompt_builder._format_todos(todos)
        
        # Should have index 9 (last allowed) but not index 10+
        assert f"[{MAX_TODOS_IN_PROMPT - 1}]" in result
        assert f"[{MAX_TODOS_IN_PROMPT}]" not in result
    
    def test_empty_todos_returns_empty_string(self, prompt_builder: PostToolReasoningPromptBuilder):
        """Empty todo list should return empty string."""
        result = prompt_builder._format_todos([])
        assert result == ""
    
    def test_string_todos_formatted_correctly(self, prompt_builder: PostToolReasoningPromptBuilder):
        """String todos should be formatted with pending status."""
        todos = ["Discover hosts", "Port scan"]
        result = prompt_builder._format_todos(todos)
        
        assert "[0] ☐ Discover hosts" in result
        assert "[1] ☐ Port scan" in result

    def test_todo_dicts_support_description_and_text(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
    ):
        """Dict todos should support both description and text keys."""
        todos = [
            {"description": "Discover hosts", "status": "in_progress"},
            {"text": "Port scan discovered hosts", "status": "complete_positive"},
        ]
        result = prompt_builder._format_todos(todos)

        assert "[0] ▶ Discover hosts (in progress) [in_progress]" in result
        assert "[1] ✅ Port scan discovered hosts (done) [completed]" in result

    def test_todos_include_explicit_status_markers(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
    ):
        """Todos should always include explicit status markers."""
        todos = [
            {"description": "Pending one", "status": "pending"},
            {"description": "Active one", "status": "in_progress"},
            {"description": "Done one", "status": "complete_negative"},
            {"description": "Skip one", "status": "skipped"},
            "Legacy string todo",
        ]
        result = prompt_builder._format_todos(todos)

        assert "[pending]" in result
        assert "[in_progress]" in result
        assert "[completed]" in result
        assert "[skipped]" in result


# -----------------------------------------------------------------------------
# User Prompt Tests
# -----------------------------------------------------------------------------


class TestUserPromptProgress:
    """Tests for user prompt progress tracking instructions."""
    
    # NOTE: ``test_user_prompt_includes_progress_instruction`` was removed.
    # ``user_goal_achieved`` and ``todo_progress`` instructions were moved
    # from the per-turn user prompt into the static system prompt
    # (``versions/post_tool/v2/system.txt``). The system-prompt level
    # ``test_system_prompt_describes_user_goal_achieved`` /
    # ``test_system_prompt_describes_todo_progress`` checks remain as the
    # contract for "the LLM is told about these fields every turn".

    def test_user_prompt_includes_indexed_todos(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_state_with_todos: InteractiveState,
        sample_synthesized: dict,
    ):
        """User prompt should include todos with indices."""
        user_prompt = prompt_builder.build_user_prompt(
            interactive=sample_state_with_todos,
            synthesized=sample_synthesized,
        )
        
        assert "[0]" in user_prompt
        assert "[1]" in user_prompt
        assert "Discover live hosts" in user_prompt
    
    # NOTE: ``test_user_prompt_mentions_indices_usage`` removed — the
    # "use indices [0], [1], etc." guidance is in the system prompt
    # (line ~73 of versions/post_tool/v2/system.txt). The user prompt
    # only renders the indexed todo list itself; ``test_user_prompt_includes_indexed_todos``
    # above is the structural check that the indices are present.

    # NOTE: ``test_user_prompt_mentions_fallback_completion`` removed — the
    # "Fallback Paths are TERMINAL" guidance lives in the system prompt's
    # Goal Achievement section. ``test_system_prompt_explains_fallback_terminal``
    # already validates the system-side contract.

    def test_user_prompt_includes_todo_section_with_explicit_markers(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_synthesized: dict,
    ):
        """Prompt should include indexed todos with explicit status markers."""
        interactive = {
            "facts": {
                "message": "Continue execution",
                "capability": "deep_reasoning",
                "metadata": {},
                "todo_list": [
                    {"description": "Task A", "status": "pending"},
                    {"text": "Task B", "status": "in_progress"},
                    {"description": "Task C", "status": "complete_positive"},
                    "Legacy task",
                ],
            }
        }
        user_prompt = prompt_builder.build_user_prompt(
            interactive=interactive,
            synthesized=sample_synthesized,
        )

        assert "## Todo List" in user_prompt
        assert "[0]" in user_prompt
        assert "[pending]" in user_prompt
        assert "[in_progress]" in user_prompt
        assert "[completed]" in user_prompt

    def test_user_prompt_omits_todo_stall_section_without_tracking(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_state_with_todo_items: InteractiveState,
        sample_synthesized: dict,
    ):
        """Prompt should not render stall guidance when no stall is tracked."""
        user_prompt = prompt_builder.build_user_prompt(
            interactive=sample_state_with_todo_items,
            synthesized=sample_synthesized,
        )

        assert "## Active Todo Stall Guard" not in user_prompt

    def test_user_prompt_renders_todo_stall_section(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_state_with_todo_items: InteractiveState,
        sample_synthesized: dict,
    ):
        """Prompt should warn PTR when the active todo has stalled."""
        sample_state_with_todo_items.facts.metadata[TODO_STALL_METADATA_KEY] = {
            "index": 1,
            "description": "Port scan hosts",
            "count": 2,
            "threshold": 3,
        }

        user_prompt = prompt_builder.build_user_prompt(
            interactive=sample_state_with_todo_items,
            synthesized=sample_synthesized,
        )

        assert "## Active Todo Stall Guard" in user_prompt
        assert "Active todo [1] `Port scan hosts`" in user_prompt
        assert "2 consecutive no-progress tool phases" in user_prompt
        assert "Prefer reflect or finalize" in user_prompt


# -----------------------------------------------------------------------------
# Integration Tests
# -----------------------------------------------------------------------------


class TestPromptIntegration:
    """Integration tests for the full prompt flow."""
    
    def test_system_prompt_is_valid_string(self, prompt_builder: PostToolReasoningPromptBuilder):
        """System prompt should be a valid non-empty string."""
        system_prompt = prompt_builder.build_system_prompt()
        
        assert isinstance(system_prompt, str)
        assert len(system_prompt) > 100  # Should be substantial
    
    def test_user_prompt_is_valid_string(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_state_with_todos: InteractiveState,
        sample_synthesized: dict,
    ):
        """User prompt should be a valid non-empty string."""
        user_prompt = prompt_builder.build_user_prompt(
            interactive=sample_state_with_todos,
            synthesized=sample_synthesized,
        )
        
        assert isinstance(user_prompt, str)
        assert len(user_prompt) > 100  # Should be substantial
    
    def test_prompts_work_with_empty_todos(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_synthesized: dict,
    ):
        """Prompts should work when todo list is empty."""
        facts = FactsState(
            task_id=123,
            message="Simple scan",
            conversation_id="conv-123",
            capability="deep_reasoning",
            todo_list=[],
            metadata={},
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        # Should not raise
        user_prompt = prompt_builder.build_user_prompt(
            interactive=state,
            synthesized=sample_synthesized,
        )
        
        assert isinstance(user_prompt, str)


# -----------------------------------------------------------------------------
# Artifact Path Tests
# -----------------------------------------------------------------------------


class TestArtifactPathInPrompt:
    """Tests for artifact path inclusion in prompts."""

    # NOTE: ``test_system_prompt_describes_artifact_reading`` was removed.
    # The system prompt's old ``read saved files`` / ``Output Info section``
    # phrasing was reorganized in v2 into ``## Saved Evidence Policy
    # (CRITICAL)`` (``versions/post_tool/v2/system.txt`` ~line 132). The new
    # section asserts the current contract: hidden artifact DB tools are not
    # model-selectable, and visible filesystem reads/searches must stay bounded.

    def test_user_prompt_includes_artifact_path_when_present(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_synthesized: dict,
    ):
        """User prompt should surface the artifact path when available.

        Asserts the actual contract — the LLM sees the path it can read —
        rather than pinning specific wording (the live builder emits
        ``Saved output path: <path>`` instead of ``Full output saved to <path>``).
        """
        facts = FactsState(
            task_id=123,
            message="Scan network",
            conversation_id="conv-123",
            capability="deep_reasoning",
            metadata={
                "last_artifact_path": "artifacts/20250101_tool.txt",
                "last_tool_result": {
                    "was_truncated": True,
                    "chars_truncated": 1200,
                    "suggest_file_reading": True,
                },
            },
        )
        state = InteractiveState(facts=facts, trace=TraceState())

        user_prompt = prompt_builder.build_user_prompt(
            interactive=state,
            synthesized=sample_synthesized,
        )

        # The artifact path itself must be rendered (the contract).
        assert "artifacts/20250101_tool.txt" in user_prompt
        # And there must be a truncation/condensation indicator so the LLM
        # knows the rendered output isn't the full payload.
        assert "Output condensed" in user_prompt
        assert "Saved output path" in user_prompt
    
    def test_user_prompt_omits_artifact_section_when_no_path(
        self,
        prompt_builder: PostToolReasoningPromptBuilder,
        sample_synthesized: dict,
    ):
        """User prompt should not include artifact section when no path."""
        facts = FactsState(
            task_id=123,
            message="Scan network",
            conversation_id="conv-123",
            capability="deep_reasoning",
            metadata={},  # No artifact path
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        user_prompt = prompt_builder.build_user_prompt(
            interactive=state,
            synthesized=sample_synthesized,
        )
        
        # Should not mention full output available
        assert "Full Output Available" not in user_prompt
