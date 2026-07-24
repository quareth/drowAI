"""Focused command, collector, and parser contracts for OWASP Amass v5."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_workspace_collector_queries_names_and_resolutions_then_cleans_session() -> None:
    tool = AmassTool()
    args = AmassArgs(target="example.com")
    prepared_files = tool.prepare_workspace_files(args)
    prepared_directories = tool.prepare_workspace_directories(args)

    assert [item.relative_path for item in prepared_directories] == [".drowai/amass"]
    assert [item.relative_path for item in prepared_files] == [
        ".drowai/amass/collect_v5.sh"
    ]

    script = prepared_files[0].content_bytes().decode("utf-8")
    assert 'amass enum -dir "$session_dir"' in script
    assert script.count('amass subs -dir "$session_dir"') == 2
    assert 'mktemp -d "$session_parent/session.XXXXXX"' in script
    assert 'rm -rf -- "$session_dir"' in script


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

    assert prepared.command.startswith(
        "bash /workspace/.drowai/amass/collect_v5.sh /workspace example.com"
    )
    assert prepared.timeout_plan.native_timeout_field == "execution_timeout"
    assert prepared.timeout_plan.deadline_seconds == 75
    assert [item.relative_path for item in prepared.pre_execution_workspace_files] == [
        ".drowai/amass/collect_v5.sh"
    ]
    assert [
        item.relative_path for item in prepared.pre_execution_workspace_directories
    ] == [".drowai/amass"]


def test_capture_contract_declares_canonical_text() -> None:
    contract = AmassTool().capture_contract()

    assert contract is not None
    assert contract.family is CaptureFamily.TEXT_NATIVE
    assert contract.canonical_format is CanonicalCaptureFormat.TEXT


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


def test_collector_script_runs_two_queries_with_a_fake_amass_binary(tmp_path) -> None:
    tool = AmassTool()
    args = AmassArgs(target="example.com", inactivity_timeout_minutes=9)
    collector = tmp_path / ".drowai/amass/collect_v5.sh"
    collector.parent.mkdir(parents=True)
    collector.write_bytes(tool.prepare_workspace_files(args)[0].content_bytes())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_amass = fake_bin / "amass"
    fake_amass.write_text(
        """#!/usr/bin/env bash
set -u
case "$1" in
    enum)
        exit 0
        ;;
    subs)
        if [[ " $* " == *" -ip "* ]]; then
            printf '%s\n' 'api.example.com 192.0.2.20,2001:db8::5'
        else
            printf '%s\n' 'api.example.com' 'unresolved.example.com'
        fi
        exit 0
        ;;
esac
exit 2
""",
        encoding="utf-8",
    )
    fake_amass.chmod(0o755)

    command = tool._build_collector_command(
        args,
        workspace_root=str(tmp_path),
        script_path=str(collector),
    )
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0
    metadata = tool.parse_output(completed.stdout, completed.stderr, 0, args)
    assert metadata["names_count"] == 2
    assert metadata["resolved_names_count"] == 1
    assert list((tmp_path / ".drowai/amass").glob("session.*")) == []


def _ip_sort_key(value: str) -> tuple[int, int]:
    import ipaddress

    address = ipaddress.ip_address(value)
    return (address.version, int(address))
