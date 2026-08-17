"""Tests for the shared shell output persistence contract."""

import pytest

from agent.tool_runtime.output_persistence_policy import resolve_output_persistence
from runtime_shared.shell_capabilities import ShellCapability


def _runtime_metadata(capability: object) -> dict:
    return {
        "runtime_session": {
            "originating_capability": capability,
        }
    }


@pytest.mark.parametrize(
    ("tool_id", "metadata", "expected_capability", "expected_evidence"),
    [
        ("shell.utility", {}, ShellCapability.UTILITY, False),
        ("shell.assessment", {}, ShellCapability.ASSESSMENT, True),
        (
            "shell.write_stdin",
            _runtime_metadata("utility"),
            ShellCapability.UTILITY,
            False,
        ),
        (
            "shell.write_stdin",
            _runtime_metadata("assessment"),
            ShellCapability.ASSESSMENT,
            True,
        ),
        ("shell.write_stdin", {}, None, False),
        ("shell.write_stdin", _runtime_metadata("unknown"), None, False),
        ("shell.exec", {}, ShellCapability.ASSESSMENT, True),
    ],
)
def test_shell_persistence_matrix(
    tool_id: str,
    metadata: dict,
    expected_capability: ShellCapability | None,
    expected_evidence: bool,
) -> None:
    decision = resolve_output_persistence(tool_id, metadata)

    assert decision.is_shell_call is True
    assert decision.originating_capability is expected_capability
    assert decision.persist_workspace_artifact is expected_evidence
    assert decision.assessment_evidence_eligible is expected_evidence
    assert decision.knowledge_eligible is expected_evidence
    assert decision.retain_durable_output is expected_evidence


def test_shell_aliases_apply_capability_persistence_regardless_of_command_text() -> None:
    nmap_utility = resolve_output_persistence("shell.utility", {"command": "nmap"})
    filesystem_assessment = resolve_output_persistence(
        "shell.assessment", {"command": "ls -la"}
    )
    another_utility = resolve_output_persistence(
        "shell.utility", {"command": "printf hello"}
    )

    assert nmap_utility.retain_durable_output is False
    assert another_utility == nmap_utility
    assert filesystem_assessment.retain_durable_output is True
    assert filesystem_assessment.persist_workspace_artifact is True


def test_non_shell_tools_keep_existing_persistence_defaults() -> None:
    nmap = resolve_output_persistence(
        "information_gathering.network_discovery.nmap"
    )
    read_file = resolve_output_persistence("filesystem.read_file")

    assert nmap.persist_workspace_artifact is True
    assert nmap.knowledge_eligible is True
    assert read_file.persist_workspace_artifact is False
    assert read_file.knowledge_eligible is True
