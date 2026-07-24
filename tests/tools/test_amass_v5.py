"""Focused command, collector, and parser contracts for OWASP Amass v5."""

from __future__ import annotations

import os
import subprocess
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

    assert command == [
        "bash",
        "/workspace/.drowai/amass/collect_v5.sh",
        "/workspace",
        "example.com",
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
        "agent.tools.information_gathering.dns.amass.subprocess.run",
        side_effect=_timeout,
    ):
        result = tool.run(args)

    command = recorded["command"]
    assert isinstance(command, list)
    assert recorded["timeout"] == 3
    assert command[command.index("-timeout") + 1] == "17"
    assert result.success is False
    assert result.exit_code == -2
    assert result.stderr == "Command timed out"


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
        AMASS_NAMES_BEGIN,
        "api.example.com",
        "unresolved.example.com",
        AMASS_NAMES_END,
        AMASS_RESOLVED_BEGIN,
        "api.example.com 192.0.2.20,2001:db8::5",
        AMASS_RESOLVED_END,
    ]
    metadata = AmassTool().parse_output(completed.stdout, completed.stderr, 0, args)
    assert metadata["names_count"] == 2
    assert metadata["resolved_names_count"] == 1

    calls = _read_fake_amass_calls(log_path)
    assert [call[0] for call in calls] == ["enum", "subs", "subs"]
    session_dir = calls[0][2]
    assert calls[0][1] == "-dir"
    assert session_dir.startswith(str(workspace / ".drowai/amass" / "session."))
    assert "workspace with spaces" in session_dir
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
    assert calls[1][1:4] == ["-dir", session_dir, "-d"]
    assert calls[2][1:4] == ["-dir", session_dir, "-d"]
    assert calls[1][4:] == ["example.com", "-names", "-nocolor"]
    assert calls[2][4:] == ["example.com", "-names", "-ip", "-nocolor"]
    assert list((workspace / ".drowai/amass").glob("session.*")) == []


@pytest.mark.parametrize(
    ("fail_stage", "expected_code", "expected_stdout"),
    [
        ("enum", 23, ""),
        ("names", 24, f"{AMASS_NAMES_BEGIN}\n{AMASS_NAMES_END}\n"),
        (
            "resolved",
            25,
            "\n".join(
                [
                    AMASS_NAMES_BEGIN,
                    "api.example.com",
                    "unresolved.example.com",
                    AMASS_NAMES_END,
                    AMASS_RESOLVED_BEGIN,
                    AMASS_RESOLVED_END,
                    "",
                ]
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
    assert list((workspace / ".drowai/amass").glob("session.*")) == []


def _run_collector_with_fake_amass(
    tmp_path: Path,
    workspace: Path,
    *,
    args: AmassArgs,
    fail_stage: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Execute the collector with a fake Amass binary and task-local workspace."""

    tool = AmassTool()
    collector = workspace / ".drowai/amass/collect_v5.sh"
    collector.parent.mkdir(parents=True)
    collector.write_bytes(tool.prepare_workspace_files(args)[0].content_bytes())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log_path = tmp_path / "fake-amass-calls.log"
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
    if [[ "${AMASS_FAIL_STAGE:-}" == "enum" ]]; then
        exit 23
    fi
    exit 0
fi

if [[ "$1" == "subs" ]]; then
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

    command = tool._build_collector_command(
        args,
        workspace_root=str(workspace),
        script_path=str(collector),
    )
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["AMASS_FAKE_LOG"] = str(log_path)
    env["AMASS_FAIL_STAGE"] = fail_stage
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return completed, log_path


def _read_fake_amass_calls(log_path: Path) -> list[list[str]]:
    """Return logged fake Amass argv calls."""

    calls: list[list[str]] = []
    current: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line == "CALL":
            if current:
                calls.append(current)
            current = []
            continue
        current.append(line.removeprefix("[").removesuffix("]"))
    if current:
        calls.append(current)
    return calls


def _ip_sort_key(value: str) -> tuple[int, int]:
    import ipaddress

    address = ipaddress.ip_address(value)
    return (address.version, int(address))
