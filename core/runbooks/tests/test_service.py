"""Tests for the public runbook service facade."""

from __future__ import annotations

from pathlib import Path

from core.runbooks.models import RunbookStage
from core.runbooks.registry import RunbookRegistry
from core.runbooks.service import RunbookService


def test_service_renders_matching_tool_runbooks(tmp_path):
    registry = _registry_with_runbook(tmp_path)
    service = RunbookService(registry=registry)

    rendered = service.render_for_tools(
        selected_tools=["filesystem.read_file"],
        stage=RunbookStage.TOOL_PARAMETERS,
    )

    assert rendered.startswith("Tool Runbooks:")
    assert "Runbook: artifact-evidence-reading" in rendered
    assert "Read the requested artifact before answering." in rendered


def test_default_service_renders_configured_filesystem_runbook():
    service = RunbookService()

    for tool_id in ("filesystem.read_file", "filesystem.search_text"):
        rendered = service.render_for_tools(
            selected_tools=[tool_id],
            stage=RunbookStage.TOOL_PARAMETERS,
        )

        assert rendered.startswith("Tool Runbooks:")
        assert "Runbook: artifact-evidence-reading" in rendered
        assert "Artifact Evidence Reading" in rendered


def test_default_service_renders_configured_metasploit_runbook():
    service = RunbookService()

    for tool_id in (
        "exploitation_tools.metasploit.search_modules",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
    ):
        rendered = service.render_for_tools(
            selected_tools=[tool_id],
            stage=RunbookStage.TOOL_PARAMETERS,
        )

        assert rendered.startswith("Tool Runbooks:")
        assert "Runbook: metasploit-exploitation" in rendered
        assert "Metasploit Exploitation" in rendered
        assert "session creation is the primary success signal" in rendered
        assert "AutoCheck" in rendered


def test_default_service_renders_configured_web_runbook_for_categories():
    service = RunbookService()

    rendered = service.render_for_categories(
        selected_categories=["web_applications"],
        stage=RunbookStage.TOOL_SELECTION,
    )

    assert rendered.startswith("Tool Runbooks:")
    assert "Runbook: web-discovery" in rendered
    assert "Choose tools by the shape of the work" in rendered
    assert "Use `/FUZZ`" not in rendered


def test_default_service_renders_configured_web_runbook_for_tools():
    service = RunbookService()

    rendered = service.render_for_tools(
        selected_tools=["web_applications.web_crawlers.ffuf"],
        stage=RunbookStage.TOOL_PARAMETERS,
    )

    assert rendered.startswith("Tool Runbooks:")
    assert "Runbook: ffuf-crawler" in rendered
    assert "The `FUZZ` marker must be in the URL path" in rendered
    assert "Do not assume the wrapper adds silent mode" in rendered
    assert "json_output_path" in rendered
    assert "Choose tools by the shape of the work" not in rendered


def test_default_service_does_not_render_web_discovery_for_ffuf_parameters():
    service = RunbookService()

    rendered = service.render_for_tools(
        selected_tools=["web_applications.web_crawlers.ffuf"],
        stage=RunbookStage.TOOL_PARAMETERS,
    )

    assert "Runbook: web-discovery" not in rendered


def test_default_service_renders_configured_tshark_runbook_for_parameters():
    service = RunbookService()

    rendered = service.render_for_tools(
        selected_tools=["sniffing_spoofing.network_sniffers.tshark"],
        stage=RunbookStage.TOOL_PARAMETERS,
    )

    assert rendered.startswith("Tool Runbooks:")
    assert "Runbook: tshark-pcap-analysis" in rendered
    assert "`survey`" in rendered
    assert "`find_security_relevant_artifacts`" in rendered
    assert "Do not request broad full-packet JSON" in rendered


def test_service_returns_empty_string_for_unrelated_tools(tmp_path):
    registry = _registry_with_runbook(tmp_path)
    service = RunbookService(registry=registry)

    assert (
        service.render_for_tools(
            selected_tools=["terminal.execute"],
            stage=RunbookStage.TOOL_PARAMETERS,
        )
        == ""
    )


def test_service_returns_empty_string_for_unrelated_categories(tmp_path):
    registry = _registry_with_runbook(tmp_path)
    service = RunbookService(registry=registry)

    assert (
        service.render_for_categories(
            selected_categories=["web_applications"],
            stage=RunbookStage.TOOL_SELECTION,
        )
        == ""
    )


def test_default_service_does_not_render_metasploit_runbook_for_removed_old_tool_id():
    service = RunbookService()

    rendered = service.render_for_tools(
        selected_tools=["exploitation_tools.metasploit.msfconsole"],
        stage=RunbookStage.TOOL_PARAMETERS,
    )

    assert rendered == ""


def test_service_returns_empty_string_without_loading_for_empty_selected_tools():
    service = RunbookService(registry=_FailingRegistry())

    assert (
        service.render_for_tools(
            selected_tools=[],
            stage=RunbookStage.TOOL_PARAMETERS,
        )
        == ""
    )


def _registry_with_runbook(tmp_path: Path) -> RunbookRegistry:
    runbook_path = tmp_path / "filesystem_artifact_reading/RUNBOOK.md"
    runbook_path.parent.mkdir(parents=True, exist_ok=True)
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
name: artifact-evidence-reading
type: tool
version: 1
description: Guide parameter generation for selected filesystem read/search tools.
trigger_tool_ids:
  - filesystem.read_file
  - filesystem.search_text
stages:
  - tool_parameters
---
Read the requested artifact before answering.
""",
        encoding="utf-8",
    )
    return RunbookRegistry(builtin_root=tmp_path)


class _FailingRegistry:
    def load_builtin_tool_runbooks(self):
        raise AssertionError("registry should not be loaded without selected tools")
