"""Tests for the packaged Amass collector shell asset and renderer."""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from agent.tools.information_gathering.dns import amass_runtime
from agent.tools.information_gathering.dns.amass_runtime import (
    AMASS_COLLECTOR_RELATIVE_PATH,
    AMASS_COLLECTOR_SCRIPT,
    prepare_amass_workspace_files,
)

_COLLECTOR_ASSET_NAME = "amass_collector_v5.sh"
_PRE_EXTRACTION_SHA256 = (
    "91b1a71ac68fd4ab3a849bae35e39e121c11f834a74aab129f9fa77d00b13646"
)


class _CollectorResource:
    """Minimal resource facade for malformed-template renderer tests."""

    def __init__(self, content: str) -> None:
        self._content = content

    def joinpath(self, _name: str) -> "_CollectorResource":
        return self

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        return self._content


def test_collector_asset_renders_byte_equivalent_workspace_content() -> None:
    """Extraction preserves the exact collector bytes materialized at runtime."""

    template = (
        files("agent.tools.information_gathering.dns")
        .joinpath(_COLLECTOR_ASSET_NAME)
        .read_text(encoding="utf-8")
    )
    rendered = amass_runtime._render_amass_collector_script()
    prepared = prepare_amass_workspace_files(object())

    assert "@@AMASS_STATUS_BEGIN@@" in template
    assert "@@" not in rendered
    assert rendered == AMASS_COLLECTOR_SCRIPT
    assert prepared[0].relative_path == AMASS_COLLECTOR_RELATIVE_PATH
    assert prepared[0].content_bytes() == rendered.encode("utf-8")
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == (
        _PRE_EXTRACTION_SHA256
    )


def test_collector_asset_and_rendered_script_are_valid_bash() -> None:
    """Both the packaged template and rendered collector pass Bash syntax."""

    template = (
        files("agent.tools.information_gathering.dns")
        .joinpath(_COLLECTOR_ASSET_NAME)
        .read_text(encoding="utf-8")
    )
    for script in (template, AMASS_COLLECTOR_SCRIPT):
        result = subprocess.run(
            ["bash", "-n"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_collector_renderer_rejects_placeholder_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing and unknown tokens fail instead of producing a partial script."""

    template = (
        files("agent.tools.information_gathering.dns")
        .joinpath(_COLLECTOR_ASSET_NAME)
        .read_text(encoding="utf-8")
        .replace("@@AMASS_STATUS_BEGIN@@", "@@UNKNOWN_MARKER@@")
    )
    monkeypatch.setattr(
        amass_runtime.resources,
        "files",
        lambda _package: _CollectorResource(template),
    )

    with pytest.raises(
        RuntimeError,
        match=r"missing=.*AMASS_STATUS_BEGIN.*unexpected=.*UNKNOWN_MARKER",
    ):
        amass_runtime._render_amass_collector_script()


def test_collector_asset_is_included_in_agent_package_data() -> None:
    """Installed wheels must retain the adjacent collector shell resource."""

    repository_root = Path(__file__).resolve().parents[2]
    with (repository_root / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    package_data = project["tool"]["setuptools"]["package-data"]
    assert "tools/information_gathering/dns/amass_collector_v5.sh" in package_data[
        "agent"
    ]
