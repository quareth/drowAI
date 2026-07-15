"""Tool-level tests for the fping host liveness tool.

Covers Task 1.1 (default command construction), Task 1.2 (deterministic
parsing of alive hosts, unresponsive counts, and diagnostics), Task 1.3
(compact compressor-facing rendering), and Task 1.4 (semantic
``network.host_discovered`` emission) of the fping host liveness knowledge
implementation guide.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

from agent.tools import FpingTool, validate_and_execute_tool
from agent.tools.information_gathering.network_discovery.fping import (
    FpingArgs,
    FpingTool as FpingToolDirect,
    _extract_alive_hosts,
    _extract_unresponsive_count,
)
from agent.tool_runtime.pty_transport import _include_stderr_in_artifacts
from agent.tool_runtime.result_enrichment import build_pty_tool_result


def test_fping_default_command_omits_c_keeps_alive_only_and_summary():
    """Default discovery command should NOT include `-c` and should keep `-a`."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")

    cmd = tool.build_command(args)

    assert "fping" in cmd[0]
    # Stats-mode (-c) must NOT be enabled by default.
    assert "-c" not in cmd
    # Alive-only output is the default.
    assert "-a" in cmd
    # Summary is preferred so unresponsive_count can be parsed without
    # per-host dead output.
    assert "-s" in cmd
    # Bounded retries are emitted via -r.
    assert "-r" in cmd
    r_index = cmd.index("-r")
    # Value must be a non-negative integer string.
    assert int(cmd[r_index + 1]) >= 0


def test_fping_default_cidr_target_uses_g_sweep():
    """CIDR targets should still use `-g` for fping sweep mode."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")

    cmd = tool.build_command(args)

    assert "-g" in cmd
    g_index = cmd.index("-g")
    assert cmd[g_index + 1] == "172.17.0.1"
    assert cmd[g_index + 2] == "172.17.0.254"


def test_fping_range_end_uses_g_sweep():
    """Explicit range_end should still use `-g` for fping sweep mode."""
    tool = FpingToolDirect()
    args = FpingArgs(target="10.0.0.1", range_end="10.0.0.10")

    cmd = tool.build_command(args)

    assert "-g" in cmd
    g_index = cmd.index("-g")
    assert cmd[g_index + 1] == "10.0.0.1"
    assert cmd[g_index + 2] == "10.0.0.10"


def test_fping_count_only_emitted_when_explicitly_provided():
    """`-c` should appear only when caller explicitly sets `count`."""
    tool = FpingToolDirect()

    default_cmd = tool.build_command(FpingArgs(target="127.0.0.1"))
    assert "-c" not in default_cmd

    explicit_cmd = tool.build_command(FpingArgs(target="127.0.0.1", count=2))
    assert "-c" in explicit_cmd
    c_index = explicit_cmd.index("-c")
    assert explicit_cmd[c_index + 1] == "2"


def test_fping_success_exit_codes_zero_and_one():
    """Exit codes 0 and 1 represent success unless hard CLI failure is present."""
    tool = FpingToolDirect()
    args = FpingArgs(target="127.0.0.1")

    assert tool.is_success_exit_code(0, args) is True
    assert tool.is_success_exit_code(1, args, stdout="172.17.0.2 is unreachable\n") is True
    assert tool.is_success_exit_code(2, args) is False
    assert (
        tool.is_success_exit_code(
            1,
            args,
            stderr="fping: can't parse address 172.0.0.0/24\n",
        )
        is False
    )


def test_fping_tool_execution(monkeypatch):
    """End-to-end execution still succeeds against a stubbed fping run."""

    def fake_run(cmd, capture_output, text, timeout):
        output = "127.0.0.1 : xmt/rcv/%loss = 1/1/0%"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(FpingTool(), {"target": "127.0.0.1"})
    assert result.success
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Task 1.2: parser helpers (`_extract_alive_hosts`, `_extract_unresponsive_count`)
# and the resulting `parse_output()` metadata contract.
# ---------------------------------------------------------------------------


def test_extract_alive_hosts_terse_single_line():
    """A terse alive line (`172.17.0.1`) becomes one alive host."""
    assert _extract_alive_hosts("172.17.0.1\n", "") == ["172.17.0.1"]


def test_extract_alive_hosts_stats_rcv_positive_is_alive():
    """`xmt/rcv/%loss = 1/1/0%` is a stats-mode alive host."""
    stats_line = "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164"
    assert _extract_alive_hosts(stats_line + "\n", "") == ["172.17.0.1"]


def test_extract_alive_hosts_stats_rcv_zero_is_not_alive():
    """`xmt/rcv/%loss = 1/0/100%` must NOT become an alive host."""
    stats_line = "172.17.0.247 : xmt/rcv/%loss = 1/0/100%"
    assert _extract_alive_hosts(stats_line + "\n", "") == []


def test_extract_alive_hosts_dedupes_repeats_across_streams():
    """A host that appears in both terse and stats output is deduped."""
    stdout = "172.17.0.1\n"
    stderr = (
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "172.17.0.1\n"
    )
    assert _extract_alive_hosts(stdout, stderr) == ["172.17.0.1"]


def test_extract_alive_hosts_ignores_diagnostic_icmp_unreachable():
    """`ICMP Host Unreachable ...` is diagnostics-only, never alive."""
    diagnostic = (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70"
    )
    assert _extract_alive_hosts("", diagnostic + "\n") == []


def test_extract_alive_hosts_ignores_summary_unreachable_count_line():
    """A summary `2 unreachable` line must not be misread as a host token."""
    output = "       3 targets\n       1 alive\n       2 unreachable\n"
    assert _extract_alive_hosts(output, "") == []


def test_extract_alive_hosts_ignores_progress_dot_lines():
    """Progress marker dot lines are not host identities."""
    output = "172.17.0.1\n.\n172.17.0.2\n"
    assert _extract_alive_hosts(output, "") == ["172.17.0.1", "172.17.0.2"]


def test_extract_unresponsive_count_prefers_summary_line():
    """`N unreachable` is the preferred source for unresponsive_count."""
    summary = "       3 targets\n       1 alive\n       2 unreachable\n"
    # Stats lines also present — summary still wins.
    stats = "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
    assert _extract_unresponsive_count("", summary + stats) == 2


def test_extract_unresponsive_count_falls_back_to_dead_stats_when_no_summary():
    """Stats `rcv=0` rows count toward unresponsive when no summary is present."""
    stats = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.1/0.1/0.1\n"
    )
    assert _extract_unresponsive_count("", stats) == 2


def test_extract_unresponsive_count_terse_alive_only_returns_none():
    """Terse alive-only output must not guess from range size."""
    assert _extract_unresponsive_count("172.17.0.1\n", "") is None


def test_extract_unresponsive_count_diagnostic_only_returns_none():
    """A diagnostic ICMP error alone is not summary or stats evidence."""
    diagnostic = (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70\n"
    )
    assert _extract_unresponsive_count("", diagnostic) is None


def test_parse_output_metadata_contract_summary_and_stats():
    """parse_output exposes alive/unresponsive/diagnostics per the MVP contract."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stdout = "172.17.0.1\n"
    stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "       3 targets\n       1 alive\n       2 unreachable\n"
    )

    metadata = tool.parse_output(stdout=stdout, stderr=stderr, exit_code=1, args=args)

    assert metadata["alive_hosts"] == ["172.17.0.1"]
    assert metadata["compact_key_findings"] == ["172.17.0.1"]
    assert metadata["alive_count"] == 1
    assert metadata["unresponsive_count"] == 2
    assert metadata["diagnostics"] == [
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70"
    ]
    assert metadata["exit_code"] == 1


def test_parse_output_terse_alive_only_omits_unresponsive_count():
    """Terse alive-only output must not pin unresponsive_count to 0."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")

    metadata = tool.parse_output(stdout="172.17.0.1\n", stderr="", exit_code=1, args=args)

    assert metadata["alive_hosts"] == ["172.17.0.1"]
    assert metadata["alive_count"] == 1
    # unresponsive_count must be omitted (or None) — never silently zeroed.
    assert metadata.get("unresponsive_count") is None
    assert "unresponsive_count" not in metadata
    assert metadata["diagnostics"] == []


def test_parse_output_stats_only_uses_dead_rows_for_unresponsive_count():
    """Without a summary line, stats rcv=0 rows feed unresponsive_count."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stderr = (
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
    )

    metadata = tool.parse_output(stdout="", stderr=stderr, exit_code=1, args=args)

    assert metadata["alive_hosts"] == ["172.17.0.1"]
    assert metadata["alive_count"] == 1
    assert metadata["unresponsive_count"] == 2


# ---------------------------------------------------------------------------
# Task 1.3: compact result rendering (`render_result_output`) and run() wiring.
# ---------------------------------------------------------------------------


def test_render_result_output_summary_with_alive_and_unresponsive():
    """Alive hosts present + parsable unresponsive count renders both lines + IPs."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stdout = "172.17.0.1\n"
    stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "       3 targets\n       1 alive\n       2 unreachable\n"
    )

    rendered_stdout, rendered_stderr = tool.render_result_output(
        args=args, stdout=stdout, stderr=stderr
    )

    lines = rendered_stdout.splitlines()
    assert lines[0] == "Alive hosts: 1"
    assert lines[1] == "Unresponsive hosts: 2"
    # Alive IP must be listed.
    assert "172.17.0.1" in lines
    # Stderr is cleared in the compact view to avoid re-importing fping noise.
    assert rendered_stderr == ""


def test_render_result_output_no_alive_no_summary_marks_unknown():
    """No alive hosts and no summary/stats evidence renders explicit unknown."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")

    rendered_stdout, _ = tool.render_result_output(args=args, stdout="", stderr="")

    lines = rendered_stdout.splitlines()
    assert lines[0] == "Alive hosts: 0"
    assert lines[1] == "Unresponsive hosts: unknown"
    # No alive IP list when there are zero alive hosts.
    assert len(lines) == 2


def test_render_result_output_does_not_include_dead_stats_lines():
    """Compact output must NOT include every per-host dead stats line."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.249 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
    )

    rendered_stdout, _ = tool.render_result_output(args=args, stdout="", stderr=stderr)

    # The exact dead-host stats text must not leak into compact output.
    assert "xmt/rcv/%loss" not in rendered_stdout
    assert "172.17.0.247" not in rendered_stdout
    assert "172.17.0.248" not in rendered_stdout
    assert "172.17.0.249" not in rendered_stdout
    # Alive host count and IP are still present.
    assert "Alive hosts: 1" in rendered_stdout
    assert "172.17.0.1" in rendered_stdout
    # Dead stats lines feed unresponsive_count (no summary line was provided).
    assert "Unresponsive hosts: 3" in rendered_stdout


def test_render_result_output_renders_diagnostics_block():
    """Diagnostic ICMP unreachable lines render in a bounded `Diagnostics:` block."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stderr = (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
    )

    rendered_stdout, _ = tool.render_result_output(args=args, stdout="", stderr=stderr)

    assert "Diagnostics:" in rendered_stdout
    assert (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70"
        in rendered_stdout
    )


def test_run_uses_renderer_for_stdout_while_artifact_keeps_raw_output(
    monkeypatch, tmp_path
):
    """run() must wire compact stdout through the renderer; artifact gets raw text."""
    raw_stdout = "172.17.0.1\n"
    raw_stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164\n"
        "       3 targets\n       1 alive\n       2 unreachable\n"
    )

    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, raw_stdout, raw_stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Run from a tmp dir so the artifact lands somewhere predictable.
    monkeypatch.chdir(tmp_path)

    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    result = tool.run(args)

    # Compact stdout was produced by the renderer.
    assert "Alive hosts: 1" in result.stdout
    assert "Unresponsive hosts: 2" in result.stdout
    assert "172.17.0.1" in result.stdout
    # Raw fping noise must NOT have leaked into ToolResult.stdout.
    assert "xmt/rcv/%loss" not in result.stdout
    assert "172.17.0.247" not in result.stdout
    # Compact stderr is intentionally cleared.
    assert result.stderr == ""

    # Artifact still contains the raw stdout/stderr for audit.
    assert result.artifacts, "expected at least one artifact path"
    artifact_path = result.artifacts[0]
    saved = open(artifact_path, "r", encoding="utf-8").read()
    assert "xmt/rcv/%loss" in saved
    assert "172.17.0.247" in saved
    assert "172.17.0.1" in saved


def test_pty_fping_artifact_preserves_raw_stderr(tmp_path):
    """PTY fping artifacts must include raw stderr summary/stats evidence."""
    raw_stdout = "172.17.0.1\n"
    raw_stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "       2 targets\n       1 alive\n       1 unreachable\n"
    )
    shell_result = SimpleNamespace(
        stdout=raw_stdout,
        stderr=raw_stderr,
        exit_code=1,
        status="success",
    )

    result = build_pty_tool_result(
        tool=FpingToolDirect(),
        args=FpingArgs(target="172.17.0.0/24"),
        shell_result=shell_result,
        command="fping -a -s -r 1 -p 1000 -g 172.17.0.0/24",
        host_workspace_path=str(tmp_path),
        include_stderr_in_artifacts=_include_stderr_in_artifacts(
            "information_gathering.network_discovery.fping"
        ),
    )

    assert result.artifacts, "expected fping artifact path"
    saved = Path(tmp_path, result.artifacts[0]).read_text(encoding="utf-8")
    assert "172.17.0.1" in saved
    assert "xmt/rcv/%loss" in saved
    assert "1 unreachable" in saved
    assert "xmt/rcv/%loss" not in result.stdout


def test_pty_fping_parse_error_marks_run_failed(tmp_path):
    """Hard CLI failures must fail even when fping uses informational exit code 1."""
    shell_result = SimpleNamespace(
        stdout="",
        stderr="fping: can't parse address 172.0.0.0/24\n",
        exit_code=1,
        status="success",
    )

    result = build_pty_tool_result(
        tool=FpingToolDirect(),
        args=FpingArgs(target="172.0.0.0/24"),
        shell_result=shell_result,
        command="fping -a -s -r 1 -p 1000 -g 172.0.0.0/24",
        host_workspace_path=str(tmp_path),
    )

    assert result.success is False
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Task 1.4: emit_semantic_observations() — one network.host_discovered per
# unique alive IP, IP-only, no service/finding observations, no dead hosts.
# ---------------------------------------------------------------------------


def _emit_with_metadata(metadata: dict, args: FpingArgs | None = None):
    """Helper: invoke the emitter with arbitrary metadata, ignoring stdout/stderr."""
    tool = FpingToolDirect()
    args = args or FpingArgs(target="172.17.0.0/24")
    return tool.emit_semantic_observations(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )


def test_emit_semantic_observations_one_per_unique_alive_ip():
    """One semantic observation per unique alive IP, exact contract shape."""
    obs = _emit_with_metadata({"alive_hosts": ["172.17.0.1", "10.0.0.5"]})

    assert len(obs) == 2
    # Both observations match the canonical contract exactly.
    for entry in obs:
        assert entry["observation_type"] == "network.host_discovered"
        assert entry["subject_type"] == "host.ip"
        assert entry["payload"] == {
            "source": "fping",
            "host_status": "up",
            "probe_protocol": "icmp",
        }

    subject_keys = sorted(entry["subject_key"] for entry in obs)
    assert subject_keys == ["host.ip:10.0.0.5", "host.ip:172.17.0.1"]


def test_emit_semantic_observations_subject_key_uses_host_ip_prefix():
    """Subject key must be the canonical ``host.ip:<ip>`` shape used by nmap/masscan."""
    obs = _emit_with_metadata({"alive_hosts": ["172.17.0.1"]})

    assert len(obs) == 1
    assert obs[0]["subject_key"] == "host.ip:172.17.0.1"


def test_emit_semantic_observations_empty_alive_hosts_emits_nothing():
    """No alive hosts means no observations — never a placeholder row."""
    assert _emit_with_metadata({"alive_hosts": []}) == []
    # Missing key behaves the same as an empty list.
    assert _emit_with_metadata({}) == []


def test_emit_semantic_observations_skips_hostnames_in_mvp():
    """Hostnames in alive_hosts are skipped (host.dns not emitted in MVP)."""
    obs = _emit_with_metadata(
        {"alive_hosts": ["scanme.example.com", "172.17.0.1"]}
    )

    assert len(obs) == 1
    assert obs[0]["subject_key"] == "host.ip:172.17.0.1"


def test_emit_semantic_observations_no_observations_for_dead_hosts():
    """Dead-only fping output (parser-stripped) yields zero host observations."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")

    # All-dead stats output: parser drops these from alive_hosts. Run the
    # parser and feed its real metadata into the emitter, end-to-end.
    stderr = (
        "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
        "172.17.0.248 : xmt/rcv/%loss = 1/0/100%\n"
    )
    metadata = tool.parse_output(stdout="", stderr=stderr, exit_code=1, args=args)
    assert metadata["alive_hosts"] == []
    assert metadata["unresponsive_count"] == 2

    assert tool.emit_semantic_observations(
        stdout="", stderr=stderr, exit_code=1, args=args, metadata=metadata
    ) == []


def test_emit_semantic_observations_ignores_diagnostic_only_runs():
    """Diagnostic ICMP unreachables alone do not produce host observations."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stderr = (
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70\n"
    )
    metadata = tool.parse_output(stdout="", stderr=stderr, exit_code=1, args=args)

    # Diagnostics are captured but never become alive hosts.
    assert metadata["alive_hosts"] == []
    assert metadata["diagnostics"] == [
        "ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70"
    ]

    assert tool.emit_semantic_observations(
        stdout="", stderr=stderr, exit_code=1, args=args, metadata=metadata
    ) == []


def test_emit_semantic_observations_ignores_summary_unreachable_count():
    """A `2 unreachable` summary alone yields no host observations."""
    tool = FpingToolDirect()
    args = FpingArgs(target="172.17.0.0/24")
    stderr = "       3 targets\n       1 alive\n       2 unreachable\n"
    metadata = tool.parse_output(stdout="", stderr=stderr, exit_code=1, args=args)

    # Summary feeds unresponsive_count but cannot produce host observations.
    assert metadata["alive_hosts"] == []
    assert metadata["unresponsive_count"] == 2

    assert tool.emit_semantic_observations(
        stdout="", stderr=stderr, exit_code=1, args=args, metadata=metadata
    ) == []


def test_emit_semantic_observations_dedupes_repeated_alive_entries():
    """Duplicate alive entries collapse to a single observation."""
    # The parser already dedupes, but the emitter must defend its own contract
    # in case another caller hands it un-deduped metadata directly.
    obs = _emit_with_metadata(
        {"alive_hosts": ["172.17.0.1", "172.17.0.1", "  172.17.0.1  "]}
    )

    assert len(obs) == 1
    assert obs[0]["subject_key"] == "host.ip:172.17.0.1"


def test_emit_semantic_observations_does_not_emit_other_observation_types():
    """Only network.host_discovered is allowed — no ports/services/findings."""
    obs = _emit_with_metadata(
        {
            "alive_hosts": ["172.17.0.1"],
            # Even if other keys appear in metadata, the emitter must ignore them.
            "unresponsive_count": 5,
            "diagnostics": ["ICMP Host Unreachable from 172.17.0.4 ..."],
        }
    )

    assert len(obs) == 1
    types = {entry["observation_type"] for entry in obs}
    assert types == {"network.host_discovered"}


def test_emit_semantic_observations_ignores_invalid_ip_tokens():
    """Garbage tokens that pass the parser regex but fail IP parsing are dropped."""
    # The terse-line regex is permissive; the emitter is the IP-validation seam.
    obs = _emit_with_metadata(
        {"alive_hosts": ["not.a.real.ipv4", "999.999.999.999", "172.17.0.1"]}
    )

    assert len(obs) == 1
    assert obs[0]["subject_key"] == "host.ip:172.17.0.1"
