"""Tests for PTR iteration-memory ledger continuity.

Purpose
-------
Validates that the post-tool reasoning observation recorder appends one
ordered PTR phase record to the shared current-turn phase ledger under
``metadata["working_memory"]["current_turn_phases"]`` from validated
``PostToolReasoningOutput`` fields, while keeping the legacy prose
trace/synthesized-output compatibility writes intact.

Scope
-----
This file covers only the ledger append contract owned by
``record_observation`` for PTR records (source="ptr") and the immediately
related PTR candidate-decision phase binding. Tool records, prompt rendering,
and PTR-node context injection are covered elsewhere.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.graph.nodes.decision_router.router import decision_router
from agent.graph.nodes.post_tool_reasoning.models import (
    CandidateAttribute,
    CandidateEvidenceRef,
    CandidateObservation,
    PostToolReasoningOutput,
    TodoProgress,
    ToolIntent,
)
from agent.graph.nodes.post_tool_reasoning.recorders import (
    record_decision,
    record_observation,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.iteration_memory import get_current_turn_scope, get_ledger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(metadata: Dict[str, Any] | None = None) -> InteractiveState:
    """Return a minimal InteractiveState with the provided metadata."""
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


def _make_candidate(position: int = 0) -> CandidateObservation:
    """Return one compact candidate observation for section rendering."""
    return CandidateObservation(
        observation_type="service.banner",
        subject_type="tcp_service",
        subject_key_hint=f"10.0.0.1:{80 + position}/tcp",
        assertion_level="candidate",
        confidence=0.71,
        attributes=[
            CandidateAttribute(key="service", value="http"),
            CandidateAttribute(key="product", value="nginx"),
        ],
        rationale="Nmap returned an HTTP-like banner on the target port.",
        evidence_refs=[
            CandidateEvidenceRef(
                excerpt="80/tcp open http nginx",
                source_artifact_id=f"artifact-{position}",
            )
        ],
    )


def _make_output(
    *,
    observation: str = (
        "The latest probe found an HTTP service on the target. "
        "This supports moving from discovery into service enumeration."
    ),
    next_action: str = "think_more",
    action_reasoning: str = "The tool result needs one reasoning pass.",
    tool_intent: ToolIntent | None = None,
    user_goal_achieved: bool = False,
    todo_progress: list[TodoProgress] | None = None,
    effective_next_goal: str | None = None,
    failure_detected: bool = False,
    failure_category: str | None = None,
    retry_suggested: bool = False,
    candidate_observations: list[CandidateObservation] | None = None,
) -> PostToolReasoningOutput:
    """Build a normal PTR output with no LLM-authored phase field."""
    return PostToolReasoningOutput(
        observation=observation,
        next_action=next_action,  # type: ignore[arg-type]
        action_reasoning=action_reasoning,
        tool_intent=tool_intent,
        user_goal_achieved=user_goal_achieved,
        todo_progress=todo_progress or [],
        effective_next_goal=effective_next_goal,
        failure_detected=failure_detected,
        failure_category=failure_category,  # type: ignore[arg-type]
        retry_suggested=retry_suggested,
        candidate_observations=candidate_observations,
    )


def _section_map(record: dict[str, Any]) -> dict[str, str]:
    return {
        section["heading"]: section["body"]
        for section in record.get("sections", [])
        if isinstance(section, dict)
    }


# ---------------------------------------------------------------------------
# Dual-write: ledger append + compat writes
# ---------------------------------------------------------------------------


class TestPtrLedgerAppend:
    """``record_observation`` appends a PTR ledger record from output fields."""

    def test_appends_ptr_record_without_llm_phase_field(self) -> None:
        state = _make_state({"turn_sequence": 12})
        output = _make_output()
        assert not hasattr(output, "phase_memory")

        record_observation(state, output)

        ledger = get_ledger(state.facts.metadata)
        assert len(ledger) == 1
        record = ledger[0]
        assert record["source"] == "ptr"
        assert record["turn_sequence"] == 12
        assert record["phase_sequence"] == 0
        assert [section["heading"] for section in record["sections"]] == [
            "PTR Decision",
            "Action Reasoning",
            "Todo Progress",
            "Observation",
        ]

    def test_sections_are_rendered_from_actual_output_fields(self) -> None:
        state = _make_state({"turn_sequence": 12})
        output = _make_output(
            next_action="call_tool",
            action_reasoning=(
                "The failed run used invalid parameters, so a corrected "
                "tool call should retry the same objective."
            ),
            tool_intent=ToolIntent(
                description="Run version detection against the HTTP service.",
                target="10.0.0.1:80",
                focus="http version and headers",
            ),
            todo_progress=[
                TodoProgress(
                    index=1,
                    status="completed",
                    completion_type="positive",
                    completion_reason="HTTP service confirmed on port 80.",
                )
            ],
            effective_next_goal="Enumerate the HTTP service for version evidence.",
            failure_detected=True,
            failure_category="invalid_params",
            retry_suggested=True,
            candidate_observations=[_make_candidate()],
        )

        record_observation(state, output)

        sections = _section_map(get_ledger(state.facts.metadata)[0])
        assert sections["PTR Decision"] == (
            "next_action: call_tool\n"
            "user_goal_achieved: false\n"
            "failure_detected: true\n"
            "failure_category: invalid_params\n"
            "retry_suggested: true"
        )
        assert sections["Action Reasoning"] == output.action_reasoning
        assert sections["Tool Intent"] == (
            "description: Run version detection against the HTTP service.\n"
            "target: 10.0.0.1:80\n"
            "focus: http version and headers"
        )
        assert sections["Effective Next Goal"] == (
            "Enumerate the HTTP service for version evidence."
        )
        assert sections["Todo Progress"] == (
            "todo[1].status: completed\n"
            "todo[1].completion_type: positive\n"
            "todo[1].completion_reason: HTTP service confirmed on port 80."
        )
        assert sections["Observation"] == output.observation
        assert "candidate[0].observation_type: service.banner" in (
            sections["Candidate Observations"]
        )
        assert "candidate[0].attributes: service=http; product=nginx" in (
            sections["Candidate Observations"]
        )
        rendered_text = "\n".join(sections.values())
        assert "{" not in rendered_text
        assert "}" not in rendered_text
        assert '"next_action"' not in rendered_text

    def test_identity_fields_are_runtime_stamped_not_supplied_by_ptr(self) -> None:
        """turn_sequence and phase_sequence must come from runtime metadata."""
        state = _make_state({"turn_sequence": 7})

        record_observation(state, _make_output())

        metadata = state.facts.metadata
        ledger = get_ledger(metadata)
        record = ledger[0]

        assert record["turn_sequence"] == 7
        assert record["phase_sequence"] == 0
        assert record["source"] == "ptr"
        assert _counter(metadata) == 1
        assert get_current_turn_scope(metadata) == 7

    def test_monotonic_phase_sequence_across_consecutive_ptr_appends(self) -> None:
        state = _make_state({"turn_sequence": 3})

        record_observation(
            state,
            _make_output(
                next_action="think_more",
                action_reasoning="First PTR pass needs local reasoning.",
            ),
        )
        record_observation(
            state,
            _make_output(
                next_action="call_tool",
                action_reasoning="Second PTR pass needs another tool.",
                tool_intent=ToolIntent(
                    description="Probe the HTTP service.",
                    target="10.0.0.1:80",
                    focus="service version",
                ),
            ),
        )
        record_observation(
            state,
            _make_output(
                next_action="finalize",
                action_reasoning="Third PTR pass has enough evidence.",
                user_goal_achieved=True,
            ),
        )

        ledger = get_ledger(state.facts.metadata)
        assert [r["phase_sequence"] for r in ledger] == [0, 1, 2]
        assert [r["source"] for r in ledger] == ["ptr", "ptr", "ptr"]
        assert [
            _section_map(r)["PTR Decision"].splitlines()[0]
            for r in ledger
        ] == [
            "next_action: think_more",
            "next_action: call_tool",
            "next_action: finalize",
        ]

    def test_omits_candidate_observations_when_not_compact(self) -> None:
        state = _make_state({"turn_sequence": 10})

        record_observation(
            state,
            _make_output(
                candidate_observations=[
                    _make_candidate(0),
                    _make_candidate(1),
                    _make_candidate(2),
                    _make_candidate(3),
                ]
            ),
        )

        headings = [
            section["heading"]
            for section in get_ledger(state.facts.metadata)[0]["sections"]
        ]
        assert "Candidate Observations" not in headings


# ---------------------------------------------------------------------------
# No-op conditions
# ---------------------------------------------------------------------------


class TestPtrLedgerAppendNoop:
    """Append is a no-op when runtime identity is unavailable."""

    def test_no_append_when_turn_sequence_missing(self) -> None:
        """Runtime identity is mandatory; absent turn_sequence => skip append."""
        state = _make_state({})  # no turn_sequence in metadata

        record_observation(state, _make_output())

        assert get_ledger(state.facts.metadata) == []
        assert len(state.trace.observations) == 1

    def test_no_append_when_turn_sequence_not_int(self) -> None:
        state = _make_state({"turn_sequence": "not-an-int"})

        record_observation(state, _make_output())

        assert get_ledger(state.facts.metadata) == []


# ---------------------------------------------------------------------------
# Candidate decision binding
# ---------------------------------------------------------------------------


class TestPtrCandidateDecisionBinding:
    """The router candidate binds to the PTR phase just appended."""

    @pytest.mark.asyncio
    async def test_candidate_uses_current_ptr_phase_and_router_accepts(self) -> None:
        state = _make_state(
            {
                "turn_sequence": 7,
                "runtime_budgets": {
                    "remaining_iterations": 8,
                    "remaining_tool_calls": 4,
                },
            }
        )
        state.facts.iterations = 2
        output = _make_output(
            next_action="call_tool",
            action_reasoning="Need one more scan before final answer.",
            tool_intent=ToolIntent(
                description="Run a follow-up HTTP probe.",
                target="10.0.0.1:80",
                focus="headers",
            ),
        )

        record_observation(state, output)
        record_decision(state, output)

        metadata = state.facts.metadata
        candidate = metadata["candidate_decision"]
        assert candidate["turn_sequence"] == 7
        assert candidate["phase_sequence"] == 0
        assert candidate["candidate_id"].startswith("ptr-7-0-")

        routed = await decision_router(state.as_graph_state())
        outcome = routed["facts"]["metadata"]["router_outcome"]
        assert outcome["action"] == "call_tool"
        assert outcome["reason"] == "candidate_decision_accepted"
        assert outcome["resolution_source"] == "candidate"


# ---------------------------------------------------------------------------
# Compatibility writes continue unchanged
# ---------------------------------------------------------------------------


class TestCompatibilityWritesPreserved:
    """The existing prose-history surfaces must remain populated."""

    def test_trace_observations_receives_full_text(self) -> None:
        state = _make_state({"turn_sequence": 2})
        output = _make_output()

        record_observation(state, output)

        assert state.trace.observations == [output.observation]

    def test_synthesized_output_observation_text_is_mirrored(self) -> None:
        state = _make_state({"turn_sequence": 2})
        output = _make_output()

        record_observation(state, output)

        synthesized = state.facts.metadata.get("synthesized_output")
        assert isinstance(synthesized, dict)
        assert synthesized.get("observation_text") == output.observation


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    pytest.main([__file__, "-v"])
