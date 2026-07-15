"""Pure fping host-liveness analysis helpers.

This module owns fping output normalization for tool metadata, compact
rendering, and host-discovered semantic facts. It performs no runtime calls,
filesystem reads, backend imports, compression work, or LLM behavior.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


# Maximum number of diagnostic lines we retain so metadata remains bounded
# regardless of how noisy a particular fping run was.
_MAX_DIAGNOSTIC_LINES = 50

# Cap diagnostics in the compressor-facing render so a noisy run cannot blow
# the compact summary back up to the size of the raw output.
_MAX_RENDERED_DIAGNOSTIC_LINES = 10

# Terse alive line: a single hostname or IP on its own line (e.g. `172.17.0.1`).
# Restricted to characters that appear in IPv4, IPv6, and DNS hostnames.
_TERSE_ALIVE_LINE_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")

# Stats-mode per-host line, e.g.
#   `172.17.0.1   : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.164/0.164/0.164`
#   `172.17.0.247 : xmt/rcv/%loss = 1/0/100%`
_STATS_LINE_RE = re.compile(
    r"^(?P<host>[A-Za-z0-9_.:\-]+)\s*:\s*xmt/rcv/%loss\s*=\s*"
    r"(?P<xmt>\d+)/(?P<rcv>\d+)/(?P<loss>\d+(?:\.\d+)?)%"
)

# Summary line, e.g. `       2 unreachable`. Allow arbitrary leading whitespace.
_SUMMARY_UNREACHABLE_RE = re.compile(r"^\s*(?P<count>\d+)\s+unreachable\b")

# Diagnostic ICMP error lines, e.g.
#   `ICMP Host Unreachable from 172.17.0.4 for ICMP Echo sent to 172.17.0.70`
_DIAGNOSTIC_ICMP_RE = re.compile(
    r"^ICMP\s+(?:Host|Network)\s+Unreachable\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FpingAnalysis:
    """Normalized host-liveness facts derived from fping output or metadata."""

    alive_hosts: tuple[str, ...]
    alive_count: int
    unresponsive_count: Optional[int]
    diagnostics: tuple[str, ...]
    exit_code: Optional[int]
    stderr_preview: Optional[str]
    compact_output: str
    semantic_observations: tuple[Mapping[str, Any], ...]


def analyze_fping_output(
    *,
    stdout: str,
    stderr: str,
    exit_code: Optional[int] = None,
) -> FpingAnalysis:
    """Analyze raw fping stdout/stderr using the established parser rules."""

    alive_hosts = tuple(_extract_alive_hosts(stdout, stderr))
    unresponsive_count = _extract_unresponsive_count(stdout, stderr)
    diagnostics = tuple(_extract_diagnostics(stdout, stderr))
    return _build_analysis(
        alive_hosts=alive_hosts,
        unresponsive_count=unresponsive_count,
        diagnostics=diagnostics,
        exit_code=exit_code,
        stderr_preview=(stderr[:2000] if stderr else None),
    )


def analyze_fping_metadata(metadata: Mapping[str, Any]) -> FpingAnalysis:
    """Normalize already-parsed fping metadata without reparsing raw text."""

    alive_hosts = tuple(_string_values(metadata.get("alive_hosts")))
    unresponsive_count = _optional_int(metadata.get("unresponsive_count"))
    diagnostics = tuple(_string_values(metadata.get("diagnostics")))
    return _build_analysis(
        alive_hosts=alive_hosts,
        unresponsive_count=unresponsive_count,
        diagnostics=diagnostics,
        exit_code=_optional_int(metadata.get("exit_code")),
        stderr_preview=_text_or_none(metadata.get("stderr")),
    )


def fping_metadata_from_analysis(analysis: FpingAnalysis) -> dict[str, Any]:
    """Return the current fping tool metadata shape from analysis facts."""

    metadata: dict[str, Any] = {
        "alive_hosts": list(analysis.alive_hosts),
        "alive_count": analysis.alive_count,
        "compact_key_findings": list(analysis.alive_hosts),
        "diagnostics": list(analysis.diagnostics),
        "exit_code": analysis.exit_code,
    }
    if analysis.unresponsive_count is not None:
        metadata["unresponsive_count"] = analysis.unresponsive_count
    if analysis.stderr_preview:
        metadata["stderr"] = analysis.stderr_preview
    return metadata


def semantic_observations_from_alive_hosts(
    alive_hosts: Any,
) -> tuple[Mapping[str, Any], ...]:
    """Emit canonical host-discovered observations for unique alive IPs."""

    observations: list[Mapping[str, Any]] = []
    emitted: set[str] = set()

    for entry in _string_values(alive_hosts):
        ip_token = entry.strip()
        if not ip_token:
            continue

        # MVP: IP-only. Hostnames stay in metadata but do not create host.dns
        # observations in this iteration.
        try:
            ipaddress.ip_address(ip_token)
        except ValueError:
            continue

        if ip_token in emitted:
            continue
        emitted.add(ip_token)

        observations.append(
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": f"host.ip:{ip_token}",
                "payload": {
                    "source": "fping",
                    "host_status": "up",
                    "probe_protocol": "icmp",
                },
            }
        )

    return tuple(observations)


def render_fping_compact_output(analysis: FpingAnalysis) -> str:
    """Render compact liveness output for the compressor/direct caller."""

    lines: list[str] = [f"Alive hosts: {analysis.alive_count}"]
    if analysis.unresponsive_count is not None:
        lines.append(f"Unresponsive hosts: {analysis.unresponsive_count}")
    else:
        lines.append("Unresponsive hosts: unknown")

    if analysis.alive_hosts:
        lines.extend(analysis.alive_hosts)

    if analysis.diagnostics:
        bounded = analysis.diagnostics[:_MAX_RENDERED_DIAGNOSTIC_LINES]
        lines.append("Diagnostics:")
        lines.extend(bounded)

    return "\n".join(lines)


def _build_analysis(
    *,
    alive_hosts: tuple[str, ...],
    unresponsive_count: Optional[int],
    diagnostics: tuple[str, ...],
    exit_code: Optional[int],
    stderr_preview: Optional[str],
) -> FpingAnalysis:
    alive_hosts = tuple(dict.fromkeys(alive_hosts))
    analysis = FpingAnalysis(
        alive_hosts=alive_hosts,
        alive_count=len(alive_hosts),
        unresponsive_count=unresponsive_count,
        diagnostics=diagnostics,
        exit_code=exit_code,
        stderr_preview=stderr_preview,
        compact_output="",
        semantic_observations=semantic_observations_from_alive_hosts(alive_hosts),
    )
    return FpingAnalysis(
        alive_hosts=analysis.alive_hosts,
        alive_count=analysis.alive_count,
        unresponsive_count=analysis.unresponsive_count,
        diagnostics=analysis.diagnostics,
        exit_code=analysis.exit_code,
        stderr_preview=analysis.stderr_preview,
        compact_output=render_fping_compact_output(analysis),
        semantic_observations=analysis.semantic_observations,
    )


def _iter_lines(stdout: str, stderr: str):
    """Yield non-empty stripped lines from stdout then stderr."""

    for source in (stdout or "", stderr or ""):
        for raw in source.splitlines():
            line = raw.strip()
            if line:
                yield line


def _extract_alive_hosts(stdout: str, stderr: str) -> list[str]:
    """Return sorted unique hosts that received at least one fping reply."""

    alive: set[str] = set()
    for line in _iter_lines(stdout, stderr):
        if _DIAGNOSTIC_ICMP_RE.match(line):
            continue
        if _SUMMARY_UNREACHABLE_RE.match(line):
            continue

        stats_match = _STATS_LINE_RE.match(line)
        if stats_match is not None:
            try:
                rcv = int(stats_match.group("rcv"))
            except ValueError:
                continue
            if rcv > 0:
                alive.add(stats_match.group("host"))
            continue

        if _TERSE_ALIVE_LINE_RE.match(line) and line not in {".", "..", "..."}:
            alive.add(line)

    return sorted(alive)


def _extract_unresponsive_count(stdout: str, stderr: str) -> Optional[int]:
    """Return the count of hosts that did not reply, or None if not derivable."""

    summary_total: Optional[int] = None
    dead_stats_count = 0
    saw_stats_line = False

    for line in _iter_lines(stdout, stderr):
        summary_match = _SUMMARY_UNREACHABLE_RE.match(line)
        if summary_match is not None:
            try:
                value = int(summary_match.group("count"))
            except ValueError:
                continue
            summary_total = value if summary_total is None else max(summary_total, value)
            continue

        stats_match = _STATS_LINE_RE.match(line)
        if stats_match is None:
            continue
        saw_stats_line = True
        try:
            rcv = int(stats_match.group("rcv"))
        except ValueError:
            continue
        if rcv == 0:
            dead_stats_count += 1

    if summary_total is not None:
        return summary_total
    if saw_stats_line:
        return dead_stats_count
    return None


def _extract_diagnostics(stdout: str, stderr: str) -> list[str]:
    """Return bounded ICMP diagnostic lines preserved for operator review."""

    diagnostics: list[str] = []
    for line in _iter_lines(stdout, stderr):
        if _DIAGNOSTIC_ICMP_RE.match(line):
            diagnostics.append(line)
            if len(diagnostics) >= _MAX_DIAGNOSTIC_LINES:
                break
    return diagnostics


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = _text_or_none(item)
        if text:
            result.append(text)
    return result


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "FpingAnalysis",
    "analyze_fping_metadata",
    "analyze_fping_output",
    "fping_metadata_from_analysis",
    "render_fping_compact_output",
    "semantic_observations_from_alive_hosts",
    "_extract_alive_hosts",
    "_extract_unresponsive_count",
]
