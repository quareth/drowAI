"""Focused command, collector, and parser contracts for OWASP Amass v5."""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.tool_runtime.command_preparation import prepare_tool_command
from agent.tools.canonical_capture import CanonicalCaptureFormat, CaptureFamily
from agent.tools.information_gathering.dns.amass import AmassArgs, AmassTool, Mode
from agent.tools.information_gathering.dns.amass_analysis import (
    AMASS_NAMES_BEGIN,
    AMASS_NAMES_END,
    AMASS_RESOLVED_BEGIN,
    AMASS_RESOLVED_END,
    parse_amass_v5_results,
)
from agent.tools.information_gathering.dns.amass_runtime import (
    AMASS_BEFORE_NAMES_BEGIN,
    AMASS_BEFORE_NAMES_END,
    AMASS_BEFORE_RESOLVED_BEGIN,
    AMASS_BEFORE_RESOLVED_END,
    AMASS_OUTPUT_RELATIVE_DIR,
    AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS,
    AMASS_STATUS_BEGIN,
    AMASS_STATUS_END,
    build_amass_collector_command,
    build_amass_timeout_budget,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "outputs"


def test_passive_mode_uses_graph_free_v5_collector_without_deprecated_flag() -> None:
    command = AmassTool().build_command(
        AmassArgs(
            target="Example.COM.",
            mode=Mode.PASSIVE,
            inactivity_timeout_minutes=7,
        )
    )

    assert command[:4] == [
        "bash",
        "/workspace/.drowai/amass/collect_v5.sh",
        "/workspace",
        "example.com",
    ]
    assert command[4:8] == [
        str(build_amass_timeout_budget(600).lock_wait_seconds),
        str(build_amass_timeout_budget(600).enum_deadline_seconds),
        str(build_amass_timeout_budget(600).query_grace_seconds),
        str(build_amass_timeout_budget(600).force_kill_grace_seconds),
    ]
    assert "-passive" not in command
    assert command[command.index("-timeout") + 1] == "7"
    assert command[-1] == "-nocolor"


def test_active_and_brute_modes_use_native_v5_flags() -> None:
    active = AmassTool().build_command(
        AmassArgs(target="example.com", mode=Mode.ACTIVE)
    )
    brute = AmassTool().build_command(
        AmassArgs(target="example.com", mode=Mode.BRUTE, wordlist="/tmp/names.txt")
    )

    assert "-active" in active
    assert "-brute" in brute
    assert brute[brute.index("-w") + 1] == "/tmp/names.txt"


def test_resolver_and_source_filters_use_v5_flag_names() -> None:
    command = AmassTool().build_command(
        AmassArgs(
            target="example.com",
            dns_server="127.0.0.1",
            source=["crtsh", "dns"],
            exclude_source=["archive"],
        )
    )

    assert command[command.index("-r") + 1] == "127.0.0.1"
    assert command[command.index("-include") + 1] == "crtsh,dns"
    assert command[command.index("-exclude") + 1] == "archive"


def test_collector_command_prepares_flags_in_installed_v5_order(tmp_path) -> None:
    wordlist = tmp_path / "word list.txt"
    args = AmassArgs(
        target="example.com",
        mode=Mode.BRUTE,
        wordlist=str(wordlist),
        inactivity_timeout_minutes=11,
        verbose=True,
        dns_server="127.0.0.1",
        source=["crtsh", "dns"],
        exclude_source=["archive"],
    )

    command = AmassTool().build_command(args)

    budget = build_amass_timeout_budget(args.execution_timeout)
    assert command == [
        "bash",
        "/workspace/.drowai/amass/collect_v5.sh",
        "/workspace",
        "example.com",
        str(budget.lock_wait_seconds),
        str(budget.enum_deadline_seconds),
        str(budget.query_grace_seconds),
        str(budget.force_kill_grace_seconds),
        "-brute",
        "-w",
        str(wordlist),
        "-timeout",
        "11",
        "-v",
        "-r",
        "127.0.0.1",
        "-include",
        "crtsh,dns",
        "-exclude",
        "archive",
        "-nocolor",
    ]


def test_execution_timeout_is_separate_from_amass_inactivity_timeout() -> None:
    args = AmassArgs(
        target="example.com",
        execution_timeout=61,
        inactivity_timeout_minutes=12,
    )
    command = AmassTool().build_command(args)

    assert args.execution_timeout == 61
    assert command[command.index("-timeout") + 1] == "12"


@pytest.mark.parametrize("target", ["127.0.0.1", "example.com,example.org", "not a domain"])
def test_target_requires_one_root_domain(target: str) -> None:
    with pytest.raises(ValidationError):
        AmassArgs(target=target)


def test_only_supported_v5_domain_enumeration_modes_are_exposed() -> None:
    assert {mode.value for mode in Mode} == {"passive", "active", "brute"}


def test_verbose_and_quiet_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        AmassArgs(target="example.com", verbose=True, quiet=True)


def test_workspace_collector_uses_one_task_scoped_output_directory() -> None:
    tool = AmassTool()
    args = AmassArgs(target="example.com")
    prepared_files = tool.prepare_workspace_files(args)
    prepared_directories = tool.prepare_workspace_directories(args)

    assert [item.relative_path for item in prepared_directories] == [
        ".drowai/amass",
        ".drowai/amass/xdg-config",
        ".drowai/amass/xdg-config/amass",
        ".drowai/amass/xdg-data",
        ".drowai/amass/xdg-cache",
        ".drowai/amass/runs",
    ]
    assert [item.relative_path for item in prepared_files] == [
        ".drowai/amass/collect_v5.sh"
    ]

    script = prepared_files[0].content_bytes().decode("utf-8")
    assert 'amass enum -dir "$output_dir"' in script
    assert script.count('amass subs -dir "$output_dir"') == 1
    assert 'output_dir="$xdg_config_home/amass"' in script
    assert 'asset_db="$output_dir/asset.db"' in script
    assert 'XDG_CONFIG_HOME="$xdg_config_home"' in script
    assert 'lock_path="$runtime_dir/workflow.lock"' in script
    assert 'engine_port="${DROWAI_AMASS_ENGINE_PORT:-4000}"' in script
    assert "mktemp" not in script
    assert "session." not in script


def test_collector_engine_default_db_path_matches_subs_dir_contract() -> None:
    script = AmassTool().prepare_workspace_files(AmassArgs(target="example.com"))[
        0
    ].content_bytes().decode("utf-8")

    assert 'xdg_config_home="$runtime_dir/xdg-config"' in script
    assert 'output_dir="$xdg_config_home/amass"' in script
    assert 'asset_db="$output_dir/asset.db"' in script
    assert 'amass enum -dir "$output_dir"' in script
    assert 'amass subs -dir "$output_dir"' in script


@pytest.mark.asyncio
async def test_command_preparation_carries_collector_workspace_materialization(
    tmp_path,
) -> None:
    config = SimpleNamespace(task_id=1, tenant_id=7, workspace_path=str(tmp_path))

    prepared = await prepare_tool_command(
        tool_id="information_gathering.dns.amass",
        parameters={"target": "example.com", "execution_timeout": 75},
        config=config,
        transport="file-comm",
        explicit_command_builder=lambda _tool_id, _parameters: "",
    )

    command_parts = prepared.command.split()
    assert command_parts[:4] == [
        "bash",
        "/workspace/.drowai/amass/collect_v5.sh",
        "/workspace",
        "example.com",
    ]
    assert command_parts[4:8] == [
        str(build_amass_timeout_budget(75).lock_wait_seconds),
        str(build_amass_timeout_budget(75).enum_deadline_seconds),
        str(build_amass_timeout_budget(75).query_grace_seconds),
        str(build_amass_timeout_budget(75).force_kill_grace_seconds),
    ]
    assert prepared.timeout_plan.native_timeout_field == "execution_timeout"
    assert prepared.timeout_plan.deadline_seconds == 75
    assert [item.relative_path for item in prepared.pre_execution_workspace_files] == [
        ".drowai/amass/collect_v5.sh"
    ]
    assert [
        item.relative_path for item in prepared.pre_execution_workspace_directories
    ] == [
        ".drowai/amass",
        ".drowai/amass/xdg-config",
        ".drowai/amass/xdg-config/amass",
        ".drowai/amass/xdg-data",
        ".drowai/amass/xdg-cache",
        ".drowai/amass/runs",
    ]


def test_capture_contract_declares_canonical_text() -> None:
    contract = AmassTool().capture_contract()

    assert contract is not None
    assert contract.family is CaptureFamily.TEXT_NATIVE
    assert contract.canonical_format is CanonicalCaptureFormat.TEXT


def test_run_uses_wall_clock_timeout_without_changing_amass_inactivity_timeout() -> None:
    tool = AmassTool()
    args = AmassArgs(
        target="example.com",
        execution_timeout=3,
        inactivity_timeout_minutes=17,
    )
    recorded: dict[str, object] = {}

    def _timeout(*positional, **kwargs):
        recorded["command"] = positional[0]
        recorded["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(positional[0], kwargs["timeout"])

    with patch(
        "agent.tools.information_gathering.dns.amass_runtime.subprocess.run",
        side_effect=_timeout,
    ):
        result = tool.run(args)

    command = recorded["command"]
    assert isinstance(command, list)
    assert recorded["timeout"] == 3
    assert command[command.index("-timeout") + 1] == "17"
    assert command[4:8] == [
        str(build_amass_timeout_budget(3).lock_wait_seconds),
        str(build_amass_timeout_budget(3).enum_deadline_seconds),
        str(build_amass_timeout_budget(3).query_grace_seconds),
        str(build_amass_timeout_budget(3).force_kill_grace_seconds),
    ]
    assert result.success is False
    assert result.exit_code == -2
    assert result.stderr == "Command timed out"


@pytest.mark.parametrize("execution_timeout", [1, 2, 3, 5, 75])
def test_collector_internal_budgets_fit_inside_provider_deadline(
    execution_timeout: int,
) -> None:
    budget = build_amass_timeout_budget(execution_timeout)
    internal_seconds = (
        budget.lock_wait_seconds
        + budget.enum_deadline_seconds
        + budget.query_grace_seconds
        + budget.force_kill_grace_seconds
    )

    assert internal_seconds <= execution_timeout
    if execution_timeout > AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS + 2:
        assert (
            internal_seconds
            <= execution_timeout - AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS
        )


def test_parser_preserves_unresolved_names_and_ipv4_ipv6_relationships() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "www.example.com",
            "api.example.com",
            "www.example.com",
            "unresolved.example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "api.example.com 2001:db8::5,192.0.2.20",
            "www.example.com 192.0.2.10",
            AMASS_RESOLVED_END,
        ]
    )

    metadata = parse_amass_v5_results(output)

    assert metadata["parse_status"] == "success"
    assert metadata["names_count"] == 3
    assert metadata["resolved_names_count"] == 2
    assert metadata["unresolved_names_count"] == 1
    assert metadata["ips"] == ["192.0.2.10", "192.0.2.20", "2001:db8::5"]
    assert metadata["subdomains"] == [
        {
            "subdomain": "api.example.com",
            "ip": ["192.0.2.20", "2001:db8::5"],
            "record_types": ["A", "AAAA"],
            "source": "amass",
        },
        {
            "subdomain": "unresolved.example.com",
            "ip": [],
            "record_types": [],
            "source": "amass",
        },
        {
            "subdomain": "www.example.com",
            "ip": ["192.0.2.10"],
            "record_types": ["A"],
            "source": "amass",
        },
    ]


def test_parser_metadata_counts_agree_with_normalized_lists() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "WWW.Example.COM.",
            "api.example.com",
            "api.example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "api.example.com 198.51.100.10",
            "api.example.com 2001:0db8::2,198.51.100.10",
            "www.example.com 2001:0db8::1,192.0.2.2,192.0.2.2",
            AMASS_RESOLVED_END,
        ]
    )

    metadata = parse_amass_v5_results(output)
    subdomains_by_name = {
        item["subdomain"]: item for item in metadata["subdomains"]
    }
    hosts_by_name = {item["hostname"]: item for item in metadata["hosts"]}
    subdomain_ips = {
        address
        for item in metadata["subdomains"]
        for address in item["ip"]
    }
    host_ips = {address for item in metadata["hosts"] for address in item["ip"]}

    assert metadata["parse_status"] == "success"
    assert metadata["names_count"] == len(metadata["subdomains"]) == len(
        metadata["hosts"]
    )
    assert metadata["resolved_names_count"] == sum(
        1 for item in metadata["subdomains"] if item["ip"]
    )
    assert metadata["unresolved_names_count"] == sum(
        1 for item in metadata["subdomains"] if not item["ip"]
    )
    assert metadata["ip_count"] == len(metadata["ips"])
    assert metadata["ips"] == sorted(subdomain_ips | host_ips, key=_ip_sort_key)
    assert (
        hosts_by_name["api.example.com"]["ip"]
        == subdomains_by_name["api.example.com"]["ip"]
    )
    assert (
        hosts_by_name["www.example.com"]["ip"]
        == subdomains_by_name["www.example.com"]["ip"]
    )
    assert subdomains_by_name["api.example.com"]["ip"] == [
        "198.51.100.10",
        "2001:db8::2",
    ]
    assert subdomains_by_name["www.example.com"]["ip"] == [
        "192.0.2.2",
        "2001:db8::1",
    ]
    assert all("subject_key" not in item for item in metadata["subdomains"])
    assert all("subject_type" not in item for item in metadata["hosts"])


def test_parser_reports_invalid_rows_without_duplicating_raw_parse_paths() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "valid.example.com",
            "bad host",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "valid.example.com not-an-ip",
            "missing-address.example.com",
            "other.example.com 203.0.113.4",
            AMASS_RESOLVED_END,
        ]
    )

    metadata = parse_amass_v5_results(output)

    assert metadata["parse_status"] == "partial"
    assert metadata["diagnostics"] == [
        "invalid_name_row",
        "resolved_row_without_valid_address",
        "invalid_resolved_row",
    ]
    assert metadata["subdomains"] == [
        {
            "subdomain": "other.example.com",
            "ip": ["203.0.113.4"],
            "record_types": ["A"],
            "source": "amass",
        },
        {
            "subdomain": "valid.example.com",
            "ip": [],
            "record_types": [],
            "source": "amass",
        },
    ]
    assert metadata["ips"] == ["203.0.113.4"]
    assert metadata["names_count"] == 2
    assert metadata["resolved_names_count"] == 1
    assert metadata["unresolved_names_count"] == 1


def test_parser_locks_current_v5_fixture_contract() -> None:
    output = (FIXTURES_DIR / "information_gathering_dns_amass.txt").read_text(
        encoding="utf-8"
    )

    metadata = parse_amass_v5_results(output)

    assert metadata["parse_status"] == "success"
    assert metadata["diagnostics"] == []
    assert metadata["names_count"] == 4
    assert metadata["resolved_names_count"] == 3
    assert metadata["unresolved_names_count"] == 1
    assert metadata["ip_count"] == 4
    assert metadata["ips"] == [
        "93.184.216.34",
        "93.184.216.35",
        "93.184.216.36",
        "2001:db8::34",
    ]
    assert metadata["hosts"] == [
        {"hostname": "api.example.com", "ip": ["93.184.216.36"]},
        {"hostname": "mail.example.com", "ip": ["93.184.216.35"]},
        {"hostname": "unresolved.example.com", "ip": []},
        {
            "hostname": "www.example.com",
            "ip": ["93.184.216.34", "2001:db8::34"],
        },
    ]


def test_parser_reports_empty_and_rejects_legacy_progress_text() -> None:
    empty = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "No names were discovered",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "No names were discovered",
            AMASS_RESOLVED_END,
        ]
    )
    legacy = "[passive] Detected subdomain: www.example.com"

    assert parse_amass_v5_results(empty)["parse_status"] == "empty"
    legacy_metadata = parse_amass_v5_results(legacy)
    assert legacy_metadata["subdomains"] == []
    assert legacy_metadata["parse_status"] == "partial"
    assert legacy_metadata["diagnostics"] == ["incomplete_capture_sections"]


def test_parser_classifies_root_seed_without_discovered_count() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "example.com 192.0.2.10",
            AMASS_RESOLVED_END,
            AMASS_STATUS_BEGIN,
            "enum_status=0",
            "final_status=complete",
            "timed_out=false",
            AMASS_STATUS_END,
        ]
    )

    metadata = parse_amass_v5_results(output, root_domain="Example.COM.")

    assert metadata["parse_status"] == "success"
    assert metadata["enumeration_status"] == "complete"
    assert metadata["result_completeness"] == "complete"
    assert metadata["partial_results"] is False
    assert metadata["seed_names_count"] == 1
    assert metadata["discovered_names_count"] == 0
    assert metadata["newly_discovered_names_count"] == 0
    assert metadata["prior_names_count"] == 0
    assert metadata["subdomains"] == [
        {
            "subdomain": "example.com",
            "ip": ["192.0.2.10"],
            "record_types": ["A"],
            "source": "amass",
            "discovery_role": "scope_seed",
            "result_scope": "task_cumulative",
        }
    ]


def test_parser_distinguishes_prior_and_newly_discovered_names() -> None:
    output = "\n".join(
        [
            AMASS_BEFORE_NAMES_BEGIN,
            "old.example.com",
            AMASS_BEFORE_NAMES_END,
            AMASS_BEFORE_RESOLVED_BEGIN,
            "old.example.com 192.0.2.11",
            AMASS_BEFORE_RESOLVED_END,
            AMASS_NAMES_BEGIN,
            "example.com",
            "new.example.com",
            "old.example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "new.example.com 192.0.2.12",
            "old.example.com 192.0.2.11",
            AMASS_RESOLVED_END,
            AMASS_STATUS_BEGIN,
            "enum_status=0",
            "final_status=complete",
            "timed_out=false",
            AMASS_STATUS_END,
        ]
    )

    metadata = parse_amass_v5_results(output, root_domain="example.com")

    assert metadata["seed_names_count"] == 1
    assert metadata["prior_names_count"] == 1
    assert metadata["newly_discovered_names_count"] == 1
    assert metadata["discovered_names_count"] == 1
    assert metadata["prior_names"] == ["old.example.com"]
    assert metadata["newly_discovered_names"] == ["new.example.com"]
    assert {
        row["subdomain"]: row["discovery_role"]
        for row in metadata["subdomains"]
    } == {
        "example.com": "scope_seed",
        "new.example.com": "newly_discovered",
        "old.example.com": "prior_known",
    }


def test_parser_keeps_parse_status_independent_from_timed_out_execution() -> None:
    output = "\n".join(
        [
            AMASS_NAMES_BEGIN,
            "api.example.com",
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            "api.example.com 192.0.2.20",
            AMASS_RESOLVED_END,
            AMASS_STATUS_BEGIN,
            "enum_status=124",
            "final_status=timed_out",
            "timed_out=true",
            AMASS_STATUS_END,
        ]
    )

    metadata = parse_amass_v5_results(
        output,
        exit_code=124,
        root_domain="example.com",
    )

    assert metadata["parse_status"] == "success"
    assert metadata["enumeration_status"] == "timed_out"
    assert metadata["enumeration_exit_code"] == 124
    assert metadata["result_completeness"] == "partial"
    assert metadata["partial_results"] is True
    assert metadata["names_count"] == 1


def test_collector_script_preserves_order_tags_spaces_and_success_cleanup(tmp_path) -> None:
    workspace = tmp_path / "workspace with spaces"
    wordlist = workspace / "word list.txt"
    args = AmassArgs(
        target="example.com",
        mode=Mode.BRUTE,
        wordlist=str(wordlist),
        inactivity_timeout_minutes=9,
        verbose=True,
        dns_server="127.0.0.1",
        source=["crtsh", "dns"],
        exclude_source=["archive"],
    )

    completed, log_path = _run_collector_with_fake_amass(tmp_path, workspace, args=args)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.splitlines() == [
        AMASS_BEFORE_NAMES_BEGIN,
        AMASS_BEFORE_NAMES_END,
        AMASS_BEFORE_RESOLVED_BEGIN,
        AMASS_BEFORE_RESOLVED_END,
        AMASS_NAMES_BEGIN,
        "api.example.com",
        "unresolved.example.com",
        AMASS_NAMES_END,
        AMASS_RESOLVED_BEGIN,
        "api.example.com 192.0.2.20,2001:db8::5",
        AMASS_RESOLVED_END,
        AMASS_STATUS_BEGIN,
        "asset_db_readable_before=false",
        "asset_db_readable_after=true",
        "enum_status=0",
        "engine_owned=true",
        "error_code=",
        "final_status=complete",
        "post_query_status=0",
        "pre_query_status=0",
        "timed_out=false",
        AMASS_STATUS_END,
    ]
    metadata = AmassTool().parse_output(completed.stdout, completed.stderr, 0, args)
    assert metadata["names_count"] == 2
    assert metadata["resolved_names_count"] == 1

    calls = _read_fake_amass_calls(log_path)
    assert [call[0] for call in calls] == ["enum", "subs", "subs"]
    output_dir = calls[0][2]
    assert calls[0][1] == "-dir"
    assert output_dir == str(workspace / AMASS_OUTPUT_RELATIVE_DIR)
    assert "workspace with spaces" in output_dir
    assert calls[0][3] == "-d"
    assert calls[0][4:] == [
        "example.com",
        "-brute",
        "-w",
        str(wordlist),
        "-timeout",
        "9",
        "-v",
        "-r",
        "127.0.0.1",
        "-include",
        "crtsh,dns",
        "-exclude",
        "archive",
        "-nocolor",
    ]
    assert calls[1][1:4] == ["-dir", output_dir, "-d"]
    assert calls[2][1:4] == ["-dir", output_dir, "-d"]
    assert calls[1][4:] == ["example.com", "-names", "-nocolor"]
    assert calls[2][4:] == ["example.com", "-names", "-ip", "-nocolor"]
    assert (workspace / AMASS_OUTPUT_RELATIVE_DIR / "asset.db").is_file()


def _collector_failure_stdout(
    *,
    name_rows: tuple[str, ...],
    resolved_rows: tuple[str, ...],
    enum_status: int,
    error_code: str,
    final_status: str,
    post_query_status: int,
    pre_query_status: int,
) -> str:
    """Render the collector's tagged output envelope for a failed stage."""

    return "\n".join(
        [
            AMASS_BEFORE_NAMES_BEGIN,
            AMASS_BEFORE_NAMES_END,
            AMASS_BEFORE_RESOLVED_BEGIN,
            AMASS_BEFORE_RESOLVED_END,
            AMASS_NAMES_BEGIN,
            *name_rows,
            AMASS_NAMES_END,
            AMASS_RESOLVED_BEGIN,
            *resolved_rows,
            AMASS_RESOLVED_END,
            AMASS_STATUS_BEGIN,
            "asset_db_readable_before=false",
            "asset_db_readable_after=true",
            f"enum_status={enum_status}",
            "engine_owned=true",
            f"error_code={error_code}",
            f"final_status={final_status}",
            f"post_query_status={post_query_status}",
            f"pre_query_status={pre_query_status}",
            "timed_out=false",
            AMASS_STATUS_END,
            "",
        ]
    )


@pytest.mark.parametrize(
    ("fail_stage", "expected_code", "expected_stdout"),
    [
        (
            "enum",
            23,
            _collector_failure_stdout(
                name_rows=("api.example.com", "unresolved.example.com"),
                resolved_rows=("api.example.com 192.0.2.20,2001:db8::5",),
                enum_status=23,
                error_code="enum_failed",
                final_status="enum_failed",
                post_query_status=0,
                pre_query_status=0,
            ),
        ),
        (
            "names",
            24,
            _collector_failure_stdout(
                name_rows=(),
                resolved_rows=("api.example.com 192.0.2.20,2001:db8::5",),
                enum_status=0,
                error_code="query_failed",
                final_status="query_failed",
                post_query_status=24,
                pre_query_status=0,
            ),
        ),
        (
            "resolved",
            25,
            _collector_failure_stdout(
                name_rows=("api.example.com", "unresolved.example.com"),
                resolved_rows=(),
                enum_status=0,
                error_code="query_failed",
                final_status="query_failed",
                post_query_status=25,
                pre_query_status=0,
            ),
        ),
    ],
)
def test_collector_script_propagates_return_codes_and_cleans_failed_sessions(
    tmp_path,
    fail_stage: str,
    expected_code: int,
    expected_stdout: str,
) -> None:
    workspace = tmp_path / f"workspace {fail_stage}"
    args = AmassArgs(target="example.com", inactivity_timeout_minutes=9)

    completed, _log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        fail_stage=fail_stage,
    )

    assert completed.returncode == expected_code
    assert completed.stdout == expected_stdout
    assert (workspace / AMASS_OUTPUT_RELATIVE_DIR / "asset.db").is_file()


def test_collector_reuses_database_across_sequential_invocations(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com")

    first, first_log = _run_collector_with_fake_amass(tmp_path, workspace, args=args)
    second, second_log = _run_collector_with_fake_amass(tmp_path, workspace, args=args)

    assert first.returncode == 0
    assert second.returncode == 0
    output_dir = str(workspace / AMASS_OUTPUT_RELATIVE_DIR)
    assert (workspace / AMASS_OUTPUT_RELATIVE_DIR / "asset.db").read_text(
        encoding="utf-8"
    ) == "fake asset db\n"
    first_calls = _read_fake_amass_calls(first_log)
    second_calls = _read_fake_amass_calls(second_log)
    assert [call[0] for call in first_calls] == ["enum", "subs", "subs"]
    assert [call[0] for call in second_calls] == [
        "subs",
        "subs",
        "enum",
        "subs",
        "subs",
    ]
    assert all(call[2] == output_dir for call in first_calls + second_calls)
    assert AMASS_BEFORE_NAMES_BEGIN in second.stdout
    assert second.stdout.index(AMASS_BEFORE_NAMES_BEGIN) < second.stdout.index(
        AMASS_NAMES_BEGIN
    )


def test_collector_serializes_enumeration_sections_for_one_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com", execution_timeout=10)
    lock_probe = tmp_path / "fake-enum-overlap"
    env_extra = {
        "AMASS_ENUM_SLEEP_SECONDS": "2",
        "AMASS_OVERLAP_PROBE": str(lock_probe),
    }

    first, first_log = _start_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        env_extra=env_extra,
    )
    second, second_log = _start_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        env_extra=env_extra,
    )
    first_stdout, first_stderr = first.communicate(timeout=8)
    second_stdout, second_stderr = second.communicate(timeout=8)

    assert first.returncode == 0
    assert second.returncode == 0
    assert "OVERLAP" not in first_stderr + second_stderr
    assert AMASS_NAMES_BEGIN in first_stdout
    assert AMASS_NAMES_BEGIN in second_stdout
    enum_markers = [
        line
        for path in (first_log, second_log)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line in {"ENUM_BEGIN", "ENUM_END"}
    ]
    assert enum_markers == ["ENUM_BEGIN", "ENUM_END", "ENUM_BEGIN", "ENUM_END"]


def test_collector_lock_wait_times_out_before_provider_deadline(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    asset_dir = workspace / AMASS_OUTPUT_RELATIVE_DIR
    asset_dir.mkdir(parents=True)
    (asset_dir / "asset.db").write_text("existing fake db\n", encoding="utf-8")
    args = AmassArgs(target="example.com", execution_timeout=5)
    lock_handle = _hold_workflow_lock(workspace)

    try:
        started = time.monotonic()
        completed, log_path = _run_collector_with_fake_amass(
            tmp_path,
            workspace,
            args=args,
            timeout=4,
        )
        duration = time.monotonic() - started
    finally:
        lock_handle.close()

    assert duration < args.execution_timeout
    assert completed.returncode == 124
    assert "DROWAI_AMASS_LOCK_TIMEOUT" in completed.stderr
    assert "error_code=lock_timeout" in completed.stdout
    assert "final_status=timed_out" in completed.stdout
    assert "asset_db_readable_before=true" in completed.stdout
    assert "asset_db_readable_after=true" in completed.stdout
    assert not log_path.exists()


def test_collector_ignores_abandoned_lock_file_before_running(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    lock_path = workspace / ".drowai/amass/workflow.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("abandoned\n", encoding="utf-8")
    args = AmassArgs(target="example.com", execution_timeout=5)

    completed, log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        timeout=5,
    )

    assert completed.returncode == 0
    assert "final_status=complete" in completed.stdout
    assert [call[0] for call in _read_fake_amass_calls(log_path)] == [
        "enum",
        "subs",
        "subs",
    ]
    assert lock_path.is_file()


def test_collector_treats_live_kernel_lock_as_active(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com", execution_timeout=5)
    lock_handle = _hold_workflow_lock(workspace)

    try:
        completed, _log_path = _run_collector_with_fake_amass(
            tmp_path,
            workspace,
            args=args,
            timeout=4,
        )
    finally:
        lock_handle.close()

    assert completed.returncode == 124
    assert "error_code=lock_timeout" in completed.stdout
    assert (workspace / ".drowai/amass/workflow.lock").is_file()


def test_collector_reuses_lock_file_after_previous_owner_releases(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com", execution_timeout=5)
    lock_handle = _hold_workflow_lock(workspace)
    lock_handle.close()

    completed, _log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        timeout=5,
    )

    assert completed.returncode == 0
    assert "final_status=complete" in completed.stdout
    assert (workspace / ".drowai/amass/workflow.lock").is_file()


def test_wall_clock_timeout_interrupts_enum_and_queries_partial_results(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com", execution_timeout=3)

    completed, _log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        env_extra={"AMASS_ENUM_SLEEP_SECONDS": "8"},
        timeout=6,
    )

    assert completed.returncode == 124
    assert "INTERRUPTED_ENUM" in completed.stderr
    assert AMASS_NAMES_BEGIN in completed.stdout
    assert "api.example.com" in completed.stdout
    assert "final_status=timed_out" in completed.stdout
    assert "timed_out=true" in completed.stdout
    metadata = AmassTool().parse_output(
        completed.stdout,
        completed.stderr,
        completed.returncode,
        args,
    )
    assert metadata["parse_status"] == "success"
    assert metadata["enumeration_status"] == "timed_out"
    assert metadata["result_completeness"] == "partial"
    assert metadata["partial_results"] is True
    assert metadata["names_count"] == 2


def test_query_grace_is_total_bounded_and_pre_query_cannot_starve_post_query(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    asset_dir = workspace / AMASS_OUTPUT_RELATIVE_DIR
    asset_dir.mkdir(parents=True)
    (asset_dir / "asset.db").write_text("existing fake db\n", encoding="utf-8")
    args = AmassArgs(target="example.com", execution_timeout=5)

    started = time.monotonic()
    completed, _log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        env_extra={"AMASS_SUBS_SLEEP_SECONDS": "8"},
        timeout=8,
    )
    duration = time.monotonic() - started

    assert duration < 7
    assert completed.returncode == 124
    assert "pre_query_status=124" in completed.stdout
    assert "post_query_status=124" in completed.stdout
    assert AMASS_NAMES_BEGIN in completed.stdout
    assert AMASS_RESOLVED_BEGIN in completed.stdout


def test_unexpected_enum_failure_still_attempts_post_query(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com")

    completed, log_path = _run_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        fail_stage="enum",
    )

    assert completed.returncode == 23
    assert "final_status=enum_failed" in completed.stdout
    calls = _read_fake_amass_calls(log_path)
    assert [call[0] for call in calls] == ["enum", "subs", "subs"]


def test_unowned_engine_port_fails_closed_with_stable_diagnostic(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    args = AmassArgs(target="example.com")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        completed, _log_path = _run_collector_with_fake_amass(
            tmp_path,
            workspace,
            args=args,
            env_extra={"DROWAI_AMASS_ENGINE_PORT": str(port)},
        )

    assert completed.returncode == 70
    assert "DROWAI_AMASS_ENGINE_UNOWNED" in completed.stderr
    assert "error_code=unowned_engine_port_occupied" in completed.stdout
    assert AMASS_NAMES_BEGIN not in completed.stdout


@pytest.mark.asyncio
async def test_pty_and_file_comm_prepare_the_same_amass_runtime_contract(tmp_path) -> None:
    config = SimpleNamespace(task_id=1, tenant_id=7, workspace_path=str(tmp_path))
    parameters = {"target": "example.com", "execution_timeout": 75}

    file_comm = await prepare_tool_command(
        tool_id="information_gathering.dns.amass",
        parameters=parameters,
        config=config,
        transport="file-comm",
        explicit_command_builder=lambda _tool_id, _parameters: "",
    )
    pty = await prepare_tool_command(
        tool_id="information_gathering.dns.amass",
        parameters=parameters,
        config=config,
        transport="pty",
        explicit_command_builder=lambda _tool_id, _parameters: "",
    )

    assert file_comm.command == pty.command
    assert file_comm.timeout_plan.deadline_seconds == pty.timeout_plan.deadline_seconds
    assert (
        file_comm.pre_execution_workspace_files
        == pty.pre_execution_workspace_files
    )
    assert (
        file_comm.pre_execution_workspace_directories
        == pty.pre_execution_workspace_directories
    )


def _run_collector_with_fake_amass(
    tmp_path: Path,
    workspace: Path,
    *,
    args: AmassArgs,
    fail_stage: str = "",
    env_extra: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Execute the collector with a fake Amass binary and task-local workspace."""

    process, log_path = _start_collector_with_fake_amass(
        tmp_path,
        workspace,
        args=args,
        fail_stage=fail_stage,
        env_extra=env_extra,
    )
    stdout, stderr = process.communicate(timeout=timeout)
    return (
        subprocess.CompletedProcess(
            args=process.args,
            returncode=int(process.returncode if process.returncode is not None else -1),
            stdout=stdout,
            stderr=stderr,
        ),
        log_path,
    )


def _start_collector_with_fake_amass(
    tmp_path: Path,
    workspace: Path,
    *,
    args: AmassArgs,
    fail_stage: str = "",
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path]:
    """Start the collector with a fake Amass binary and task-local workspace."""

    tool = AmassTool()
    collector = workspace / ".drowai/amass/collect_v5.sh"
    collector.parent.mkdir(parents=True, exist_ok=True)
    collector.write_bytes(tool.prepare_workspace_files(args)[0].content_bytes())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log_path = tmp_path / f"fake-amass-calls-{time.time_ns()}.log"
    fake_amass = fake_bin / "amass"
    fake_amass.write_text(
        """#!/usr/bin/env bash
set -u
{
    printf 'CALL\\n'
    for arg in "$@"; do
        printf '[%s]\\n' "$arg"
    done
} >> "$AMASS_FAKE_LOG"

if [[ "$1" == "enum" ]]; then
    mkdir -p "$3"
    printf '%s\\n' 'fake asset db' > "$3/asset.db"
    printf '%s\\n' 'ENUM_BEGIN' >> "$AMASS_FAKE_LOG"
    if [[ -n "${AMASS_OVERLAP_PROBE:-}" ]]; then
        if ! mkdir "$AMASS_OVERLAP_PROBE" 2>/dev/null; then
            printf '%s\\n' 'OVERLAP' >&2
            exit 88
        fi
    fi
    trap 'printf "%s\\n" "INTERRUPTED_ENUM" >&2; [[ -n "${AMASS_OVERLAP_PROBE:-}" ]] && rmdir "$AMASS_OVERLAP_PROBE" 2>/dev/null || true; exit 130' INT TERM
    if [[ -n "${AMASS_ENUM_SLEEP_SECONDS:-}" ]]; then
        sleep_until=$((SECONDS + AMASS_ENUM_SLEEP_SECONDS))
        while [[ "$SECONDS" -lt "$sleep_until" ]]; do
            sleep 0.1
        done
    fi
    if [[ "${AMASS_FAIL_STAGE:-}" == "enum" ]]; then
        [[ -n "${AMASS_OVERLAP_PROBE:-}" ]] && rmdir "$AMASS_OVERLAP_PROBE" 2>/dev/null || true
        exit 23
    fi
    [[ -n "${AMASS_OVERLAP_PROBE:-}" ]] && rmdir "$AMASS_OVERLAP_PROBE" 2>/dev/null || true
    printf '%s\\n' 'ENUM_END' >> "$AMASS_FAKE_LOG"
    exit 0
fi

if [[ "$1" == "subs" ]]; then
    trap 'exit 130' INT TERM
    if [[ -n "${AMASS_SUBS_SLEEP_SECONDS:-}" ]]; then
        sleep_until=$((SECONDS + AMASS_SUBS_SLEEP_SECONDS))
        while [[ "$SECONDS" -lt "$sleep_until" ]]; do
            sleep 0.1
        done
    fi
    if [[ " $* " == *" -ip "* ]]; then
        if [[ "${AMASS_FAIL_STAGE:-}" == "resolved" ]]; then
            exit 25
        fi
        printf '%s\\n' 'api.example.com 192.0.2.20,2001:db8::5'
    else
        if [[ "${AMASS_FAIL_STAGE:-}" == "names" ]]; then
            exit 24
        fi
        printf '%s\\n' 'api.example.com' 'unresolved.example.com'
    fi
    exit 0
fi

exit 2
""",
        encoding="utf-8",
    )
    fake_amass.chmod(0o755)

    command = build_amass_collector_command(
        args,
        workspace_root=str(workspace),
        script_path=str(collector),
    )
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["AMASS_FAKE_LOG"] = str(log_path)
    env["AMASS_FAIL_STAGE"] = fail_stage
    env.setdefault("DROWAI_AMASS_ENGINE_PORT", str(_unused_local_port()))
    if env_extra:
        env.update(env_extra)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return process, log_path


def _read_fake_amass_calls(log_path: Path) -> list[list[str]]:
    """Return logged fake Amass argv calls."""

    calls: list[list[str]] = []
    current: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line in {"ENUM_BEGIN", "ENUM_END"}:
            continue
        if line == "CALL":
            if current:
                calls.append(current)
            current = []
            continue
        current.append(line.removeprefix("[").removesuffix("]"))
    if current:
        calls.append(current)
    return calls


def _hold_workflow_lock(workspace: Path):
    """Hold the same kernel file lock used by the generated collector."""

    lock_path = workspace / ".drowai/amass/workflow.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unused_local_port() -> int:
    """Return a currently unused localhost TCP port for collector tests."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ip_sort_key(value: str) -> tuple[int, int]:
    import ipaddress

    address = ipaddress.ip_address(value)
    return (address.version, int(address))
