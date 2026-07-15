"""Tests for generic CLI execution outcome resolution."""

from agent.tools.execution_outcome import (
    detect_hard_cli_failure,
    resolve_execution_success,
)


def test_detect_hard_cli_failure_matches_common_error_lines():
    assert detect_hard_cli_failure(stderr="fping: can't parse address 172.0.0.0/24\n")
    assert detect_hard_cli_failure(stderr="nmap: invalid argument\n")
    assert detect_hard_cli_failure(stdout="command not found: foo\n")
    assert detect_hard_cli_failure(stdout="bash: line 1: fping: command not found\n")
    assert detect_hard_cli_failure(stdout="/bin/sh: 1: fping: not found\n")


def test_detect_hard_cli_failure_ignores_normal_scan_output():
    assert not detect_hard_cli_failure(stdout="172.17.0.2 is unreachable\n")
    assert not detect_hard_cli_failure(stdout="", stderr="")


def test_resolve_execution_success_exit_zero():
    assert resolve_execution_success(exit_code=0, informational_exit_codes=frozenset({1}))


def test_resolve_execution_success_informational_exit_code():
    assert resolve_execution_success(
        exit_code=1,
        informational_exit_codes=frozenset({1}),
        stdout="172.17.0.2 is unreachable\n",
    )


def test_resolve_execution_success_hard_failure_overrides_informational_code():
    assert not resolve_execution_success(
        exit_code=1,
        informational_exit_codes=frozenset({1}),
        stderr="fping: can't parse address 172.0.0.0/24\n",
    )
    assert not resolve_execution_success(
        exit_code=127,
        informational_exit_codes=frozenset({127}),
    )


def test_resolve_execution_success_metadata_override():
    assert resolve_execution_success(
        exit_code=99,
        informational_exit_codes=frozenset(),
        parsed_metadata={"execution_outcome": "succeeded"},
    )
    assert not resolve_execution_success(
        exit_code=0,
        informational_exit_codes=frozenset(),
        parsed_metadata={"execution_outcome": "failed"},
    )
