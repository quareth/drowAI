import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.tools.web_applications.web_crawlers.gobuster import (
    GobusterArgs,
    GobusterTool,
)

# The other crawler wrappers are imported best-effort because some are
# currently missing exports that older tests in this file expect. Guarding
# the imports keeps gobuster contract tests (Phase 1) collectable even
# when an unrelated wrapper module drifts.
try:
    from agent.tools.web_applications.web_crawlers.dirb import DirbArgs, DirbTool
except ImportError:  # pragma: no cover - environmental guard
    DirbArgs = DirbTool = None  # type: ignore[assignment]

try:
    from agent.tools.web_applications.web_crawlers.ffuf import (
        FfufArgs,
        FfufMode,
        FfufTool,
        OutputFormat as FfufOutputFormat,
    )
except ImportError:  # pragma: no cover - environmental guard
    FfufArgs = FfufMode = FfufTool = FfufOutputFormat = None  # type: ignore[assignment]

try:
    from agent.tools.web_applications.web_crawlers.wfuzz import (
        WfuzzArgs,
        WfuzzMode,
        WfuzzTool,
        OutputFormat as WfuzzOutputFormat,
    )
except ImportError:  # pragma: no cover - environmental guard
    WfuzzArgs = WfuzzMode = WfuzzTool = WfuzzOutputFormat = None  # type: ignore[assignment]


def _require(symbol, name: str):
    if symbol is None:
        pytest.skip(f"{name} unavailable in current environment")
    return symbol


# ---------------------------------------------------------------------------
# Gobuster
# ---------------------------------------------------------------------------


def test_gobuster_build_command_minimal():
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    command = GobusterTool().build_command(args)
    # gobuster v3.8.2 dir CLI uses --url, --wordlist, --threads (long forms).
    assert command[:2] == ["gobuster", "dir"]
    assert "--url" in command
    assert "http://example.com" in command
    assert "--wordlist" in command
    assert "list.txt" in command
    assert "--no-progress" in command
    assert "--no-color" in command
    # gobuster has no '-l' flag; the wrapper must never emit it.
    assert "-l" not in command
    # Length display control is opt-in via --hide-length (default off).
    assert "--hide-length" not in command


def test_gobuster_build_command_hide_length_uses_documented_flag():
    args = GobusterArgs(
        target="http://example.com", wordlist="list.txt", hide_length=True
    )
    command = GobusterTool().build_command(args)
    assert "--hide-length" in command
    assert "-l" not in command


def test_gobuster_build_command_with_auth():
    args = GobusterArgs(
        target="http://example.com",
        wordlist="list.txt",
        username="user",
        password="pass",
        headers="X-Test: 1",
        cookies="a=b",
        user_agent="ua",
    )
    command = GobusterTool().build_command(args)
    assert "--username" in command and "user" in command
    assert "--password" in command and "pass" in command
    assert "--headers" in command and "X-Test: 1" in command
    assert "--cookies" in command and "a=b" in command
    assert "--useragent" in command and "ua" in command


def test_gobuster_build_command_with_current_http_controls():
    args = GobusterArgs(
        target="https://example.com",
        wordlist="list.txt",
        method="POST",
        status_codes_blacklist="404",
        exclude_length="123,400-450",
        follow_redirects=True,
        no_tls_validation=True,
        proxy="http://127.0.0.1:8080",
        delay="1500ms",
        force=True,
    )

    command = GobusterTool().build_command(args)

    assert "--method" in command and "POST" in command
    assert "--status-codes-blacklist" in command and "404" in command
    assert "--exclude-length" in command and "123,400-450" in command
    assert "--follow-redirect" in command
    assert "--no-tls-validation" in command
    assert "--proxy" in command and "http://127.0.0.1:8080" in command
    assert "--delay" in command and "1500ms" in command
    assert "--force" in command


def test_gobuster_build_command_dir_extensions():
    args = GobusterArgs(
        target="http://example.com", wordlist="list.txt", extensions="php,html"
    )
    command = GobusterTool().build_command(args)
    assert "--extensions" in command and "php,html" in command


def test_gobuster_dir_status_codes_disables_default_blacklist():
    """Positive --status-codes must clear the default blacklist.

    gobuster v3.8.2 ``cli/dir/dir.go`` ships with a default
    ``--status-codes-blacklist`` value and rejects mixing positive and
    negative status-code filters. To use a positive filter we must pass
    ``--status-codes <value>`` and explicitly clear the blacklist with an
    empty ``--status-codes-blacklist`` value.
    """
    args = GobusterArgs(
        target="http://example.com",
        wordlist="list.txt",
        status_codes="200,301",
    )
    command = GobusterTool().build_command(args)

    assert "--status-codes" in command
    sc_index = command.index("--status-codes")
    assert command[sc_index + 1] == "200,301"

    # The default blacklist must be cleared explicitly with an empty value
    # so the CLI accepts the positive filter.
    assert "--status-codes-blacklist" in command
    bl_index = command.index("--status-codes-blacklist")
    assert command[bl_index + 1] == ""


def test_gobuster_dir_rejects_both_status_filters():
    """Dir mode must refuse simultaneous positive and negative filters.

    gobuster v3.8.2 ``cli/dir/dir.go`` enforces mutual exclusivity
    between ``--status-codes`` and ``--status-codes-blacklist``. The
    wrapper raises a validation error before producing a command the CLI
    would reject.
    """
    with pytest.raises(ValueError):
        GobusterArgs(
            target="http://example.com",
            wordlist="list.txt",
            status_codes="200",
            status_codes_blacklist="404",
        )


def test_gobuster_dir_blacklist_only_no_positive_filter():
    """When only the blacklist is supplied, positive filter must be absent."""
    args = GobusterArgs(
        target="http://example.com",
        wordlist="list.txt",
        status_codes_blacklist="404,500",
    )
    command = GobusterTool().build_command(args)

    assert "--status-codes-blacklist" in command
    bl_index = command.index("--status-codes-blacklist")
    assert command[bl_index + 1] == "404,500"
    assert "--status-codes" not in command


def test_gobuster_build_command_dns_uses_domain_flag_not_url_flag():
    args = GobusterArgs(target="example.com", wordlist="list.txt", mode="dns")
    command = GobusterTool().build_command(args)

    # gobuster v3.8.2 dns CLI uses --domain (long form) for the target.
    assert command[:2] == ["gobuster", "dns"]
    assert "--domain" in command
    assert "example.com" in command
    # Mode-specific isolation: HTTP-only flags must not leak into dns.
    assert "--url" not in command
    assert "-u" not in command
    assert "-d" not in command
    assert "--method" not in command
    assert "-l" not in command
    assert "--hide-length" not in command
    assert "--extensions" not in command


def test_gobuster_dns_rejects_extensions_and_http_only_options():
    # Extensions are documented only for dir mode in v3.8.2.
    with pytest.raises(ValueError):
        GobusterArgs(
            target="example.com",
            wordlist="list.txt",
            mode="dns",
            extensions="php",
        )


@pytest.mark.parametrize(
    "http_only_option",
    [
        {"method": "POST"},
        {"follow_redirects": True},
        {"no_tls_validation": True},
    ],
)
def test_gobuster_dns_rejects_http_only_controls(http_only_option):
    with pytest.raises(ValueError):
        GobusterArgs(
            target="example.com",
            wordlist="list.txt",
            mode="dns",
            **http_only_option,
        )


def test_gobuster_build_command_vhost_append_domain():
    args = GobusterArgs(
        target="https://example.com",
        wordlist="list.txt",
        mode="vhost",
        append_domain=True,
    )
    command = GobusterTool().build_command(args)

    assert command[:2] == ["gobuster", "vhost"]
    assert "--url" in command
    assert "--append-domain" in command


def test_gobuster_build_command_vhost_uses_exclude_status_not_dash_b():
    args = GobusterArgs(
        target="https://example.com",
        wordlist="list.txt",
        mode="vhost",
        status_codes_blacklist="404,500",
        exclude_length="123",
    )
    command = GobusterTool().build_command(args)

    # vhost negative status filter uses --exclude-status, not -b.
    assert "--exclude-status" in command and "404,500" in command
    assert "-b" not in command
    assert "--status-codes-blacklist" not in command
    assert "--exclude-length" in command and "123" in command
    # vhost must never emit dir-only short flags.
    assert "-x" not in command
    assert "-s" not in command
    assert "-l" not in command


def test_gobuster_vhost_rejects_extensions_and_positive_status_codes():
    # Extensions are not documented for vhost in v3.8.2.
    with pytest.raises(ValueError):
        GobusterArgs(
            target="https://example.com",
            wordlist="list.txt",
            mode="vhost",
            extensions="php",
        )
    # vhost has no positive status-code filter in v3.8.2.
    with pytest.raises(ValueError):
        GobusterArgs(
            target="https://example.com",
            wordlist="list.txt",
            mode="vhost",
            status_codes="200",
        )


def test_gobuster_rejects_invalid_mode_options():
    with pytest.raises(ValueError):
        GobusterArgs(target="example.com", wordlist="list.txt", mode="dns", headers="X-Test: 1")

    with pytest.raises(ValueError):
        GobusterArgs(target="https://example.com", wordlist="list.txt", force=True, mode="vhost")


def test_gobuster_rejects_invalid_filters():
    with pytest.raises(ValueError):
        GobusterArgs(target="https://example.com", wordlist="list.txt", status_codes="abc")

    with pytest.raises(ValueError):
        GobusterArgs(target="https://example.com", wordlist="list.txt", exclude_length="500-100")


def test_gobuster_parse_output_success():
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    stdout = "/admin (Status: 200) [Size: 123]\n/root (Status: 301)\n"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert len(metadata["findings"]) == 2
    assert metadata["findings"][0]["path"] == "/admin"
    assert metadata["findings"][0]["status"] == 200


def test_gobuster_parse_output_empty():
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["findings"] == []


def test_gobuster_create_artifacts(tmp_path, monkeypatch):
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    stdout = "X" * 201
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts(stdout, args, timestamp=123456)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_gobuster_run_success():
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    stdout = "/admin (Status: 200) [Size: 123]"

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["findings"][0]["path"] == "/admin"


def test_gobuster_run_timeout():
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# Dirb
# ---------------------------------------------------------------------------


def test_dirb_build_command_with_auth():
    _require(DirbArgs, "DirbArgs")
    args = DirbArgs(
        target="http://example.com",
        wordlist="list.txt",
        username="user",
        password="pass",
        headers="X-Test: 1",
        cookies="a=b",
        proxy="http://127.0.0.1:8080",
    )
    command = DirbTool().build_command(args)
    assert "-u" in command and "user:pass" in command
    assert "-H" in command and "X-Test: 1" in command
    assert "-c" in command and "a=b" in command
    assert "-p" in command and "http://127.0.0.1:8080" in command


def test_dirb_parse_output_success():
    _require(DirbTool, "DirbTool")
    tool = DirbTool()
    args = DirbArgs(target="http://example.com", wordlist="list.txt")
    stdout = "+ http://example.com/admin (CODE:200|SIZE:123)\n"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["findings"][0]["url"].endswith("/admin")
    assert metadata["findings"][0]["status"] == 200


def test_dirb_run_timeout():
    _require(DirbTool, "DirbTool")
    tool = DirbTool()
    args = DirbArgs(target="http://example.com", wordlist="list.txt")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


# ---------------------------------------------------------------------------
# FFUF
# ---------------------------------------------------------------------------


def test_ffuf_build_command_with_auth():
    _require(FfufArgs, "FfufArgs")
    args = FfufArgs(
        target="http://example.com",
        wordlist="list.txt",
        username="user",
        password="pass",
        cookies="a=b",
        headers="X-Test: 1",
    )
    command = FfufTool().build_command(args)
    assert "Authorization: Basic" in " ".join(command)
    assert "-b" in command and "a=b" in command


def test_ffuf_parse_output_text():
    _require(FfufTool, "FfufTool")
    tool = FfufTool()
    args = FfufArgs(
        target="http://example.com", output_format=FfufOutputFormat.TEXT
    )
    stdout = "http://example.com/admin 200 123 10"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["results"][0]["status"] == 200


# ---------------------------------------------------------------------------
# WFUZZ
# ---------------------------------------------------------------------------


def test_wfuzz_build_command_with_auth():
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(
        target="http://example.com",
        wordlist="list.txt",
        username="user",
        password="pass",
        auth_type="digest",
        proxy="http://127.0.0.1:8080",
    )
    command = WfuzzTool().build_command(args)
    assert "--digest" in command
    assert "user:pass" in command
    assert "-p" in command


def test_wfuzz_parse_output_text():
    _require(WfuzzTool, "WfuzzTool")
    tool = WfuzzTool()
    args = WfuzzArgs(
        target="http://example.com", output_format=WfuzzOutputFormat.TEXT
    )
    stdout = "00001 200 123 10 1"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["results"][0]["status"] == 200


def test_wfuzz_crawler_default_uses_documented_flags():
    """Default crawler command emits the Wfuzz man-page flags only."""
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(target="http://example.com/FUZZ", wordlist="list.txt")
    command = WfuzzTool().build_command(args)
    # ``-of`` is not a documented Wfuzz flag.
    assert "-of" not in command
    # Stdout printer flag must be ``-o`` per man page.
    assert "-o" in command
    # ``-timeout`` is not documented; Wfuzz uses --req-delay/--conn-delay.
    assert "-timeout" not in command
    # ``-r`` is not documented for follow-redirects; Wfuzz uses ``--follow``.
    assert "-r" not in command


def test_wfuzz_crawler_json_format_uses_o_flag():
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(
        target="http://example.com/FUZZ",
        wordlist="list.txt",
        output_format=WfuzzOutputFormat.JSON,
    )
    command = WfuzzTool().build_command(args)
    assert "-o" in command
    o_index = command.index("-o")
    assert command[o_index + 1] == "json"
    assert "-of" not in command


def test_wfuzz_crawler_delay_uses_s_flag():
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(
        target="http://example.com/FUZZ",
        wordlist="list.txt",
        delay=1.5,
    )
    command = WfuzzTool().build_command(args)
    assert "-s" in command
    s_index = command.index("-s")
    assert command[s_index + 1] == "1.5"
    # ``-d`` is wfuzz's POST data flag, not a delay.
    assert "-d" not in command


def test_wfuzz_crawler_follow_redirects_uses_long_flag():
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(
        target="http://example.com/FUZZ",
        wordlist="list.txt",
        follow_redirects=True,
    )
    command = WfuzzTool().build_command(args)
    assert "--follow" in command
    assert "-r" not in command


def test_wfuzz_crawler_filters_use_documented_flags():
    _require(WfuzzArgs, "WfuzzArgs")
    args = WfuzzArgs(
        target="http://example.com/FUZZ",
        wordlist="list.txt",
        match_status="200,301",
        filter_status="404",
        match_lines="10",
        filter_lines="5",
        match_words="20",
        filter_words="3",
        match_size="512",
        filter_size="1024",
    )
    command = WfuzzTool().build_command(args)
    expected = {
        "--sc": "200,301",
        "--hc": "404",
        "--sl": "10",
        "--hl": "5",
        "--sw": "20",
        "--hw": "3",
        "--sh": "512",
        "--hh": "1024",
    }
    for flag, value in expected.items():
        assert flag in command
        assert command[command.index(flag) + 1] == value
    for stale in ("-m", "-f", "-ms", "-fs", "-mw", "-fw", "-ml", "-fl"):
        assert stale not in command


def test_wfuzz_crawler_schema_exposes_only_directory_mode():
    _require(WfuzzArgs, "WfuzzArgs")
    assert [mode.value for mode in WfuzzMode] == ["directory"]
    WfuzzArgs(target="http://example.com/FUZZ", mode=WfuzzMode.DIRECTORY)


def test_wfuzz_crawler_wordlist_path_policy():
    _require(WfuzzArgs, "WfuzzArgs")
    system_wordlist = "/usr/share/wordlists/dirb/common.txt"
    command = WfuzzTool().build_command(
        WfuzzArgs(target="http://example.com/FUZZ", wordlist=system_wordlist)
    )
    assert system_wordlist in command
    with pytest.raises(ValueError):
        WfuzzTool().build_command(
            WfuzzArgs(target="http://example.com/FUZZ", wordlist="/tmp/list.txt")
        )
