"""Tests for deterministic subagent ownership routing policy."""

from __future__ import annotations

from dataclasses import replace

from backend.services.agent_runs.ownership_policy import (
    resolve_subagent_handoff,
)
from backend.services.agent_runs.subagent_registry import (
    SubagentRegistry,
    get_subagent_registry,
)


def _metadata(
    *,
    raw_capabilities: list[str],
    targets: list[str] | None = None,
    classifier_label: str = "direct_executor",
    agent_handoffs: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "intent_classifier_label": classifier_label,
        "intent_classifier_raw_response": {
            "suggested_capabilities": raw_capabilities,
            "agent_handoffs": agent_handoffs
            if agent_handoffs is not None
            else [
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Map exposed services on the approved target.",
                }
            ],
        },
        "intent_hints": {
            "classifier_label": classifier_label,
            "targets": targets if targets is not None else ["10.0.0.10"],
        },
    }


def test_policy_accepts_direct_executor_recon_capabilities_with_targets() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["host discovery", "port scanning", "service enum"])
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.agent_id == "pathfinder"
    assert decision.capabilities == (
        "host_discovery",
        "port_scanning",
        "service_enumeration",
    )
    assert decision.targets == ("10.0.0.10",)
    assert decision.objective == "Map exposed services on the approved target."


def test_generic_policy_resolves_name_and_dispatch_from_injected_registry() -> None:
    probe = replace(
        get_subagent_registry().require("pathfinder"),
        name="probe",
        agent_id="probe",
        display_name="Probe",
    )
    metadata = _metadata(
        raw_capabilities=["port_scan"],
        agent_handoffs=[
            {
                "agent_handoff": "required",
                "subagent": "probe",
                "objective": "Probe the approved target.",
            }
        ],
    )

    decision = resolve_subagent_handoff(
        metadata,
        registry=SubagentRegistry((probe,)),
    )

    assert decision.should_delegate is True
    assert decision.agent_id == "probe"
    assert decision.agent_kind == "recon"
    assert decision.dispatch_branch == "subagent"
    assert decision.reason == "probe_owned"


def test_policy_accepts_classifier_network_scanning_vocabulary() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scanning", "service_discovery"],
            targets=["127.0.0.1"],
        )
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning", "service_enumeration")
    assert decision.targets == ("127.0.0.1",)


def test_policy_accepts_canonical_intent_classifier_network_scan_capability() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scan"],
            targets=["127.0.0.1"],
        )
    )

    assert decision.should_delegate is True
    assert decision.capabilities == ("port_scanning",)


def test_policy_accepts_port_enumeration_from_live_classifier_vocabulary() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scanning", "port_enumeration"],
            targets=["127.0.0.1"],
        ),
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)
    assert decision.targets == ("127.0.0.1",)


def test_policy_ignores_generic_tool_route_hint_alongside_recon_capability() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["tool_call", "network_scan"],
            targets=["127.0.0.1:5432"],
        )
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)


def test_policy_routes_explicit_handoff_when_capabilities_are_empty() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=[], targets=["127.0.0.1:5432"]),
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ()


def test_policy_keeps_capabilities_advisory_for_explicit_handoff() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port scanning", "report"])
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)


def test_policy_rejects_missing_handoff_and_unbounded_scope() -> None:
    missing_handoff = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], agent_handoffs=[])
    )
    missing_target = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], targets=[])
    )

    assert missing_handoff.should_delegate is False
    assert missing_handoff.reason == "missing_agent_handoff"
    assert missing_target.should_delegate is False
    assert missing_target.reason == "invalid_assignment_scope"


def test_policy_rejects_unsupported_or_multiple_required_handoffs() -> None:
    unsupported = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=[],
            agent_handoffs=[
                {
                    "agent_handoff": "required",
                    "subagent": "exploit",
                    "objective": "Exploit the target.",
                }
            ],
        )
    )
    multiple = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["port_scan"],
            agent_handoffs=[
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Scan the first target.",
                },
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Scan the second target.",
                },
            ],
        )
    )

    assert unsupported.should_delegate is False
    assert unsupported.reason == "unsupported_agent_handoff"
    assert multiple.should_delegate is False
    assert multiple.reason == "unsupported_handoff_cardinality"


def test_policy_rejects_non_direct_executor_and_active_local_run() -> None:
    non_direct = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], classifier_label="plan_executor")
    )
    active = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"]),
        active_runs_by_agent_id={"pathfinder": 1},
    )

    assert non_direct.should_delegate is False
    assert non_direct.reason == "classifier_not_direct_executor"
    assert active.should_delegate is False
    assert active.reason == "subagent_unavailable"
