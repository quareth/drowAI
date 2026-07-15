"""Legacy nmap scan and parsing helpers for executor compatibility paths.

Purpose:
- Own legacy nmap output parsing, scan execution, and artifact-output persistence.

Owns:
- Parsing nmap stdout into Finding records for legacy scan flows.
- Executing legacy nmap scan subprocess with timeout handling.
- Writing legacy command output artifacts within workspace policy.

Does not own:
- Facade orchestration contracts and adapter compatibility surfaces.
- Tool routing, PTY/file-comm transport, and shell-policy internals.
- Scope/approval gate policy logic.

Invariants:
- Preserve existing finding formatting, timeout/error behavior, and debug log messages.
- Preserve zero-behavior-change result shapes and artifact-write safety behavior.
- Preserve compatibility with executor wrapper methods and existing tests.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, List

try:
    from ..models import ExecutionResult, Finding
except ImportError:  # pragma: no cover
    from models import ExecutionResult, Finding


def parse_nmap_output(output: str, target: str) -> List[Finding]:
    """Parse nmap output for open ports and host status."""
    findings: List[Finding] = []

    port_pattern = r"(\d+)/(tcp|udp)\s+open\s+(\w+)"
    for match in re.finditer(port_pattern, output):
        port, protocol, service = match.groups()
        findings.append(
            Finding(
                id=f"{target}_{port}_{protocol}",
                severity="info",
                title=f"Open port {port}/{protocol}",
                description=f"{service} service open on {target}:{port}/{protocol}",
                target=target,
                evidence=match.group(0),
                recommendation="Review service configuration and restrict access if unnecessary",
            )
        )

    if "Host is up" in output:
        findings.append(
            Finding(
                id=f"{target}_host_up",
                severity="info",
                title="Host is up",
                description=f"Host {target} responded to probes",
                target=target,
                evidence="nmap detected host as up",
                recommendation="Proceed with further enumeration",
            )
        )
    elif "Host seems down" in output:
        findings.append(
            Finding(
                id=f"{target}_host_down",
                severity="info",
                title="Host appears down",
                description=f"Host {target} did not respond to probes",
                target=target,
                evidence="nmap detected host as down",
                recommendation="Verify target availability and network connectivity",
            )
        )
    else:
        findings.append(
            Finding(
                id=f"{target}_scan_completed",
                severity="info",
                title="Port scan completed",
                description=f"Port scan completed for {target}",
                target=target,
                evidence=f"nmap scan output: {output[:200]}...",
                recommendation="Review scan results and proceed with next phase",
            )
        )

    return findings


async def execute_nmap_scan(
    *,
    target: str,
    timeout_seconds: float,
    logger: Any = None,
    store_output_fn: Callable[[str, str], Awaitable[None]],
) -> ExecutionResult:
    """Run a simple nmap scan against the target."""
    command = f"nmap -T4 -F {target}"

    if logger:
        logger.log_operation("DEBUG", f"[DEBUG_NMAP_COMMAND] {command}")

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )

        result = ExecutionResult(
            success=process.returncode == 0,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
            exit_code=process.returncode,
        )

        await store_output_fn(command, result.stdout)

        if logger:
            logger.log_operation(
                "DEBUG",
                f"[DEBUG_NMAP_OUTPUT] stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            logger.log_operation("INFO", f"Action executed, result length: {len(str(result))}")

        return result

    except asyncio.TimeoutError:
        if logger:
            logger.log_operation("ERROR", f"Command timed out after {timeout_seconds} seconds")
        return ExecutionResult(False, "", f"Command timed out after {timeout_seconds} seconds", -2)


async def store_command_output(
    *,
    command: str,
    output: str,
    logger: Any = None,
    workspace: str = "/workspace",
) -> None:
    """Store command output to an artifact file under the workspace."""
    artifacts_dir = os.path.join(workspace, "artifacts")

    try:
        os.makedirs(artifacts_dir, exist_ok=True)
    except (OSError, PermissionError) as exc:
        if logger:
            logger.log_operation(
                "ERROR",
                f"Workspace artifacts directory is not writable (no fallback allowed): {exc}",
            )
        return

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}_{command.split()[0]}.txt"
    path = os.path.join(artifacts_dir, filename)

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(output)
        if logger:
            logger.log_operation("DEBUG", f"Stored command output to: {path}")
    except Exception as exc:
        if logger:
            logger.log_operation("ERROR", f"Failed to store command output: {exc}")
