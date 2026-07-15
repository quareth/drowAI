"""Behavioral regression checks for the canonical runtime VPN manager."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VPN_MANAGER = REPO_ROOT / "runtime" / "vpn" / "vpn-manager.sh"


def _state(path: Path) -> dict[str, str]:
    return {
        key: value
        for key, _, value in (
            line.partition("=")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }


def _runtime_env(
    tmp_path: Path, *, exit_code: int = 0, lifetime: int = 60
) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_openvpn = fake_bin / "openvpn"
    fake_openvpn.write_text(
        """#!/usr/bin/env bash
set -u
pid_file=""
while (($#)); do
  if [[ "$1" == "--writepid" ]]; then pid_file="$2"; shift 2; else shift; fi
done
if [[ "${FAKE_OPENVPN_EXIT_CODE:-0}" != "0" ]]; then exit "${FAKE_OPENVPN_EXIT_CODE}"; fi
sleep "${FAKE_OPENVPN_LIFETIME:-60}" </dev/null >/dev/null 2>&1 &
printf '%s\n' "$!" >"$pid_file"
""",
        encoding="utf-8",
    )
    fake_openvpn.chmod(0o755)
    fake_ip = fake_bin / "ip"
    fake_ip.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == *"addr show dev"* ]] && [[ -f "${FAKE_TUN_UP:-}" ]]; then
  echo "7: tun0: <POINTOPOINT,UP> mtu 1500"
  echo "    inet 10.8.0.2/24 scope global tun0"
elif [[ "$*" == *"route show dev eth0"* ]]; then
  echo "198.18.0.8/29 dev eth0 proto kernel scope link src 198.18.0.10"
elif [[ "$*" == *"route show dev tun0"* ]]; then
  echo "10.0.0.0/8 dev tun0"
fi
""",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)
    config = tmp_path / "task.ovpn"
    config.write_text("client\ndev tun\nremote invalid.example 443\n", encoding="utf-8")
    state_dir = tmp_path / "vpn"
    tun_marker = tmp_path / "tun-up"
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "VPN_STATE_DIR": str(state_dir),
        "VPN_CONFIG": str(config),
        "VPN_TUN_DEVICE": "/dev/null",
        "VPN_OPENVPN_BIN": str(fake_openvpn),
        "VPN_CLASSIFIER_PYTHONPATH": str(REPO_ROOT),
        "VPN_ATTEMPT_DEADLINE_SECONDS": "1",
        "VPN_WATCH_POLL_SECONDS": "0.1",
        "VPN_STOP_TIMEOUT_SECONDS": "1",
        "FAKE_OPENVPN_EXIT_CODE": str(exit_code),
        "FAKE_OPENVPN_LIFETIME": str(lifetime),
        "FAKE_TUN_UP": str(tun_marker),
    }


def _run(action: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(VPN_MANAGER), action],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_vpn_manager_uses_lock_attempt_ownership_and_polling_watchdog() -> None:
    script = VPN_MANAGER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'LOCK_FILE="$LOG_DIR/manager.lock"' in script
    assert 'ATTEMPT_DIR="$LOG_DIR/attempts"' in script
    assert "process_start_identity" in script
    assert "run_connect_action reconnecting" in script
    assert "while true; do" in script
    assert 'sleep "$ATTEMPT_DEADLINE_SECONDS"' not in script
    assert "pkill" not in script
    assert "runtime_shared.vpn_observability classify" in script
    assert '--log-append "$LOG_FILE"' in script


@pytest.mark.skipif(
    shutil.which("flock") is None, reason="runtime flock binary is unavailable"
)
def test_start_failure_preserves_openvpn_exit_code_and_valid_json(
    tmp_path: Path,
) -> None:
    env = _runtime_env(tmp_path, exit_code=42)

    result = _run("reconnect", env)

    assert result.returncode == 42
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {
        "status": "failed",
        "ip_address": "",
        "error_message": "OpenVPN process failed to start",
    }
    assert _state(tmp_path / "vpn" / "state")["error_category"] == "process_start"


@pytest.mark.skipif(
    shutil.which("flock") is None, reason="runtime flock binary is unavailable"
)
def test_watchdog_uses_persisted_identity_when_attempt_pid_file_disappears(
    tmp_path: Path,
) -> None:
    env = _runtime_env(tmp_path)
    initiated = _run("reconnect", env)
    assert initiated.returncode == 0
    assert json.loads(initiated.stdout.splitlines()[-1])["status"] == "reconnecting"
    state = _state(tmp_path / "vpn" / "state")
    (tmp_path / "vpn" / "attempts" / f"{state['attempt_id']}.pid").unlink()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = _state(tmp_path / "vpn" / "state")
        if state["status"] == "failed":
            break
        time.sleep(0.1)

    assert state["status"] == "failed"
    assert state["error_category"] == "deadline"
    assert state["error"] == "VPN connection deadline exceeded"


@pytest.mark.skipif(
    shutil.which("flock") is None, reason="runtime flock binary is unavailable"
)
def test_connected_reconnect_is_idempotent_and_does_not_replace_attempt(
    tmp_path: Path,
) -> None:
    env = _runtime_env(tmp_path)
    assert _run("reconnect", env).returncode == 0
    before = _state(tmp_path / "vpn" / "state")
    Path(env["FAKE_TUN_UP"]).touch()

    result = _run("reconnect", env)
    after = _state(tmp_path / "vpn" / "state")

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "status": "connected",
        "ip_address": "10.8.0.2",
        "error_message": "",
    }
    assert after["attempt_id"] == before["attempt_id"]
    _run("disconnect", env)


@pytest.mark.skipif(
    shutil.which("flock") is None, reason="runtime flock binary is unavailable"
)
def test_status_uses_tunnel_ipv4_as_the_connected_authority(tmp_path: Path) -> None:
    env = _runtime_env(tmp_path)
    Path(env["FAKE_TUN_UP"]).touch()

    result = _run("status", env)

    assert result.returncode == 0
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "status": "connected",
        "ip_address": "10.8.0.2",
        "error_message": "",
    }
    assert _state(tmp_path / "vpn" / "state")["status"] == "connected"


@pytest.mark.skipif(
    shutil.which("flock") is None, reason="runtime flock binary is unavailable"
)
def test_connected_tunnel_loss_transitions_to_failed_for_retry(tmp_path: Path) -> None:
    env = _runtime_env(tmp_path)
    assert _run("reconnect", env).returncode == 0
    Path(env["FAKE_TUN_UP"]).touch()
    assert json.loads(_run("status", env).stdout.splitlines()[-1])["status"] == "connected"

    Path(env["FAKE_TUN_UP"]).unlink()
    result = _run("status", env)

    assert result.returncode == 0
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "status": "failed",
        "ip_address": "",
        "error_message": "VPN tunnel device setup failed",
    }
    state = _state(tmp_path / "vpn" / "state")
    assert state["status"] == "failed"
    assert state["error_category"] == "device"
    assert state["pid"]
    _run("disconnect", env)
