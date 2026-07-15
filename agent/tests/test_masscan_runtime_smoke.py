"""Runtime smoke tests for masscan command compatibility.

These tests validate generated commands with `masscan --echo` when the binary is
available, without transmitting scan traffic.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent.tools.information_gathering.network_discovery.masscan import MasscanArgs, MasscanTool


@pytest.mark.integration
def test_masscan_echo_accepts_generated_command() -> None:
    """Validate that generated masscan command is accepted by runtime parser."""

    if "host_discovery" not in MasscanArgs.model_fields:
        pytest.skip("MASSCAN_SCHEMA_V2 disabled")

    if shutil.which("masscan") is None:
        pytest.skip("masscan binary not available in test environment")

    tool = MasscanTool()
    args = MasscanArgs(
        target="127.0.0.1",
        ports="80,443",
        rate=1000,
        retries=0,
        wait=0,
    )

    cmd = tool.build_command(args)
    echo_cmd = [cmd[0], "--echo", *cmd[1:]]

    proc = subprocess.run(echo_cmd, capture_output=True, text=True, timeout=10, check=False)
    assert proc.returncode == 0, f"masscan --echo failed: {proc.stderr}"
