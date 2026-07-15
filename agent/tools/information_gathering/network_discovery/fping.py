"""fping host liveness tool and metadata registration.

This module builds and runs bounded fping commands, parses host reachability
evidence, and registers planner-facing metadata for ICMP-based discovery.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from .fping_analysis import (
    analyze_fping_metadata,
    analyze_fping_output,
    fping_metadata_from_analysis,
)


def _fping_g_range_from_target(target: str) -> Optional[tuple[str, str]]:
    """Expand a CIDR target into fping ``-g`` start/end host addresses."""
    text = str(target or "").strip()
    if not text or "/" not in text:
        return None
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None

    if network.version != 4:
        return None

    hosts = list(network.hosts())
    if hosts:
        return str(hosts[0]), str(hosts[-1])

    if network.num_addresses >= 2:
        return str(network.network_address), str(network.broadcast_address)
    return None


class FpingArgs(BaseToolArgs):
    """Arguments for the fping tool."""

    count: Optional[int] = Field(
        None,
        description=(
            "Optional number of echo requests per target. When omitted, fping runs "
            "in default discovery mode without -c, preferring concise alive-only "
            "output. Provide only when stats-mode (per-host xmt/rcv/%loss) output "
            "is explicitly desired."
        ),
        ge=1,
        le=10,
    )
    retries: int = Field(
        1,
        description=(
            "Bounded number of retries per target (fping -r <retries>). Caps the "
            "number of probes fping makes per host so discovery stays fast and "
            "deterministic."
        ),
        ge=0,
        le=5,
    )
    interval_seconds: float = Field(
        1.0,
        description="Interval between requests in seconds",
        ge=0.1,
        le=5.0,
    )
    alive_only: bool = Field(
        True,
        description="Only show alive hosts in output (fping -a).",
    )
    summary: bool = Field(
        True,
        description=(
            "Include summary counts (fping -s) so unresponsive_count can be parsed "
            "without per-host dead output. Disable only if a specific environment's "
            "fping does not support -s."
        ),
    )
    unreachable_only: bool = Field(
        False,
        description="Only show unreachable hosts in output (fping -u).",
    )
    ipv4_only: bool = Field(
        False,
        description="Force IPv4 (fping -4).",
    )
    ipv6_only: bool = Field(
        False,
        description="Force IPv6 (fping -6).",
    )
    range_end: Optional[str] = Field(
        None,
        description=(
            "Optional end address for range sweeps. When provided, fping will run in "
            "generate/sweep mode (-g) using start=target and end=range_end."
        ),
    )


class FpingTool(BaseTool):
    """Ping hosts quickly using fping."""

    args_model = FpingArgs
    informational_exit_codes = frozenset({1})

    def build_command(self, args: FpingArgs) -> List[str]:
        cmd: List[str] = ["fping"]

        # Default discovery mode is alive-only with bounded retries and a
        # summary line. Stats-mode (`-c`) is opt-in via an explicit `count`
        # argument so the default scan does not produce per-host dead output.
        if args.alive_only:
            cmd.append("-a")
        if args.summary:
            cmd.append("-s")

        # Bounded retries keep probe volume predictable.
        cmd.extend(["-r", str(args.retries)])

        # Interval (ms) between probes.
        cmd.extend(["-p", str(int(args.interval_seconds * 1000))])

        # Only emit -c when the caller explicitly requested stats-mode counts.
        if args.count is not None:
            cmd.extend(["-c", str(args.count)])

        if args.unreachable_only:
            cmd.append("-u")

        # IP version flags (mutually exclusive)
        if args.ipv4_only and not args.ipv6_only:
            cmd.append("-4")
        elif args.ipv6_only and not args.ipv4_only:
            cmd.append("-6")

        # Sweep mode:
        # - CIDR targets expand to ``-g <first_host> <last_host>``
        # - Explicit ``range_end`` uses ``-g target range_end``
        cidr_range = _fping_g_range_from_target(args.target)
        if cidr_range is not None:
            cmd.extend(["-g", cidr_range[0], cidr_range[1]])
        elif args.range_end:
            cmd.extend(["-g", args.target, args.range_end])
        else:
            cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FpingArgs,
    ) -> Dict[str, Any]:
        """Parse fping output into compact host-liveness metadata.

        The metadata contract is intentionally narrow for the MVP:
        ``alive_hosts``, ``alive_count``, ``unresponsive_count``,
        ``diagnostics``, and ``exit_code``. ``unresponsive_count`` is omitted
        from the dict when the output does not contain enough evidence to
        derive it (terse alive-only mode), so downstream consumers must not
        infer "all other targets are offline" from its absence.
        """
        _ = args

        analysis = analyze_fping_output(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
        return fping_metadata_from_analysis(analysis)

    def render_result_output(
        self,
        args: FpingArgs,
        stdout: str,
        stderr: str,
    ) -> Tuple[str, str]:
        """Render compact liveness output for the compressor / direct caller.

        The PTY transport hook (``agent.tool_runtime.result_enrichment``) calls
        this to replace stdout/stderr with a deterministic compact summary
        instead of forwarding the raw, possibly-very-long fping stream. Direct
        ``run()`` uses the same renderer so both transports present the same
        compact stdout to the model. Raw stdout/stderr are still archived by
        ``create_artifacts``; this renderer must NOT be wired into the artifact
        path.

        Reuses the Task 1.2 parser helpers — no independent parsing here.
        """
        _ = args
        analysis = analyze_fping_output(stdout=stdout, stderr=stderr)
        compact = analysis.compact_output
        # Stderr is intentionally cleared in the compact view: fping's per-host
        # stats often go to stderr and would otherwise re-import the noise we
        # just compressed away. Real timeout/error visibility is preserved by
        # ``run()`` (TimeoutExpired returns "Command timed out" before this
        # renderer is ever consulted) and by centralized execution-outcome
        # resolution over raw stdout/stderr.
        return compact, ""

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FpingArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Emit canonical ``network.host_discovered`` observations for alive IPs.

        MVP scope (strict):

        - Emit one observation per **unique alive IP** (IPv4 in this iteration).
        - Hostnames in ``alive_hosts`` are intentionally skipped — fping cannot
          tell us anything beyond "this token answered ICMP", and we do not
          create ``host.dns`` observations from fping in MVP.
        - Dead/unreachable hosts produce zero observations. They never enter
          ``alive_hosts`` (parser drops them), and ``unresponsive_count`` /
          diagnostics are not durable host facts.
        - Payload is exactly ``{source, host_status, probe_protocol}``. No
          service, port, finding, OS, or relationship inference is allowed
          here — those belong to nmap/masscan-style tools.

        The emitter consumes the parser output via ``metadata["alive_hosts"]``
        (Task 1.2 contract) rather than re-parsing stdout/stderr, so there is a
        single parsing authority inside this module.
        """
        _ = stdout, stderr, exit_code, args

        analysis = analyze_fping_metadata(metadata)
        return [dict(item) for item in analysis.semantic_observations]

    def create_artifacts(
        self,
        stdout: str,
        args: FpingArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        # Save output for auditing. Note: fping stats often print to stderr.
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        path = f"artifacts/fping_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(combined + "\n")
            return [path]
        except OSError:
            return []

    def run(self, args: FpingArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=args.timeout
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        # Artifact creation MUST receive the raw stdout/stderr; we do not want
        # the rendered compact summary to replace audit evidence on disk.
        artifacts = self.create_artifacts(
            proc.stdout, args=args, timestamp=int(start), stderr=proc.stderr
        )

        # Render compact compressor-facing output from raw fping streams using
        # the same helper the PTY transport calls, so both paths agree.
        compact_stdout, compact_stderr = self.render_result_output(
            args=args,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

        return ToolResult(
            success=self.is_success_exit_code(
                proc.returncode,
                args,
                stdout=proc.stdout,
                stderr=proc.stderr,
            ),
            exit_code=proc.returncode,
            stdout=compact_stdout,
            stderr=compact_stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.network_discovery.fping",
        display_name="fping",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="host_discovery",
                description="Discover host liveness across IPs/ranges with parallel ICMP probes; returns reachable and unreachable evidence; prefer for liveness checks, not for port discovery",
                output_indicators=["alive", "unreachable"],
            ),
        ],
        required_services=[],
        target_protocols=["icmp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
