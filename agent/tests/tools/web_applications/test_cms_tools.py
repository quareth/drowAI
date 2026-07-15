import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.tools.web_applications.cms_identification.wpscan import (
    WPScanArgs,
    WPScanTool,
    ScanMode as WPScanMode,
    OutputFormat as WPOutputFormat,
)
from agent.tools.web_applications.cms_identification.whatweb import (
    WhatWebArgs,
    WhatWebTool,
    AggressionLevel,
    LogFormat as WhatWebLogFormat,
    RedirectMode,
)
from agent.tools.web_applications.cms_identification.joomscan import (
    JoomScanArgs,
    JoomScanTool,
    ScanType as JoomScanType,
    OutputFormat as JoomOutputFormat,
)
from agent.tools.web_applications.cms_identification.droopescan import (
    DroopescanArgs,
    DroopescanTool,
    ScanType as DroopeScanType,
    OutputFormat as DroopeOutputFormat,
)
from agent.tools.web_applications.cms_identification.cmsmap import (
    CMSmapArgs,
    CMSmapTool,
    CMSType,
    OutputFormat as CMSmapOutputFormat,
)


# ---------------------------------------------------------------------------
# WPScan
# ---------------------------------------------------------------------------


def test_wpscan_build_command_minimal():
    args = WPScanArgs(target="http://example.com")
    command = WPScanTool().build_command(args)
    assert "wpscan" in command[0]
    assert "--url" in command
    assert args.target in command


def test_wpscan_build_command_with_auth():
    args = WPScanArgs(
        target="http://example.com",
        api_token="tok",
        headers="X-Test: 1",
        proxy="http://127.0.0.1:8080",
        disable_tls_checks=True,
        cookies="a=b",
        user_agent="ua",
    )
    command = WPScanTool().build_command(args)
    assert "--api-token" in command
    assert "--headers" in command
    assert "--proxy" in command
    assert "--disable-tls-checks" in command
    assert "a=b" in command
    assert "ua" in command


def test_wpscan_build_command_with_enumeration():
    args = WPScanArgs(
        target="http://example.com",
        plugins=True,
        themes=True,
        users=True,
        enumerate_all=True,
    )
    command = WPScanTool().build_command(args)
    assert "--enumerate" in command
    enum_index = command.index("--enumerate") + 1
    flags = command[enum_index]
    assert "p" in flags and "t" in flags and "u" in flags
    assert "ap" in flags and "cb" in flags


def test_wpscan_parse_output_json():
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com", output_format=WPOutputFormat.JSON)
    stdout = """
    {"version": {"number": "6.0"}, "plugins": ["plugin1"], "vulnerabilities": [{"severity": "high", "name": "CVE-2020"}]}
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["wordpress_version"] == "6.0"
    assert metadata["vulnerabilities"][0]["severity"] == "High"


def test_wpscan_parse_output_text():
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com", output_format=WPOutputFormat.CLI)
    stdout = "WordPress version: 6.0\nVulnerability: test\nPlugin: sample"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["wordpress_version"] == "6.0"
    assert "Plugin" in metadata["plugins"][0] or metadata["plugins"]


def test_wpscan_parse_output_empty():
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_wpscan_create_artifacts(tmp_path, monkeypatch):
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com")
    stdout = "X" * 150
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_wpscan_run_success():
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com")
    stdout = '{"version": {"number": "6.1"}}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["wordpress_version"] == "6.1"


def test_wpscan_run_timeout():
    tool = WPScanTool()
    args = WPScanArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# WhatWeb
# ---------------------------------------------------------------------------


def test_whatweb_build_command_minimal():
    args = WhatWebArgs(target="http://example.com")
    command = WhatWebTool().build_command(args)
    assert command[0] == "whatweb"
    assert "-a" in command and "1" in command
    assert "--log-json=-" in command
    assert "--quiet" in command
    assert args.target in command
    assert "--output-format" not in command
    assert "--follow-redirects" not in command
    assert "--timeout" not in command


def test_whatweb_build_command_with_auth():
    args = WhatWebArgs(
        target="http://example.com",
        user="user:pass",
        proxy="127.0.0.1:8080",
        proxy_user="proxy:pass",
        headers=["X-Test: 1"],
        cookie="a=b",
        cookie_jar="/tmp/cookies.txt",
        follow_redirect=RedirectMode.NEVER,
        user_agent="ua",
    )
    command = WhatWebTool().build_command(args)
    assert "--user" in command and "user:pass" in command
    assert "--proxy" in command
    assert "--proxy-user" in command and "proxy:pass" in command
    assert "--header" in command and "X-Test: 1" in command
    assert "--cookie" in command and "a=b" in command
    assert "--cookie-jar" in command and "/tmp/cookies.txt" in command
    assert "--follow-redirect=never" in command
    assert "ua" in command


def test_whatweb_build_command_with_enumeration():
    args = WhatWebArgs(
        target="http://example.com",
        plugins=["wordpress", "-apache"],
        aggression_level=AggressionLevel.HEAVY,
    )
    command = WhatWebTool().build_command(args)
    assert "--plugins" in command
    assert "wordpress,-apache" in command
    assert str(int(AggressionLevel.HEAVY.value)) in command
    assert "--exclude-plugins" not in command
    assert "--pluginpath" not in command


def test_whatweb_validation_rejects_string_aggression_level():
    with pytest.raises(ValidationError):
        WhatWebArgs(target="http://example.com", aggression_level="aggressive")


def test_whatweb_parse_output_json():
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com", log_format=WhatWebLogFormat.JSON)
    stdout = (
        '[{"target":"http://example.com","http_status":200,"plugins":{"WordPress":{"version":["6.0"]},'
        '"Apache":{"string":["2.4.57"]},"PHP":{"version":["8.2.0"]}}}]'
    )
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["technologies"]
    assert metadata["scan_status"] == "parsed_json"
    assert any(str(t.get("name", "")).lower() == "wordpress" for t in metadata["technologies"])
    assert metadata["cms_detected"]


def test_whatweb_parse_output_text():
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com", log_format=WhatWebLogFormat.BRIEF)
    stdout = "http://example.com [200 OK] Apache[2.4.57], PHP[8.2.0], HTML5"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["web_servers"] or metadata["languages"] or metadata["technologies"]


def test_whatweb_parse_output_empty():
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["technologies"] == []


def test_whatweb_create_artifacts(tmp_path, monkeypatch):
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com")
    stdout = "X" * 120
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_whatweb_run_success():
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com")
    stdout = '[{"target":"http://example.com","http_status":200,"plugins":{"Drupal":{}}}]'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert isinstance(result.metadata.get("technologies"), list)
    assert result.metadata["technologies"]


def test_whatweb_run_timeout():
    tool = WhatWebTool()
    args = WhatWebArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# JoomScan
# ---------------------------------------------------------------------------


def test_joomscan_build_command_minimal():
    args = JoomScanArgs(target="http://example.com", scan_type=JoomScanType.BASIC)
    command = JoomScanTool().build_command(args)
    assert command[0] == "joomscan"
    assert "--type" in command
    assert args.target in command


def test_joomscan_build_command_with_auth():
    args = JoomScanArgs(
        target="http://example.com",
        authentication="user:pass",
        headers="X-Test: 1",
        proxy="http://127.0.0.1:8080",
        cookies="a=b",
        random_user_agent=True,
    )
    command = JoomScanTool().build_command(args)
    assert "--auth" in command
    assert "--headers" in command
    assert "--proxy" in command
    assert "--cookies" in command
    assert "--random-agent" in command


def test_joomscan_build_command_with_enumeration():
    args = JoomScanArgs(
        target="http://example.com",
        enumerate_components=True,
        enumerate_plugins=True,
        enumerate_modules=True,
        enumerate_templates=True,
    )
    command = JoomScanTool().build_command(args)
    assert "--enumerate-components" in command
    assert "--enumerate-plugins" in command
    assert "--enumerate-modules" in command
    assert "--enumerate-templates" in command


def test_joomscan_parse_output_json():
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com", output_format=JoomOutputFormat.JSON)
    stdout = """
    {"version": {"number": "4.0"}, "components": ["com_content"], "vulnerabilities": [{"severity": "medium"}]}
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["joomla_version"] in ("4.0", {"number": "4.0"})
    assert metadata["vulnerabilities"][0]["severity"] == "Medium"


def test_joomscan_parse_output_text():
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com", output_format=JoomOutputFormat.TEXT)
    stdout = "Joomla version: 4.0\nComponent: com_content"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["joomla_version"] == "4.0"
    assert metadata["components"]


def test_joomscan_parse_output_empty():
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_joomscan_create_artifacts(tmp_path, monkeypatch):
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com")
    stdout = "X" * 140
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_joomscan_run_success():
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com")
    stdout = '{"version": {"number": "4.1"}}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["joomla_version"] in ("4.1", {"number": "4.1"})


def test_joomscan_run_timeout():
    tool = JoomScanTool()
    args = JoomScanArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Droopescan
# ---------------------------------------------------------------------------


def test_droopescan_build_command_minimal():
    args = DroopescanArgs(target="http://example.com")
    command = DroopescanTool().build_command(args)
    assert command[0] == "droopescan"
    assert args.target in command
    assert "-c" in command


def test_droopescan_build_command_with_auth():
    args = DroopescanArgs(
        target="http://example.com",
        authentication="user:pass",
        proxy="http://127.0.0.1:8080",
        custom_payload="exploit",
        enumerate_plugins=True,
        enumerate_themes=True,
    )
    command = DroopescanTool().build_command(args)
    assert "-a" in command and "user:pass" in command
    assert "--proxy" in command
    assert "--plugins" in command
    assert "--themes" in command
    assert "exploit" in command


def test_droopescan_parse_output_json():
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com", output_format=DroopeOutputFormat.JSON)
    stdout = """
    {"version": "9", "modules": ["views"], "vulnerabilities": [{"severity": "low"}]}
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["version_found"] == "9"
    assert metadata["vulnerabilities"][0]["severity"] == "Low"


def test_droopescan_parse_output_text():
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com", output_format=DroopeOutputFormat.TEXT)
    stdout = "Drupal detected\nVersion: 9\nModule: views"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["drupal_detected"]
    assert metadata["version_found"]


def test_droopescan_parse_output_empty():
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_droopescan_create_artifacts(tmp_path, monkeypatch):
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com")
    stdout = "X" * 160
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_droopescan_run_success():
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com")
    stdout = '{"version": "9"}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["version_found"] is not None


def test_droopescan_run_timeout():
    tool = DroopescanTool()
    args = DroopescanArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# CMSmap
# ---------------------------------------------------------------------------


def test_cmsmap_build_command_minimal():
    args = CMSmapArgs(target="http://example.com", cms_type=CMSType.WORDPRESS)
    command = CMSmapTool().build_command(args)
    assert command[0] == "cmsmap"
    assert "-t" in command
    assert args.target in command


def test_cmsmap_build_command_with_auth():
    args = CMSmapArgs(
        target="http://example.com",
        authentication="user:pass",
        proxy="http://127.0.0.1:8080",
        enumerate_plugins=True,
        enumerate_themes=True,
        enumerate_users=True,
        brute_force=True,
        wordlist="wordlist.txt",
    )
    command = CMSmapTool().build_command(args)
    assert "-a" in command and "user:pass" in command
    assert "--proxy" in command
    assert "--plugins" in command
    assert "--themes" in command
    assert "--users" in command
    assert "--bruteforce" in command
    assert "wordlist.txt" in command


def test_cmsmap_parse_output_json():
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com", output_format=CMSmapOutputFormat.JSON)
    stdout = """
    {"cms": "wordpress", "version": "6.2", "plugins": ["akismet"], "vulnerabilities": [{"severity": "critical"}]}
    """
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["cms_detected"] == "wordpress"
    assert metadata["vulnerabilities"][0]["severity"] == "Critical"


def test_cmsmap_parse_output_text():
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com", output_format=CMSmapOutputFormat.TEXT)
    stdout = "WordPress detected: wordpress\nVersion: 6.2\nPlugin: akismet"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["cms_detected"]
    assert metadata["version_found"]


def test_cmsmap_parse_output_empty():
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_cmsmap_create_artifacts(tmp_path, monkeypatch):
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com")
    stdout = "X" * 180
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_cmsmap_run_success():
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com")
    stdout = '{"cms": "wordpress"}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert "wordpress" in str(result.metadata.get("cms_detected", ""))


def test_cmsmap_run_timeout():
    tool = CMSmapTool()
    args = CMSmapArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2
