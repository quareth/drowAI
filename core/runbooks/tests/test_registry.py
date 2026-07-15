"""Tests for loading discovered builtin runbooks."""

from __future__ import annotations

from pathlib import Path

from core.runbooks.loader import RunbookLoader
from core.runbooks.models import RunbookStage, RunbookType
from core.runbooks.registry import (
    DEFAULT_BUILTIN_RUNBOOK_ROOT,
    DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT,
    RunbookRegistry,
)


def test_default_builtin_tool_runbook_root_is_scoped_to_tool_runbooks():
    assert DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT == (
        DEFAULT_BUILTIN_RUNBOOK_ROOT / "tool_runbooks"
    )


def test_default_filesystem_artifact_runbook_asset_validates():
    loaded = RunbookLoader().load(
        DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT / "filesystem_artifact_reading/RUNBOOK.md"
    )

    assert loaded.id == "filesystem_artifact_reading"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == (
        "filesystem.read_file",
        "filesystem.search_text",
    )
    assert loaded.stages == (RunbookStage.TOOL_PARAMETERS,)
    assert "Use this skill" not in loaded.body
    assert "Use this runbook" in loaded.body
    assert 'read_mode="grep"' in loaded.body


def test_default_metasploit_exploitation_runbook_asset_validates():
    loaded = RunbookLoader().load(
        DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT / "metasploit_exploitation/RUNBOOK.md"
    )

    assert loaded.id == "metasploit_exploitation"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == (
        "exploitation_tools.metasploit.search_modules",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
    )
    assert loaded.stages == (RunbookStage.TOOL_PARAMETERS,)
    assert "Use this skill" not in loaded.body
    assert "Metasploit is a module console" in loaded.body
    assert "exploitation_tools.metasploit.msfconsole" in loaded.body
    assert "AutoCheck=true" in loaded.body
    assert "Targeting Drupal 7.x as a fallback" in loaded.body
    assert "`run_exploit` module paths outside `exploit/...` are invalid" in loaded.body


def test_default_web_discovery_runbook_asset_validates():
    loaded = RunbookLoader().load(
        DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT / "web_discovery/RUNBOOK.md"
    )

    assert loaded.id == "web_discovery"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == (
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    )
    assert loaded.trigger_category_ids == ("web_applications",)
    assert loaded.stages == (
        RunbookStage.TOOL_SELECTION,
        RunbookStage.TOOL_PARAMETERS,
    )
    assert "Stage: tool_selection" in loaded.body
    assert "Stage: tool_parameters" in loaded.body


def test_default_ffuf_crawler_runbook_asset_validates():
    loaded = RunbookLoader().load(
        DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT / "ffuf_crawler/RUNBOOK.md"
    )

    assert loaded.id == "ffuf_crawler"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == ("web_applications.web_crawlers.ffuf",)
    assert loaded.trigger_category_ids == ()
    assert loaded.stages == (RunbookStage.TOOL_PARAMETERS,)
    assert "FFUF Crawler" in loaded.body
    assert "Do not assume the wrapper adds silent mode" in loaded.body
    assert "json_output_path" in loaded.body


def test_default_tshark_pcap_analysis_runbook_asset_validates():
    loaded = RunbookLoader().load(
        DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT / "tshark_pcap_analysis/RUNBOOK.md"
    )

    assert loaded.id == "tshark_pcap_analysis"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == ("sniffing_spoofing.network_sniffers.tshark",)
    assert loaded.trigger_category_ids == ()
    assert loaded.stages == (RunbookStage.TOOL_PARAMETERS,)
    assert "TShark PCAP Analysis" in loaded.body
    assert "`survey`" in loaded.body
    assert "`investigate_protocol`" in loaded.body
    assert "`find_security_relevant_artifacts`" in loaded.body
    assert "Do not request non-allowlisted fields" in loaded.body


def test_registry_discovers_runbooks_without_python_registration(tmp_path):
    runbook_path = tmp_path / "filesystem_artifact_reading/RUNBOOK.md"
    _write_runbook(runbook_path, runbook_id="filesystem_artifact_reading")
    _write_runbook(tmp_path / "tenant/RUNBOOK.md", runbook_id="tenant_runbook")
    loader = _RecordingLoader()
    registry = RunbookRegistry(builtin_root=tmp_path, loader=loader)

    runbooks = registry.load_builtin_tool_runbooks()

    assert tuple(runbook.id for runbook in runbooks) == (
        "filesystem_artifact_reading",
        "tenant_runbook",
    )
    assert loader.loaded_paths == [
        runbook_path,
        tmp_path / "tenant/RUNBOOK.md",
    ]


def test_registry_discovers_runbooks_in_deterministic_relative_path_order(tmp_path):
    _write_runbook(tmp_path / "zeta/RUNBOOK.md", runbook_id="zeta")
    _write_runbook(tmp_path / "alpha/RUNBOOK.md", runbook_id="alpha")
    _write_runbook(tmp_path / "middle/nested/RUNBOOK.md", runbook_id="middle")

    runbooks = RunbookRegistry(builtin_root=tmp_path).load_builtin_tool_runbooks()

    assert tuple(runbook.id for runbook in runbooks) == ("alpha", "middle", "zeta")


def test_registry_skips_invalid_runbook_with_warning(tmp_path, caplog):
    _write_runbook(tmp_path / "valid/RUNBOOK.md", runbook_id="valid")
    invalid_path = tmp_path / "invalid/RUNBOOK.md"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text("missing frontmatter\n", encoding="utf-8")

    runbooks = RunbookRegistry(builtin_root=tmp_path).load_builtin_tool_runbooks()

    assert tuple(runbook.id for runbook in runbooks) == ("valid",)
    assert "Skipping invalid runbook" in caplog.text
    assert str(invalid_path) in caplog.text


def test_registry_skips_duplicate_runbook_ids_with_warning(tmp_path, caplog):
    _write_runbook(tmp_path / "first/RUNBOOK.md", runbook_id="duplicate")
    _write_runbook(tmp_path / "second/RUNBOOK.md", runbook_id="duplicate")

    runbooks = RunbookRegistry(builtin_root=tmp_path).load_builtin_tool_runbooks()

    assert tuple(runbook.id for runbook in runbooks) == ("duplicate",)
    assert "Skipping duplicate runbook id" in caplog.text
    assert "second/RUNBOOK.md" in caplog.text


def test_registry_returns_empty_tuple_for_missing_builtin_root(tmp_path):
    registry = RunbookRegistry(
        builtin_root=tmp_path / "missing",
    )

    assert registry.load_builtin_tool_runbooks() == ()


class _RecordingLoader(RunbookLoader):
    def __init__(self) -> None:
        self.loaded_paths: list[Path] = []

    def load(self, path: Path | str):
        self.loaded_paths.append(Path(path))
        return super().load(path)


def _write_runbook(path: Path, *, runbook_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
id: {runbook_id}
name: Filesystem Artifact Reading
type: tool
version: 1
description: Guides artifact reads for filesystem tools.
trigger_tool_ids:
  - filesystem.read_file
stages:
  - tool_parameters
---
Read the requested artifact before answering.
""",
        encoding="utf-8",
    )
