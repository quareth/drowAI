"""Regression coverage for shared tool execution utility behavior."""

from __future__ import annotations

import pytest

from agent.tools.utils import sanitize_command_text


def test_sanitize_command_text_preserves_shell_syntax_and_layout() -> None:
    command = (
        'printf "%s\\n" "a b" | sed \'s/ /_/\' > out.txt\n'
        "printf '%s  %s\\n' left right"
    )

    assert sanitize_command_text(command) == command


def test_sanitize_command_text_redacts_assignment_without_reformatting_command() -> None:
    command = (
        "TOKEN='secret value' curl --oauth2-bearer=second-secret \\\n"
        "  -H 'Authorization: Bearer third-secret' https://user:pass@example.test | jq . > out.json"
    )

    assert sanitize_command_text(command) == (
        "TOKEN='<REDACTED>' curl --oauth2-bearer=<REDACTED> \\\n"
        "  -H 'Authorization: <REDACTED>' https://<REDACTED>@example.test | jq . > out.json"
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sshpass -p secret ssh host", "sshpass -p <REDACTED> ssh host"),
        ("curl --user 'name:pass' URL", "curl --user '<REDACTED>' URL"),
        ("curl -u=name:pass URL", "curl -u=<REDACTED> URL"),
        ("curl -H 'X-Api-Key: key' URL", "curl -H 'X-Api-Key: <REDACTED>' URL"),
        ("PASSWORD=abc command", "PASSWORD=<REDACTED> command"),
    ],
)
def test_sanitize_command_text_redacts_supported_credential_forms(
    command: str,
    expected: str,
) -> None:
    assert sanitize_command_text(command) == expected
