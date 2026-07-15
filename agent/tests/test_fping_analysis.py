"""Unit tests for pure fping analysis helpers."""

from __future__ import annotations

from agent.tools.information_gathering.network_discovery.fping_analysis import (
    analyze_fping_metadata,
    analyze_fping_output,
)


def test_fping_analysis_matches_alive_summary_and_diagnostics() -> None:
    stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "       2 targets\n       1 alive\n       1 unreachable\n"
    )

    analysis = analyze_fping_output(stdout="", stderr=stderr, exit_code=1)

    assert analysis.alive_hosts == ("172.17.0.1",)
    assert analysis.alive_count == 1
    assert analysis.unresponsive_count == 1
    assert analysis.diagnostics == (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70",
    )
    assert "Alive hosts: 1" in analysis.compact_output
    assert "Unresponsive hosts: 1" in analysis.compact_output
    assert analysis.semantic_observations[0]["subject_key"] == "host.ip:172.17.0.1"


def test_fping_metadata_analysis_does_not_infer_missing_unresponsive_count() -> None:
    analysis = analyze_fping_metadata(
        {"alive_hosts": ["scanme.example.com", "10.0.0.5"], "exit_code": 1}
    )

    assert analysis.alive_hosts == ("scanme.example.com", "10.0.0.5")
    assert analysis.unresponsive_count is None
    assert "Unresponsive hosts: unknown" in analysis.compact_output
    assert len(analysis.semantic_observations) == 1
    assert analysis.semantic_observations[0]["subject_key"] == "host.ip:10.0.0.5"
