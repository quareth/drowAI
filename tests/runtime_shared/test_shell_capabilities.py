"""Contract tests for shared shell alias and capability normalization."""

from runtime_shared.shell_capabilities import (
    MODEL_FACING_SHELL_START_TOOL_IDS,
    SHELL_EXEC_TOOL_ID,
    SHELL_WRITE_STDIN_TOOL_ID,
    ShellCapability,
    canonical_shell_implementation_tool_id,
    resolve_shell_start_capability,
)


def test_model_aliases_map_to_declared_capabilities_and_one_implementation() -> None:
    assert resolve_shell_start_capability("shell.utility") is ShellCapability.UTILITY
    assert (
        resolve_shell_start_capability("shell.assessment")
        is ShellCapability.ASSESSMENT
    )
    assert resolve_shell_start_capability(SHELL_WRITE_STDIN_TOOL_ID) is None
    assert MODEL_FACING_SHELL_START_TOOL_IDS == frozenset(
        {"shell.utility", "shell.assessment"}
    )
    for alias in MODEL_FACING_SHELL_START_TOOL_IDS:
        assert canonical_shell_implementation_tool_id(alias) == SHELL_EXEC_TOOL_ID


def test_capability_resolution_does_not_accept_command_text() -> None:
    assert resolve_shell_start_capability("nmap -sV target") is None
    assert canonical_shell_implementation_tool_id("echo hello") == "echo hello"
