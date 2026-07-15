"""Tests for rendering loaded runbooks into prompt-ready text."""

from __future__ import annotations

from core.runbooks.models import LoadedRunbook, RunbookStage, RunbookType
from core.runbooks.renderer import render_runbooks


def test_empty_runbook_list_renders_empty_string():
    assert render_runbooks(()) == ""


def test_non_empty_runbook_list_renders_one_tool_runbooks_section():
    first = _runbook(
        "filesystem_artifact_reading",
        name="artifact-evidence-reading",
        body="# Artifact Evidence Reading\nRead bounded artifact evidence.",
    )
    second = _runbook(
        "filesystem_artifact_search",
        name="artifact-evidence-search",
        body="# Artifact Evidence Search\nSearch bounded artifact evidence.",
    )

    rendered = render_runbooks((first, second))

    assert rendered.count("Tool Runbooks:") == 1
    assert "Runbook: artifact-evidence-reading" in rendered
    assert "Description: filesystem_artifact_reading guidance." in rendered
    assert "# Artifact Evidence Reading" in rendered
    assert "Runbook: artifact-evidence-search" in rendered
    assert "# Artifact Evidence Search" in rendered


def test_rendered_output_uses_runbook_label():
    runbook = _runbook(
        "filesystem_artifact_reading",
        name="artifact-evidence-reading",
        body="# Artifact Evidence Reading\nUse bounded reads.",
    )

    rendered = render_runbooks((runbook,))

    assert "Runbook: artifact-evidence-reading" in rendered
    assert "Description:" in rendered
    assert "Use bounded reads." in rendered


def test_rendered_output_uses_only_requested_stage_section():
    runbook = _runbook(
        "web_discovery",
        name="web-discovery",
        body=(
            "# Web Discovery\n\n"
            "Intro text.\n\n"
            "## Stage: tool_selection\n\n"
            "Choose ffuf for broad path discovery.\n\n"
            "## Stage: tool_parameters\n\n"
            "Use /FUZZ for crawler targets."
        ),
    )

    rendered = render_runbooks((runbook,), stage=RunbookStage.TOOL_SELECTION)

    assert "Choose ffuf for broad path discovery." in rendered
    assert "Use /FUZZ for crawler targets." not in rendered
    assert "Intro text." not in rendered


def test_rendered_output_omits_runbook_when_requested_stage_section_missing():
    runbook = _runbook(
        "web_discovery",
        name="web-discovery",
        body="## Stage: tool_parameters\n\nUse /FUZZ for crawler targets.",
    )

    assert render_runbooks((runbook,), stage=RunbookStage.TOOL_SELECTION) == ""


def _runbook(
    runbook_id: str,
    *,
    name: str,
    body: str,
) -> LoadedRunbook:
    return LoadedRunbook(
        id=runbook_id,
        name=name,
        type=RunbookType.TOOL,
        version=1,
        description=f"{runbook_id} guidance.",
        trigger_tool_ids=("filesystem.read_file",),
        stages=(RunbookStage.TOOL_PARAMETERS,),
        body=body,
    )
