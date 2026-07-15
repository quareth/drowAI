"""Layer 4 replay signatures for deterministic LangGraph regression checks."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.tests.langgraph_regression.harness import RegressionHarness, SignatureResult
from backend.tests.langgraph_regression.scenarios import BASELINE_SCENARIOS, RegressionScenario

pytestmark = [
    pytest.mark.regression_layer4,
    pytest.mark.regression_main,
    pytest.mark.regression_nightly,
]

GOLDEN_PATH = (
    Path(__file__).resolve().parent / "golden" / "replay_signatures_v1.json"
)
UPDATE_ENV_VAR = "DROWAI_UPDATE_REGRESSION_GOLDENS"


def _signature_for_scenario(
    scenario: RegressionScenario,
    harness: RegressionHarness,
) -> SignatureResult:
    if scenario.category == "branch":
        return harness.signature(
            branch=scenario.expected_branch,
            node_path_signature=f"facade->{scenario.expected_branch}->result",
            key_decisions=["branch_selected"],
            terminal_status=scenario.expected_terminal_status,
            event_type_sequence=scenario.expected_event_types or ("assistant_final",),
            interrupted=scenario.expected_interrupt,
        )

    if scenario.category == "history":
        key = "history_empty" if len(scenario.history) == 0 else "history_present"
        return harness.signature(
            branch="normal_chat",
            node_path_signature="metadata->planner_prompt->history",
            key_decisions=[key],
            terminal_status="completed",
            event_type_sequence=("assistant_final",),
            interrupted=False,
        )

    if scenario.category == "tool":
        decisions = list(scenario.metadata.get("decision_history", ()))
        simple_route = harness.route_simple_tool_decision(
            decision_history=decisions,
            metadata=scenario.metadata,
        )
        deep_route = harness.route_deep_reasoning_decision(
            decision_history=decisions,
            metadata=scenario.metadata,
        )
        events = (
            ("assistant_final",)
            if simple_route == "format_results"
            else ("tool_start", "tool_delta", "tool_end", "assistant_final")
        )
        return harness.signature(
            branch="simple_tool_execution",
            node_path_signature=f"simple_tool:{simple_route}|deep:{deep_route}",
            key_decisions=[scenario.expected_key_decision],
            terminal_status="completed",
            event_type_sequence=events,
            interrupted=False,
        )

    conversation_id = scenario.metadata.get("conversation_id")
    anchor_sequence = scenario.metadata.get("anchor_sequence")
    thread_config = harness.make_thread_config(
        conversation_id=conversation_id,
        anchor_sequence=anchor_sequence,
    )
    configurable = thread_config["configurable"]
    action = str(scenario.metadata.get("resume_action", "approve"))
    has_checkpoint = "checkpoint_id" in configurable
    events = (
        ("interrupt_pending", "resume_complete")
        if scenario.expected_interrupt
        else ("resume_complete",)
    )
    return harness.signature(
        branch=scenario.expected_branch,
        node_path_signature=f"hitl:{action}:{configurable['thread_id']}",
        key_decisions=[action, "checkpoint" if has_checkpoint else "no_checkpoint"],
        terminal_status=scenario.expected_terminal_status,
        event_type_sequence=events,
        interrupted=scenario.expected_interrupt,
    )


def _build_actual_signatures(harness: RegressionHarness) -> Dict[str, Dict[str, Any]]:
    signatures: Dict[str, Dict[str, Any]] = {}
    for scenario in BASELINE_SCENARIOS:
        signatures[scenario.scenario_id] = asdict(
            _signature_for_scenario(scenario, harness)
        )
    return dict(sorted(signatures.items()))


def test_replay_signature_golden_matches_baseline(regression_harness) -> None:
    actual = _build_actual_signatures(regression_harness)
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    if os.getenv(UPDATE_ENV_VAR) == "1":
        GOLDEN_PATH.write_text(
            json.dumps(actual, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"Updated golden signatures via {UPDATE_ENV_VAR}=1")

    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual == expected


def test_replay_signature_coverage_matches_scenario_catalog(regression_harness) -> None:
    signatures = _build_actual_signatures(regression_harness)
    scenario_ids = {scenario.scenario_id for scenario in BASELINE_SCENARIOS}
    assert set(signatures.keys()) == scenario_ids
    assert len(signatures) == 12

