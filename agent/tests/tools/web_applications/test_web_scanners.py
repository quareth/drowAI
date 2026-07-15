import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import (
    SqlmapArgs,
    SqlmapLevel,
    SqlmapRisk,
    SqlmapTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.nikto import (
    NiktoArgs,
    NiktoMode,
    NiktoTool,
    OutputFormat as NiktoOutputFormat,
)
from agent.tools.web_applications.web_vulnerability_scanners.wapiti import (
    OutputFormat as WapitiOutputFormat,
    ScanLevel,
    WapitiArgs,
    WapitiTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.skipfish import (
    AuthMode,
    OutputFormat as SkipfishOutputFormat,
    SkipfishArgs,
    SkipfishTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.commix import (
    CommixArgs,
    CommixTool,
    InjectionMethod as CommixInjectionMethod,
    OutputFormat as CommixOutputFormat,
)
from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
    NucleiArgs,
    NucleiMode,
    NucleiTool,
    ReportFormat,
)
from agent.tools.web_applications.web_vulnerability_scanners.xsser import (
    OutputFormat as XsserOutputFormat,
    XsserArgs,
    XsserTool,
)


# ---------------------------------------------------------------------------
# SQLMap
# ---------------------------------------------------------------------------


def test_sqlmap_build_command_minimal():
    args = SqlmapArgs(target="http://example.com")
    command = SqlmapTool().build_command(args)
    assert command[0] == "sqlmap"
    assert "-u" in command
    assert "http://example.com" in command


def test_sqlmap_build_command_with_auth():
    args = SqlmapArgs(
        target="http://example.com",
        auth_type="Basic",
        auth_cred="user:pass",
        proxy="http://127.0.0.1:8080",
    )
    command = SqlmapTool().build_command(args)
    assert "--auth-type" in command and "--auth-cred" in command
    assert "--proxy" in command


def test_sqlmap_build_command_excludes_output_format():
    """SQLMap is text-native; --output-format is not a documented CLI flag."""
    args = SqlmapArgs(target="http://example.com")
    command = SqlmapTool().build_command(args)
    assert "--output-format" not in command
    assert "json" not in command  # No fake JSON output mode

    # Even with enumeration flags set
    args2 = SqlmapArgs(
        target="http://example.com",
        db="testdb",
        table="users",
        column="password",
        dump=True,
    )
    command2 = SqlmapTool().build_command(args2)
    assert "--output-format" not in command2


def test_sqlmap_build_command_uses_short_enumeration_flags():
    """SQLMap documents -D / -T / -C for db/table/column enumeration."""
    args = SqlmapArgs(
        target="http://example.com",
        db="appdb",
        table="users",
        column="email",
    )
    command = SqlmapTool().build_command(args)
    # Documented short flags are present
    assert "-D" in command and "appdb" in command
    assert "-T" in command and "users" in command
    assert "-C" in command and "email" in command
    # Undocumented long forms are not emitted
    assert "--db" not in command
    assert "--table" not in command
    assert "--column" not in command


def test_sqlmap_build_command_verbose_uses_v_level():
    """Verbose flag must map to documented -v <level>, not --verbose."""
    args = SqlmapArgs(target="http://example.com", verbose=True)
    command = SqlmapTool().build_command(args)
    assert "--verbose" not in command
    assert "-v" in command
    # ``-v`` must be followed by a numeric level
    idx = command.index("-v")
    assert command[idx + 1].isdigit()


def test_sqlmap_parse_output_json():
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com")
    stdout = '{"data": [{"type": "sql-injection", "parameter": "id", "severity": "3"}]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert "vulnerabilities" in metadata


def test_sqlmap_parse_output_text_fallback():
    """Text parsing triggers as fallback when JSON parsing fails."""
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com")
    stdout = "Vulnerable parameter: id\nDatabase: testdb"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]
    assert "testdb" in metadata.get("databases", []) or metadata["databases"] == []


def test_sqlmap_parse_output_empty():
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_sqlmap_create_artifacts(tmp_path, monkeypatch):
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("output", args, timestamp=123)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_sqlmap_run_success():
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com", level=SqlmapLevel.LEVEL_2, risk=SqlmapRisk.RISK_2)
    stdout = '{"data": [{"type": "sql-injection", "parameter": "id"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert "vulnerabilities" in result.metadata


def test_sqlmap_run_timeout():
    tool = SqlmapTool()
    args = SqlmapArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Nikto
# ---------------------------------------------------------------------------


def test_nikto_build_command_minimal():
    args = NiktoArgs(target="http://example.com")
    command = NiktoTool().build_command(args)
    assert command[0] == "nikto"
    assert "-h" in command


def test_nikto_build_command_with_auth():
    args = NiktoArgs(
        target="http://example.com",
        auth="user:pass",
        cookies="a=b",
        user_agent="ua",
        ssl=True,
    )
    command = NiktoTool().build_command(args)
    assert "-auth" in command and "user:pass" in command
    assert "-cookies" in command and "a=b" in command
    assert "-useragent" in command and "ua" in command
    assert "-ssl" in command


def test_nikto_parse_output_json():
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com", output_format=NiktoOutputFormat.JSON)
    stdout = '{"results": [{"risk": "3", "name": "XSS"}]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_nikto_parse_output_text():
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com", output_format=NiktoOutputFormat.TEXT)
    stdout = "+ OSVDB-12345: Sample Vulnerability"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_nikto_parse_output_empty():
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_nikto_create_artifacts(tmp_path, monkeypatch):
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("nikto output", args, timestamp=321)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_nikto_run_success():
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com", mode=NiktoMode.SCAN)
    stdout = '{"results": [{"risk": "2", "name": "Info leak"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["vulnerabilities"]


def test_nikto_run_timeout():
    tool = NiktoTool()
    args = NiktoArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Wapiti
# ---------------------------------------------------------------------------


def test_wapiti_build_command_minimal():
    args = WapitiArgs(target="http://example.com")
    command = WapitiTool().build_command(args)
    assert command[0] == "wapiti"
    # Target must be passed via ``-u`` per the Wapiti man page.
    assert "-u" in command
    u_idx = command.index("-u")
    assert command[u_idx + 1] == "http://example.com"


def test_wapiti_minimal_uses_format_flag_not_output_path():
    """Default report format must use ``-f`` and never ``-o json`` (man page)."""
    args = WapitiArgs(target="http://example.com")
    command = WapitiTool().build_command(args)
    assert "-f" in command
    f_idx = command.index("-f")
    assert command[f_idx + 1] == "json"
    # ``-o`` only appears when an output path is explicitly provided.
    if "-o" in command:
        idx = command.index("-o")
        assert command[idx + 1] not in {"json", "xml", "html", "txt"}
    else:
        assert "-o" not in command


def test_wapiti_output_path_uses_o_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = WapitiArgs(
        target="http://example.com",
        output_path="reports/wapiti.json",
    )
    tool = WapitiTool()
    command = tool.build_command(args)
    assert "-o" in command
    o_idx = command.index("-o")
    assert command[o_idx + 1] == "/workspace/reports/wapiti.json"
    assert not (tmp_path / "reports").exists()
    workspace_dirs = tool.prepare_workspace_directories(args)
    assert [item.relative_path for item in workspace_dirs] == ["reports"]


def test_wapiti_build_command_with_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = WapitiArgs(
        target="http://example.com",
        auth_method="basic",
        auth_user="user",
        auth_password="pass",
        cookie_file="cookies.json",
        user_agent="example-agent",
    )
    command = WapitiTool().build_command(args)
    assert "--auth-cred" in command and "user%pass" in command
    assert "--auth-type" in command and "basic" in command
    assert "--auth-user" not in command
    assert "--auth-password" not in command
    assert "--auth-method" not in command
    # Cookie file via ``-c`` (file or browser name)
    assert "-c" in command and "/workspace/cookies.json" in command
    # User-agent via documented ``-A``
    assert "-A" in command and "example-agent" in command
    # ``-u`` must remain target-only; never collide with user-agent
    assert command.count("-u") == 1


def test_wapiti_legacy_auth_cred_is_normalized():
    args = WapitiArgs(
        target="http://example.com",
        auth_method="digest",
        auth_cred="legacy:secret",
    )
    command = WapitiTool().build_command(args)
    assert "--auth-cred" in command and "legacy%secret" in command
    assert "--auth-type" in command and "digest" in command
    assert "--auth-user" not in command
    assert "--auth-password" not in command
    assert "--auth-method" not in command


def test_wapiti_legacy_percent_auth_cred_is_normalized():
    args = WapitiArgs(
        target="http://example.com",
        auth_cred="legacy%secret",
    )
    assert args.auth_user == "legacy"
    assert args.auth_password == "secret"


def test_wapiti_path_safety_rejects_absolute_output_path():
    args = WapitiArgs(target="http://example.com", output_path="/tmp/wapiti.json")
    with pytest.raises(ValueError):
        WapitiTool().build_command(args)


def test_wapiti_path_safety_rejects_absolute_cookie_file():
    args = WapitiArgs(target="http://example.com", cookie_file="/tmp/cookies.json")
    with pytest.raises(ValueError):
        WapitiTool().build_command(args)


def test_wapiti_output_schema_values_instantiate():
    assert [fmt.value for fmt in WapitiOutputFormat] == ["json", "xml", "html", "text"]
    for fmt in WapitiOutputFormat:
        args = WapitiArgs(target="http://example.com", output_format=fmt)
        assert args.output_format == fmt


def test_wapiti_tuning_flags_match_man_page():
    args = WapitiArgs(
        target="http://example.com",
        tasks=8,
        timeout=15,
        scan_level=ScanLevel.PARANOID,
    )
    command = WapitiTool().build_command(args)
    # Concurrency via ``--tasks``; ``-t`` must NOT carry the thread count.
    assert "--tasks" in command
    tasks_idx = command.index("--tasks")
    assert command[tasks_idx + 1] == "8"
    # ``-t`` carries the timeout (seconds)
    assert "-t" in command
    t_idx = command.index("-t")
    assert command[t_idx + 1] == "15"
    # Scan force via ``-S``; ``-l`` is not a Wapiti flag.
    assert "-S" in command
    s_idx = command.index("-S")
    assert command[s_idx + 1] == "paranoid"
    assert "-l" not in command


def test_wapiti_no_raw_cookie_field_exposed():
    """Raw cookie strings are not accepted; only cookie files via ``-c``."""
    # ``cookies`` field has been removed; only ``cookie_file`` is exposed.
    assert "cookies" not in WapitiArgs.model_fields
    assert "cookie_file" in WapitiArgs.model_fields


def test_wapiti_parse_output_json():
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com", output_format=WapitiOutputFormat.JSON)
    stdout = '{"vulnerabilities": [{"name": "xss"}], "urls": ["http://example.com"]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_wapiti_parse_output_text():
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com", output_format=WapitiOutputFormat.TEXT)
    stdout = "URLs scanned: 1\nVulnerabilities found: 1\nmodule: xss"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"] == []
    assert metadata.get("modules_used")


def test_wapiti_parse_output_empty():
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_wapiti_create_artifacts(tmp_path, monkeypatch):
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com", output_format=WapitiOutputFormat.JSON)
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("wapiti output", args, timestamp=456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_wapiti_run_success():
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com", scan_level=ScanLevel.PARANOID)
    stdout = '{"vulnerabilities": [{"name": "xss"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["vulnerabilities"]


def test_wapiti_run_timeout():
    tool = WapitiTool()
    args = WapitiArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Skipfish
# ---------------------------------------------------------------------------


def test_skipfish_build_command_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = SkipfishArgs(target="http://example.com")
    tool = SkipfishTool()
    command = tool.build_command(args)
    assert command[0] == "skipfish"
    # ``-o <dir>`` is required by Skipfish; must be present.
    assert "-o" in command
    o_idx = command.index("-o")
    output_dir = command[o_idx + 1]
    # Workspace-safe default lives under ``artifacts/`` and is not a fake fmt.
    assert output_dir.startswith("/workspace/artifacts/skipfish_")
    assert output_dir not in {"json", "xml", "csv"}
    assert "-W" in command
    w_idx = command.index("-W")
    rw_wordlist = command[w_idx + 1]
    assert rw_wordlist.startswith("/workspace/artifacts/skipfish_rw_")
    assert not (tmp_path / rw_wordlist.removeprefix("/workspace/")).exists()
    workspace_files = tool.prepare_workspace_files(args)
    assert len(workspace_files) == 1
    assert workspace_files[0].relative_path == rw_wordlist.removeprefix("/workspace/")
    assert workspace_files[0].content_bytes() == b""
    # Target is the last positional argument.
    assert command[-1] == "http://example.com"


def test_skipfish_no_fake_output_format_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = SkipfishArgs(target="http://example.com")
    command = SkipfishTool().build_command(args)
    # Skipfish does not document JSON/XML/CSV stdout output; values must
    # never appear as ``-o`` operands.
    o_idx = command.index("-o")
    assert command[o_idx + 1] not in {"json", "xml", "csv"}


def test_skipfish_explicit_output_dir_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = SkipfishArgs(
        target="http://example.com",
        output_dir="artifacts/myscan",
    )
    command = SkipfishTool().build_command(args)
    o_idx = command.index("-o")
    assert command[o_idx + 1] == "/workspace/artifacts/myscan"


def test_skipfish_build_command_with_auth():
    args = SkipfishArgs(
        target="http://example.com",
        auth_mode=AuthMode.HTTP,
        auth_user="example-user",
        auth_pass="example-pass",
        cookies="session=abc",
        headers="X-Test: 1",
    )
    command = SkipfishTool().build_command(args)
    # Basic auth uses documented ``-A user:pass`` (Skipfish man page).
    assert "-A" in command and "example-user:example-pass" in command
    # ``-a`` is not a Skipfish flag; never emitted.
    assert "-a" not in command
    # Cookies via ``-C`` and headers via ``-H`` (man page).
    assert "-C" in command and "session=abc" in command
    assert "-H" in command and "X-Test: 1" in command


def test_skipfish_build_command_with_form_auth():
    args = SkipfishArgs(
        target="http://example.com",
        auth_mode=AuthMode.FORM,
        auth_form="http://example.com/login",
        auth_user="example-user",
        auth_pass="example-pass",
    )
    command = SkipfishTool().build_command(args)
    assert "--auth-form" in command and "http://example.com/login" in command
    assert "--auth-user" in command and "example-user" in command
    assert "--auth-pass" in command and "example-pass" in command


def test_skipfish_performance_flags_match_man_page():
    args = SkipfishArgs(
        target="http://example.com",
        max_connections=20,
        request_timeout=15,
        max_time="0:10:00",
    )
    command = SkipfishTool().build_command(args)
    # Concurrency is ``-g``; ``-t`` is request timeout; ``-k`` is stop time.
    assert "-g" in command
    g_idx = command.index("-g")
    assert command[g_idx + 1] == "20"
    assert "-t" in command
    t_idx = command.index("-t")
    assert command[t_idx + 1] == "15"
    assert "-k" in command
    k_idx = command.index("-k")
    assert command[k_idx + 1] == "0:10:00"


def test_skipfish_wordlist_flags_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = SkipfishArgs(
        target="http://example.com",
        rw_wordlist="artifacts/skipfish.wl",
        wordlist="dicts/extra.wl",
    )
    command = SkipfishTool().build_command(args)
    assert "-W" in command and "/workspace/artifacts/skipfish.wl" in command
    assert "-S" in command and "/workspace/dicts/extra.wl" in command
    assert not (tmp_path / "artifacts" / "skipfish.wl").exists()


def test_skipfish_schema_exposes_only_html_output_and_documented_auth_modes():
    assert [fmt.value for fmt in SkipfishOutputFormat] == ["html"]
    assert [mode.value for mode in AuthMode] == ["none", "form", "http"]
    SkipfishArgs(target="http://example.com", output_format=SkipfishOutputFormat.HTML)
    schema = SkipfishArgs.model_json_schema()
    assert "json" not in str(schema)


def test_skipfish_rejects_missing_form_auth_url():
    with pytest.raises(ValidationError):
        SkipfishArgs(target="http://example.com", auth_mode=AuthMode.FORM)


def test_skipfish_parse_output_json_compatible():
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com")
    stdout = '{"vulnerabilities": [{"type": "xss"}], "urls": ["http://example.com"]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_skipfish_parse_output_text():
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com")
    stdout = "URLs scanned: 1\nVulnerabilities found: 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata.get("vulnerabilities_found") == 1 or metadata["vulnerabilities"] == []


def test_skipfish_parse_output_empty():
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["vulnerabilities"] == []


def test_skipfish_create_artifacts(tmp_path, monkeypatch):
    tool = SkipfishTool()
    args = SkipfishArgs(
        target="http://example.com",
        output_dir="artifacts/skipfish_explicit",
    )
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("skipfish output", args, timestamp=789)
    assert artifacts
    # First artifact is the log file; report directory is referenced second.
    assert (tmp_path / artifacts[0]).exists()
    assert "artifacts/skipfish_explicit" in artifacts


def test_skipfish_generated_report_dir_is_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com")
    command = tool.build_command(args)
    report_dir = command[command.index("-o") + 1]
    artifacts = tool.create_artifacts("skipfish output", args, timestamp=789)
    assert report_dir.removeprefix("/workspace/") in artifacts


def test_skipfish_path_safety_rejects_absolute_paths():
    with pytest.raises(ValueError):
        SkipfishTool().build_command(
            SkipfishArgs(target="http://example.com", output_dir="/tmp/skipfish")
        )
    with pytest.raises(ValueError):
        SkipfishTool().build_command(
            SkipfishArgs(target="http://example.com", rw_wordlist="/tmp/rw.wl")
        )


def test_skipfish_allows_system_supplemental_wordlist():
    system_wordlist = "/usr/share/skipfish/dictionaries/minimal.wl"
    command = SkipfishTool().build_command(
        SkipfishArgs(target="http://example.com", wordlist=system_wordlist)
    )
    assert "-S" in command and system_wordlist in command


def test_skipfish_run_success():
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com", max_depth=3)
    stdout = '{"vulnerabilities": [{"type": "issue"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["vulnerabilities"]


def test_skipfish_run_timeout():
    tool = SkipfishTool()
    args = SkipfishArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Nuclei
# ---------------------------------------------------------------------------


def test_nuclei_build_command_minimal():
    args = NucleiArgs(target="http://example.com")
    command = NucleiTool().build_command(args)
    assert command[0] == "nuclei"
    assert "-u" in command and args.target in command
    assert "-jsonl" in command  # default JSON output format


def test_nuclei_build_command_with_templates():
    args = NucleiArgs(
        target="http://example.com",
        templates="cves/",
        exclude_templates="misc/",
        severity="high,critical",
        mode=NucleiMode.SCAN,
    )
    command = NucleiTool().build_command(args)
    assert "-t" in command and "cves/" in command
    assert "-et" in command and "misc/" in command
    assert "-severity" in command and "high,critical" in command
    assert "-jsonl" in command


def test_nuclei_build_command_uses_current_header_and_proxy_flags():
    args = NucleiArgs(
        target="http://example.com",
        user_agent="drowAI-test",
        cookies="session=abc",
        auth="user:pass",
        proxy="http://127.0.0.1:8080",
    )
    command = NucleiTool().build_command(args)

    assert "-user-agent" not in command
    assert "-cookie" not in command
    assert "-auth" not in command
    assert "-proxy" not in command
    assert command.count("-H") == 3
    assert "User-Agent: drowAI-test" in command
    assert "Cookie: session=abc" in command
    assert any(value.startswith("Authorization: Basic ") for value in command)
    assert "-p" in command and "http://127.0.0.1:8080" in command


def test_nuclei_build_command_exposes_current_filter_and_safety_flags():
    args = NucleiArgs(
        target="http://example.com",
        tags="cve,rce",
        exclude_tags="dos,intrusive",
        include_tags="fuzz",
        template_ids="CVE-2024-*",
        exclude_template_ids="deprecated-template",
        protocol_types="http,ssl",
        exclude_protocol_types="headless,code",
        bulk_size=10,
        retries=2,
        rate_limit=50,
    )
    command = NucleiTool().build_command(args)

    assert "-tags" in command and "cve,rce" in command
    assert "-etags" in command and "dos,intrusive" in command
    assert "-itags" in command and "fuzz" in command
    assert "-id" in command and "CVE-2024-*" in command
    assert "-eid" in command and "deprecated-template" in command
    assert "-pt" in command and "http,ssl" in command
    assert "-ept" in command and "headless,code" in command
    assert "-bs" in command and "10" in command
    assert "-retries" in command and "2" in command
    assert "-omit-raw" in command
    assert "-no-color" in command
    assert "-disable-update-check" in command


def test_nuclei_rejects_invalid_filter_values():
    with pytest.raises(ValidationError):
        NucleiArgs(target="http://example.com", severity="important")

    with pytest.raises(ValidationError):
        NucleiArgs(target="http://example.com", protocol_types="http,invalid")


def test_nuclei_build_command_scan_mode_with_report_export():
    args = NucleiArgs(
        target="http://example.com",
        mode=NucleiMode.SCAN,
        report_format=ReportFormat.SARIF,
    )
    command = NucleiTool().build_command(args)
    assert "-jsonl" in command
    assert "-se" in command
    assert "nuclei_report.sarif" in command


def test_nuclei_build_command_non_scan_mode_omits_jsonl():
    args = NucleiArgs(target="http://example.com", mode=NucleiMode.LIST)
    command = NucleiTool().build_command(args)
    assert "-jsonl" not in command
    assert "-tl" in command
    assert "-u" not in command


def test_nuclei_report_format_rejected_outside_scan_mode():
    with pytest.raises(ValidationError):
        NucleiArgs(
            target="http://example.com",
            mode=NucleiMode.VALIDATE,
            report_format=ReportFormat.JSON,
        )


def test_nuclei_parse_output_json():
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    stdout = '[{"template":"cve-2024-0001","severity":"high","matched":"http://example.com"}]'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["results"]
    assert metadata["results"][0]["template"] == "cve-2024-0001"


def test_nuclei_parse_output_empty():
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    # Empty input: JSON parse fails, text fallback produces empty or no results
    assert metadata.get("results", []) == []


def test_nuclei_parse_output_text_fallback_uses_official_result_shape():
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    metadata = tool.parse_output(
        "[CVE-2024-0001] [http] [critical] https://example.com/login (matcher-a)",
        "",
        0,
        args,
    )

    row = metadata["results"][0]
    assert row["template_id"] == "CVE-2024-0001"
    assert row["severity"] == "critical"
    assert row["target_url"] == "https://example.com/login"
    assert row["matcher"] == "matcher-a"


def test_nuclei_create_artifacts(tmp_path, monkeypatch):
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    stdout = '{"template":"cve-2024-0001"}'
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args=args, timestamp=1700000000)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_nuclei_create_artifacts_collects_report_export(tmp_path, monkeypatch):
    tool = NucleiTool()
    args = NucleiArgs(
        target="http://example.com",
        mode=NucleiMode.SCAN,
        report_format=ReportFormat.SARIF,
    )
    stdout = '{"template":"cve-2024-0001"}'
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nuclei_report.sarif").write_text("sarif payload", encoding="utf-8")

    artifacts = tool.create_artifacts(stdout, args=args, timestamp=1700000000)

    assert len(artifacts) == 2
    assert (tmp_path / "artifacts" / "nuclei_report_1700000000.sarif").exists()


def test_nuclei_create_artifacts_non_scan_ignores_stale_report_file(tmp_path, monkeypatch):
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com", mode=NucleiMode.LIST)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nuclei_report.sarif").write_text("stale payload", encoding="utf-8")

    artifacts = tool.create_artifacts("list output", args=args, timestamp=1700000000)

    assert artifacts == ["artifacts/nuclei_list_1700000000.txt"]
    assert (tmp_path / "nuclei_report.sarif").exists()


def test_nuclei_run_success():
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    stdout = '[{"template":"cve-2024-0001","severity":"high"}]'

    def _mock_run(cmd, capture_output, text, timeout):
        assert timeout == args.execution_timeout
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["results"]


def test_nuclei_run_timeout():
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Commix
# ---------------------------------------------------------------------------


def test_commix_build_command_minimal():
    args = CommixArgs(target="http://example.com")
    command = CommixTool().build_command(args)
    assert command[0] == "commix"
    # Target must be passed via documented ``--url``.
    assert "--url" in command
    url_idx = command.index("--url")
    assert command[url_idx + 1] == "http://example.com"


def test_commix_build_command_excludes_fake_output_formats():
    """Commix has no JSON/XML/HTML stdout format; -o <fmt> must not appear."""
    args = CommixArgs(target="http://example.com")
    command = CommixTool().build_command(args)
    assert "-o" not in command
    for fake in ("json", "xml", "html"):
        # ``json``/``xml``/``html`` must not appear as a flag value pair.
        assert fake not in command


def test_commix_build_command_uses_documented_request_flags():
    args = CommixArgs(
        target="http://example.com",
        injection_method=CommixInjectionMethod.POST,
        cookies="a=b",
        user_agent="example-agent",
        exclude_parameters=["session"],
    )
    command = CommixTool().build_command(args)
    # HTTP method via documented ``--method``
    assert "--method" in command
    m_idx = command.index("--method")
    assert command[m_idx + 1] == "POST"
    assert "-i" not in command
    # Cookie via ``--cookie``
    assert "--cookie" in command and "a=b" in command
    # ``-c`` is not Commix's cookie flag (it's --tor-related); must not appear
    assert "-c" not in command
    # User agent via ``--user-agent``
    assert "--user-agent" in command and "example-agent" in command
    # Parameter exclusion via ``--skip``
    assert "--skip" in command and "session" in command
    # No bogus thread flag (Commix has no thread option; -t is traffic log)
    assert "-t" not in command


def test_commix_schema_exposes_only_valid_http_methods():
    assert [method.value for method in CommixInjectionMethod] == [
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "HEAD",
        "OPTIONS",
        "PATCH",
    ]
    for method in CommixInjectionMethod:
        args = CommixArgs(target="http://example.com", injection_method=method)
        assert args.injection_method == method


def test_commix_schema_exposes_only_text_output_format():
    assert [fmt.value for fmt in CommixOutputFormat] == ["text"]
    args = CommixArgs(target="http://example.com", output_format=CommixOutputFormat.TEXT)
    assert args.output_format == CommixOutputFormat.TEXT


def test_commix_parse_output_json_compatible():
    tool = CommixTool()
    args = CommixArgs(target="http://example.com")
    stdout = '{"vulnerabilities": [{"name": "cmdi"}]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_commix_parse_output_text():
    tool = CommixTool()
    args = CommixArgs(target="http://example.com")
    stdout = "Vulnerable parameter: cmd\nParameters tested: 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert "vulnerable_parameters" in metadata or metadata["vulnerabilities"] == []


def test_commix_parse_output_empty():
    tool = CommixTool()
    args = CommixArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata.get("vulnerabilities", []) == []


def test_commix_create_artifacts(tmp_path, monkeypatch):
    tool = CommixTool()
    args = CommixArgs(target="http://example.com")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("commix output", args, timestamp=101112)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_commix_run_success():
    tool = CommixTool()
    args = CommixArgs(target="http://example.com", injection_method=CommixInjectionMethod.POST)
    stdout = '{"vulnerabilities": [{"name": "cmd"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["vulnerabilities"]


def test_commix_run_timeout():
    tool = CommixTool()
    args = CommixArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# XSSer
# ---------------------------------------------------------------------------


def test_xsser_build_command_minimal():
    args = XsserArgs(target="http://example.com")
    command = XsserTool().build_command(args)
    assert command[0] == "xsser"
    # Target via documented ``-u``.
    assert "-u" in command
    u_idx = command.index("-u")
    assert command[u_idx + 1] == "http://example.com"


def test_xsser_build_command_request_flags_match_kali_docs():
    args = XsserArgs(
        target="http://example.com",
        cookie="a=b",
        user_agent="example-agent",
    )
    command = XsserTool().build_command(args)
    # Cookie must use ``--cookie``; ``-c`` is XSSer's crawling depth.
    assert "--cookie" in command and "a=b" in command
    assert "-c" not in command
    # User agent must use ``--user-agent``.
    assert "--user-agent" in command and "example-agent" in command
    # Target ``-u`` must remain target-only.
    assert command.count("-u") == 1


def test_xsser_does_not_emit_fake_output_format():
    args = XsserArgs(target="http://example.com")
    command = XsserTool().build_command(args)
    # No fake ``-o json/xml/html`` flags.
    assert "-o" not in command
    for fake in ("json", "html"):
        assert fake not in command


def test_xsser_xml_report_uses_xml_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    args = XsserArgs(
        target="http://example.com",
        output_format=XsserOutputFormat.XML,
        output_path="reports/xsser.xml",
    )
    tool = XsserTool()
    command = tool.build_command(args)
    # ``--xml=<path>`` is the documented Kali XSSer report flag.
    assert "--xml=/workspace/reports/xsser.xml" in command
    assert "-o" not in command
    assert not (tmp_path / "reports").exists()
    workspace_dirs = tool.prepare_workspace_directories(args)
    assert [item.relative_path for item in workspace_dirs] == ["reports"]


def test_xsser_save_flag_is_explicit():
    args_no_save = XsserArgs(target="http://example.com")
    args_save = XsserArgs(target="http://example.com", save=True)
    assert "--save" not in XsserTool().build_command(args_no_save)
    assert "--save" in XsserTool().build_command(args_save)


def test_xsser_schema_exposes_only_text_and_xml_outputs():
    assert [fmt.value for fmt in XsserOutputFormat] == ["text", "xml"]
    XsserArgs(target="http://example.com", output_format=XsserOutputFormat.TEXT)
    XsserArgs(
        target="http://example.com",
        output_format=XsserOutputFormat.XML,
        output_path="reports/xsser.xml",
    )
    assert "injection_method" not in XsserArgs.model_fields


def test_xsser_rejects_xml_without_output_path():
    with pytest.raises(ValidationError):
        XsserArgs(target="http://example.com", output_format=XsserOutputFormat.XML)


def test_xsser_path_safety_rejects_absolute_output_path():
    args = XsserArgs(
        target="http://example.com",
        output_format=XsserOutputFormat.XML,
        output_path="/tmp/xsser.xml",
    )
    with pytest.raises(ValueError):
        XsserTool().build_command(args)


def test_xsser_parse_output_json_compatible():
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")
    stdout = '{"vulnerabilities": [{"name": "xss"}]}'
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["vulnerabilities"]


def test_xsser_parse_output_text():
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")
    stdout = "Parameters tested: 1\nXSS vulnerabilities found: 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata.get("xss_vulnerabilities_found") == 1 or metadata["vulnerabilities"] == []


def test_xsser_parse_output_empty():
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata.get("vulnerabilities", []) == []


def test_xsser_create_artifacts(tmp_path, monkeypatch):
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("xsser output", args, timestamp=131415)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_xsser_run_success():
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")
    stdout = '{"vulnerabilities": [{"name": "xss"}]}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["vulnerabilities"]


def test_xsser_run_timeout():
    tool = XsserTool()
    args = XsserArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2
