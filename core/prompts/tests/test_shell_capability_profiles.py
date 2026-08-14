"""Tests for conditional stable shell capability profile composition."""

import pytest

from core.prompts.builders.shell_capability_profiles import (
    build_shell_capability_profiles,
)
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


@pytest.mark.parametrize(
    ("tool_ids", "expected"),
    [
        ((), False),
        (("shell.write_stdin",), False),
        (("information_gathering.network_discovery.nmap",), False),
        (("shell.utility",), True),
        (("shell.assessment",), True),
        (("shell.utility", "shell.assessment"), True),
    ],
)
def test_profiles_are_conditioned_on_callable_start_aliases(
    tool_ids: tuple[str, ...],
    expected: bool,
) -> None:
    rendered = build_shell_capability_profiles(tool_ids)

    assert bool(rendered) is expected
    if expected:
        assert rendered.count("Use shell.utility for ordinary") == 1
        assert rendered.count("Use shell.assessment for commands") == 1
        assert rendered.count("Leave interactive=false for ordinary commands") == 1
        assert rendered.count("never resend the originating") == 1


def test_duplicate_aliases_do_not_duplicate_profile_text() -> None:
    rendered = build_shell_capability_profiles(
        ["shell.utility", "shell.utility", "shell.assessment"]
    )

    assert rendered.count("Use shell.utility for ordinary") == 1
    assert rendered.count("Use shell.assessment for commands") == 1
    assert rendered.count("Leave interactive=false for ordinary commands") == 1


def test_main_tool_parameter_system_prompt_uses_exact_callable_ids() -> None:
    builder = ToolPlanningPromptBuilder()

    without_shell = builder.build_tool_parameters_system_prompt(
        callable_tool_ids=["information_gathering.network_discovery.nmap"]
    )
    with_shell = builder.build_tool_parameters_system_prompt(
        callable_tool_ids=["shell.assessment"]
    )

    assert "Shell Capability Profiles:" not in without_shell
    assert with_shell.count("Shell Capability Profiles:") == 1
    assert with_shell.count("Use shell.utility for ordinary") == 1
    assert with_shell.count("Use shell.assessment for commands") == 1
    assert with_shell.count("Leave interactive=false for ordinary commands") == 1


def test_main_tool_selector_prompt_uses_visible_shell_ids() -> None:
    builder = ToolPlanningPromptBuilder()
    common = {
        "target": "localhost",
        "phase": "enumeration",
        "constraints": {},
    }

    without_shell = builder.build_select_tools_prompt(
        resolved_tools=["information_gathering.network_discovery.nmap"],
        **common,
    )
    with_shell = builder.build_select_tools_prompt(
        resolved_tools=["shell.utility", "shell.assessment"],
        **common,
    )

    assert "Shell Capability Profiles:" not in without_shell
    assert with_shell.count("Shell Capability Profiles:") == 1
    assert with_shell.count("Use shell.utility for ordinary") == 1
    assert with_shell.count("Use shell.assessment for commands") == 1
    assert with_shell.count("Leave interactive=false for ordinary commands") == 1
