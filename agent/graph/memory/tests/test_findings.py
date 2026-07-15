"""Focused tests for working-memory findings helpers."""

from __future__ import annotations

import inspect

from agent.graph.memory.findings import (
    build_relevant_findings_for_prompt,
    count_known_open_port_findings,
    extract_observed_findings,
    format_findings_for_finalizer,
    merge_available_findings,
    select_relevant_findings,
    select_relevant_findings_for_prompt,
)
from agent.graph.memory.target_resolution import resolve_target_from_working_memory
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_merge_available_findings_prefers_observed_over_candidate_and_caps() -> None:
    existing = [
        {
            "kind": "port_open",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "candidate",
            "confidence": 0.4,
            "seen_at": 10,
            "ttl_seconds": 300,
        }
    ]
    incoming = [
        {
            "kind": "port_open",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 20,
            "ttl_seconds": 600,
        }
    ] + [
        {
            "kind": "host_up",
            "target": f"10.10.10.{idx}",
            "subject": f"10.10.10.{idx}",
            "details": {"status": "up"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": idx,
            "ttl_seconds": 600,
        }
        for idx in range(60)
    ]

    merged = merge_available_findings(existing, incoming)

    assert len(merged) == 50
    replaced = next(item for item in merged if item["subject"] == "10.10.10.5:80/tcp")
    assert replaced["assertion_level"] == "observed"
    assert replaced["confidence"] == 1.0


def test_extract_observed_findings_supports_nmap_style_hosts_and_ports() -> None:
    findings = extract_observed_findings(
        {
            "hosts": [
                {
                    "ip": "10.10.10.5",
                    "status": "up",
                    "ports": [
                        {
                            "port": 80,
                            "protocol": "tcp",
                            "service": "http",
                            "product": "nginx",
                            "version": "1.24",
                        }
                    ],
                }
            ]
        },
        seen_at=100,
    )

    kinds = {(item["kind"], item["subject"]) for item in findings}
    assert ("host_up", "10.10.10.5") in kinds
    assert ("port_open", "10.10.10.5:80/tcp") in kinds
    assert ("service_detected", "10.10.10.5:80/tcp") in kinds


def test_extract_observed_findings_supports_masscan_and_unicornscan_shapes() -> None:
    masscan_findings = extract_observed_findings(
        {
            "hosts": [{"ip": "10.10.10.7", "ports_count": 1}],
            "open_ports": [{"port": 443, "protocol": "tcp", "status": "open", "service": "https"}],
        },
        target_hint="10.10.10.7",
        seen_at=200,
    )
    unicornscan_findings = extract_observed_findings(
        {
            "open_ports": [
                {"ip": "10.10.10.8", "port": 22, "protocol": "tcp", "status": "open"},
            ]
        },
        seen_at=300,
    )

    assert any(item["subject"] == "10.10.10.7:443/tcp" for item in masscan_findings)
    assert any(item["kind"] == "service_detected" for item in masscan_findings)
    assert any(item["subject"] == "10.10.10.8:22/tcp" for item in unicornscan_findings)


def test_extract_observed_findings_normalizes_url_target_hint_to_host() -> None:
    findings = extract_observed_findings(
        {
            "host_status": "up",
            "open_ports": [
                {"port": 443, "protocol": "tcp", "status": "open", "service": "https"},
            ],
        },
        target_hint="https://10.10.10.5/admin",
        seen_at=400,
    )

    assert any(item["target"] == "10.10.10.5" for item in findings)
    assert any(item["subject"] == "10.10.10.5:443/tcp" for item in findings)


def test_select_relevant_findings_filters_by_target_and_derives_state() -> None:
    findings = [
        {
            "kind": "service_detected",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_000,
            "ttl_seconds": 600,
        },
        {
            "kind": "port_open",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:22/tcp",
            "details": {},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 10,
            "ttl_seconds": 10,
        },
        {
            "kind": "finding.vulnerability",
            "target": "10.10.10.5",
            "subject": "http/nginx",
            "details": {"rationale": "Possible nginx issue"},
            "assertion_level": "candidate",
            "confidence": 0.6,
            "seen_at": 1_100,
            "ttl_seconds": 300,
        },
        {
            "kind": "port_open",
            "target": "10.10.10.6",
            "subject": "10.10.10.6:80/tcp",
            "details": {},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_100,
            "ttl_seconds": 600,
        },
    ]

    selected = select_relevant_findings(
        findings,
        target="10.10.10.5",
        subject_hint="http",
        limit=8,
        now_ts=1_200,
    )

    assert [item["target"] for item in selected] == ["10.10.10.5", "10.10.10.5", "10.10.10.5"]
    assert selected[0]["state"] == "fresh"
    assert selected[0]["kind"] == "service_detected"
    assert selected[1]["state"] == "candidate"
    assert selected[2]["state"] == "stale"
    assert count_known_open_port_findings(findings, target="10.10.10.5", now_ts=1_200) == 0


def test_relevant_findings_match_host_findings_when_target_is_url() -> None:
    findings = [
        {
            "kind": "service_detected",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_100,
            "ttl_seconds": 600,
        },
        {
            "kind": "port_open",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_100,
            "ttl_seconds": 600,
        },
    ]

    selected = select_relevant_findings(
        findings,
        target="http://10.10.10.5/login",
        subject_hint="http",
        limit=8,
        now_ts=1_200,
    )

    assert len(selected) == 2
    assert all(item["target"] == "10.10.10.5" for item in selected)
    assert count_known_open_port_findings(
        findings,
        target="http://10.10.10.5/login",
        now_ts=1_200,
    ) == 1


def test_select_relevant_findings_for_prompt_matches_manual_pipeline() -> None:
    findings = [
        {
            "kind": "service_detected",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:80/tcp",
            "details": {"service": "http", "product": "nginx"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_100,
            "ttl_seconds": 600,
        },
        {
            "kind": "port_open",
            "target": "10.10.10.5",
            "subject": "10.10.10.5:22/tcp",
            "details": {},
            "assertion_level": "candidate",
            "confidence": 0.6,
            "seen_at": 1_100,
            "ttl_seconds": 300,
        },
    ]
    components = (
        "Enumerate HTTP service details",
        "check nginx",
        "http",
    )

    manual = select_relevant_findings(
        findings,
        target="10.10.10.5",
        subject_hint="Enumerate HTTP service details check nginx http",
        limit=8,
    )
    via_helper = select_relevant_findings_for_prompt(
        available_findings=findings,
        target="10.10.10.5",
        subject_hint_components=components,
        limit=8,
    )

    assert via_helper == manual


def test_select_relevant_findings_for_prompt_signature_is_stable() -> None:
    signature = inspect.signature(select_relevant_findings_for_prompt)
    params = tuple(signature.parameters.values())

    assert all(param.kind is inspect.Parameter.KEYWORD_ONLY for param in params)
    assert [param.name for param in params] == [
        "available_findings",
        "target",
        "subject_hint_components",
        "limit",
    ]


def test_format_findings_for_finalizer_renders_rich_candidate_details() -> None:
    rendered = format_findings_for_finalizer(
        [
            {
                "kind": "finding.vulnerability_candidate",
                "target": "10.10.10.5:443",
                "subject": "10.10.10.5",
                "assertion_level": "candidate",
                "confidence": 0.35,
                "details": {
                    "attributes": {
                        "service": "nginx",
                        "paths": ["/", "/admin"],
                    },
                    "rationale": "Potential unauthenticated administrative exposure.",
                    "evidence_refs": ["artifact://tool-output-1"],
                    "vulnerability": "AUTHZ-CANDIDATE-EXPOSED-ENDPOINTS",
                    "vulnerability_confidence": 0.35,
                },
            }
        ]
    )

    assert "- [candidate confidence=0.35] finding.vulnerability_candidate @ 10.10.10.5 (target=10.10.10.5:443)" in rendered
    assert "Attributes:" in rendered
    assert "rationale: Potential unauthenticated administrative exposure.".lower() in rendered.lower()
    assert "Evidence:" in rendered
    assert "Vulnerability hypothesis: AUTHZ-CANDIDATE-EXPOSED-ENDPOINTS (confidence=0.35)" in rendered


def test_build_relevant_findings_for_prompt_matches_manual_pipeline() -> None:
    working_memory = {
        "referents": {"intent:target": "10.10.10.5"},
        "available_findings": [
            {
                "kind": "service_detected",
                "target": "10.10.10.5",
                "subject": "10.10.10.5:443/tcp",
                "details": {"service": "https"},
                "assertion_level": "observed",
                "confidence": 1.0,
                "seen_at": 1_200,
                "ttl_seconds": 600,
            },
            {
                "kind": "finding.vulnerability_candidate",
                "target": "10.10.10.5",
                "subject": "10.10.10.5",
                "details": {"rationale": "Possible weak TLS configuration."},
                "assertion_level": "candidate",
                "confidence": 0.6,
                "seen_at": 1_300,
                "ttl_seconds": 300,
            },
        ],
    }
    interactive = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Assess TLS exposure",
            current_goal="Validate TLS exposure",
            selected_tool="information_gathering.network_discovery.nmap",
            tool_parameters={
                "information_gathering.network_discovery.nmap": {
                    "target": "10.10.10.5",
                    "ports": "443",
                }
            },
            metadata={
                "working_memory": working_memory,
                "next_tool_hint": "inspect tls service details",
                "tool_intent": {"focus": "tls exposure"},
            },
        ),
        trace=TraceState(),
    )

    actual = build_relevant_findings_for_prompt(interactive)
    resolved_target = resolve_target_from_working_memory(
        working_memory,
        intent_referent_key="intent:target",
        recent_turn_limit=4,
    )
    expected = select_relevant_findings_for_prompt(
        available_findings=working_memory["available_findings"],
        target=resolved_target,
        subject_hint_components=(
            interactive.facts.current_goal,
            interactive.facts.metadata.get("next_tool_hint"),
            interactive.facts.tool_parameters[
                "information_gathering.network_discovery.nmap"
            ],
            interactive.facts.metadata["tool_intent"]["focus"],
        ),
        limit=8,
    )

    assert actual == expected
