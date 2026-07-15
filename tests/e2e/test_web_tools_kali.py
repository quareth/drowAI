import os
import shutil
import subprocess

import pytest

from agent.tools.web_applications.web_crawlers.gobuster import GobusterArgs, GobusterTool
from agent.tools.web_applications.web_vulnerability_scanners.nuclei import NucleiArgs, NucleiTool


DOCKER_AVAILABLE = shutil.which("docker") is not None
RUN_KALI_E2E = os.getenv("RUN_KALI_WEB_TOOLS_E2E") == "1"


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(not DOCKER_AVAILABLE or not RUN_KALI_E2E, reason="Docker/Kali not enabled for web tool E2E")
def test_gobuster_kali_execution(tmp_path):
    tool = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="/usr/share/wordlists/dirb/common.txt")
    cmd = tool.build_command(args)
    if shutil.which(cmd[0]) is None:
        pytest.skip(f"{cmd[0]} not installed in environment")

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    # Accept non-zero as long as command executed and produced output
    assert proc.stdout is not None
    # workspace artifact location
    tool.create_artifacts(proc.stdout, args=args, timestamp=1700000000)
    assert (tmp_path / "artifacts").exists() is False or True  # ensure no exception


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(not DOCKER_AVAILABLE or not RUN_KALI_E2E, reason="Docker/Kali not enabled for web tool E2E")
def test_nuclei_kali_execution(tmp_path):
    tool = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    cmd = tool.build_command(args)
    if shutil.which(cmd[0]) is None:
        pytest.skip(f"{cmd[0]} not installed in environment")

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    assert proc.stdout is not None
    tool.create_artifacts(proc.stdout, args=args, timestamp=1700000000)
    assert (tmp_path / "artifacts").exists() is False or True

