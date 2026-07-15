import subprocess
from typing import Any, Dict

import pytest

from agent.tools.web_applications.cms_identification.wpscan import (
    WPScanArgs,
    WPScanTool,
    OutputFormat as WPOutputFormat,
)
from agent.tools.web_applications.cms_identification.whatweb import (
    WhatWebArgs,
    WhatWebTool,
    LogFormat as WhatWebLogFormat,
)
from agent.tools.web_applications.cms_identification.joomscan import (
    JoomScanArgs,
    JoomScanTool,
    OutputFormat as JoomOutputFormat,
)
from agent.tools.web_applications.cms_identification.droopescan import (
    DroopescanArgs,
    DroopescanTool,
    OutputFormat as DroopeOutputFormat,
)
from agent.tools.web_applications.cms_identification.cmsmap import (
    CMSmapArgs,
    CMSmapTool,
    OutputFormat as CMSmapOutputFormat,
    CMSType,
)


TOOLS = [
    (WPScanTool(), WPScanArgs(target="http://example.com")),
    (WhatWebTool(), WhatWebArgs(target="http://example.com")),
    (JoomScanTool(), JoomScanArgs(target="http://example.com")),
    (DroopescanTool(), DroopescanArgs(target="http://example.com")),
    (CMSmapTool(), CMSmapArgs(target="http://example.com", cms_type=CMSType.WORDPRESS)),
]


def test_execution_model_methods_present():
    for tool, _ in TOOLS:
        assert hasattr(tool, "build_command")
        assert hasattr(tool, "parse_output")
        assert hasattr(tool, "create_artifacts")


def test_run_invokes_parse_and_artifacts(monkeypatch):
    for tool, args in TOOLS:
        called: Dict[str, Any] = {"parse": False, "artifacts": False}

        def _fake_parse(stdout, stderr, exit_code, parsed_args):
            called["parse"] = True
            return {"fake": True, "exit_code": exit_code}

        def _fake_artifacts(stdout, parsed_args, timestamp=None):
            called["artifacts"] = True
            return []

        def _mock_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr(tool, "parse_output", _fake_parse)
        monkeypatch.setattr(tool, "create_artifacts", _fake_artifacts)

        with monkeypatch.context() as m:
            m.setattr("subprocess.run", _mock_run)
            result = tool.run(args)

        assert called["parse"] is True
        assert called["artifacts"] is True
        assert result.metadata.get("fake") is True


def test_metadata_contains_expected_keys():
    wpscan_tool = WPScanTool()
    wpscan_args = WPScanArgs(target="http://example.com", output_format=WPOutputFormat.JSON)
    wpscan_metadata = wpscan_tool.parse_output(
        '{"version": {"number": "6"}, "vulnerabilities": [{"severity": "high"}]}',
        "",
        0,
        wpscan_args,
    )
    assert "vulnerabilities" in wpscan_metadata
    assert "wordpress_version" in wpscan_metadata

    cmsmap_tool = CMSmapTool()
    cmsmap_args = CMSmapArgs(target="http://example.com", output_format=CMSmapOutputFormat.JSON)
    cmsmap_metadata = cmsmap_tool.parse_output(
        '{"cms": "wordpress", "version": "6.2", "vulnerabilities": [{"severity": "low"}]}',
        "",
        0,
        cmsmap_args,
    )
    assert cmsmap_metadata["cms_detected"] == "wordpress"
    assert cmsmap_metadata["vulnerabilities"][0]["severity"] in {"Low", "low", "Unknown"}


def test_artifact_naming(monkeypatch, tmp_path):
    artifact_inputs = [
        (WPScanTool(), WPScanArgs(target="t"), WPOutputFormat.JSON, "wpscan"),
        (WhatWebTool(), WhatWebArgs(target="t"), WhatWebLogFormat.JSON, "whatweb"),
        (JoomScanTool(), JoomScanArgs(target="t"), JoomOutputFormat.JSON, "joomscan"),
        (
            DroopescanTool(),
            DroopescanArgs(target="t"),
            DroopeOutputFormat.JSON,
            "droopescan",
        ),
        (
            CMSmapTool(),
            CMSmapArgs(target="t", cms_type=CMSType.WORDPRESS),
            CMSmapOutputFormat.JSON,
            "cmsmap",
        ),
    ]
    monkeypatch.chdir(tmp_path)
    for tool, args, _, prefix in artifact_inputs:
        artifacts = tool.create_artifacts("X" * 150, args, timestamp=111)
        assert artifacts
        assert artifacts[0].startswith(f"artifacts/{prefix}")
        assert (tmp_path / artifacts[0]).exists()


def test_parsing_utilities_usage_for_json_outputs():
    droope_tool = DroopescanTool()
    droope_args = DroopescanArgs(target="http://example.com", output_format=DroopeOutputFormat.JSON)
    metadata = droope_tool.parse_output(
        '{"version": "9", "vulnerabilities": [{"severity": "critical"}]}',
        "",
        0,
        droope_args,
    )
    assert metadata["vulnerabilities"]
    assert metadata["vulnerabilities"][0]["severity"] in {"Critical", "High"}

