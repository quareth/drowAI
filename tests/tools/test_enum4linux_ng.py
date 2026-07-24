"""Focused command-contract tests for the enum4linux-ng adapter."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from agent.tools.information_gathering.smb_enumeration.enum4linux import (
    Enum4LinuxArgs,
    Enum4LinuxMode,
    Enum4LinuxTool,
)


@pytest.mark.parametrize(
    ("mode", "expected_flags"),
    [
        (Enum4LinuxMode.BASIC, ["-A"]),
        (Enum4LinuxMode.FULL, ["-A", "-C", "-R"]),
        (Enum4LinuxMode.USERS, ["-U"]),
        (Enum4LinuxMode.SHARES, ["-S"]),
        (Enum4LinuxMode.GROUPS, ["-G"]),
        (Enum4LinuxMode.PASSWORDS, ["-P"]),
    ],
)
def test_build_command_maps_modes_to_enum4linux_ng(
    mode: Enum4LinuxMode,
    expected_flags: list[str],
) -> None:
    command = Enum4LinuxTool().build_command(
        Enum4LinuxArgs(target="127.0.0.1", mode=mode)
    )

    assert command[0] == "enum4linux-ng"
    assert command[1 : 1 + len(expected_flags)] == expected_flags
    assert command[-1] == "127.0.0.1"


def test_build_command_maps_supported_optional_arguments() -> None:
    command = Enum4LinuxTool().build_command(
        Enum4LinuxArgs(
            target="smb.internal",
            mode=Enum4LinuxMode.USERS,
            username="analyst",
            password="secret",
            domain="EXAMPLE",
            timeout=15,
            verbose=True,
            output_file="artifacts/enum4linux/result",
        )
    )

    assert command == [
        "enum4linux-ng",
        "-U",
        "-u",
        "analyst",
        "-p",
        "secret",
        "-w",
        "EXAMPLE",
        "-t",
        "15",
        "-v",
        "-oA",
        "artifacts/enum4linux/result",
        "smb.internal",
    ]


def test_args_reject_unsupported_custom_smb_port() -> None:
    with pytest.raises(ValidationError):
        Enum4LinuxArgs(target="127.0.0.1", port=1445)


def test_args_reject_conflicting_domain_and_workgroup() -> None:
    with pytest.raises(ValidationError):
        Enum4LinuxArgs(
            target="127.0.0.1",
            domain="EXAMPLE",
            workgroup="OTHER",
        )


@patch(
    "agent.tools.information_gathering.smb_enumeration.enum4linux.subprocess.run"
)
def test_run_executes_enum4linux_ng_command(mock_run: Mock) -> None:
    mock_run.return_value = Mock(returncode=0, stdout="scan completed", stderr="")

    result = Enum4LinuxTool().run(Enum4LinuxArgs(target="127.0.0.1"))

    command = mock_run.call_args.args[0]
    assert command[0] == "enum4linux-ng"
    assert result.success is True
