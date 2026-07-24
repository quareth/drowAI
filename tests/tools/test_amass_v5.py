"""Focused command-contract tests for the OWASP Amass v5 adapter."""

from __future__ import annotations

from agent.tools.information_gathering.dns.amass import AmassArgs, AmassTool, Mode


def test_passive_mode_uses_v5_domain_flag_and_minute_timeout() -> None:
    command = AmassTool().build_command(
        AmassArgs(target="example.com", mode=Mode.PASSIVE, timeout=61)
    )

    assert command == [
        "amass",
        "enum",
        "-passive",
        "-timeout",
        "2",
        "-nocolor",
        "-d",
        "example.com",
    ]


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


def test_dns_mode_uses_default_v5_enumeration() -> None:
    command = AmassTool().build_command(
        AmassArgs(target="example.com", mode=Mode.DNS)
    )

    assert command[:2] == ["amass", "enum"]
    assert "-d" in command
    assert "dns" not in command


def test_reverse_mode_maps_target_to_v5_addr_flag() -> None:
    command = AmassTool().build_command(
        AmassArgs(target="127.0.0.1", mode=Mode.REVERSE_DNS)
    )

    assert command[-2:] == ["-addr", "127.0.0.1"]
    assert "-d" not in command


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
    assert "-src" not in command
