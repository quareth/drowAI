"""Opt-in real-Docker canary for managed task networking and VPN diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

import docker
import pytest

from runtime_shared.docker_network_manager import DockerTaskNetworkManager
from runtime_shared.runtime_network import (
    RUNTIME_NETWORK_OPTIONS,
    build_runtime_network_spec,
    parse_runtime_network_pool,
)
from runtime_shared.vpn_observability import normalize_vpn_log_lines


REPO_ROOT = Path(__file__).resolve().parents[2]
VPN_MANAGER = REPO_ROOT / "runtime" / "vpn" / "vpn-manager.sh"
VPN_OBSERVABILITY = REPO_ROOT / "runtime_shared" / "vpn_observability.py"
INVALID_OVPN = REPO_ROOT / "tests" / "fixtures" / "vpn" / "invalid-public-endpoint.ovpn"
CANARY_AUTH = REPO_ROOT / "tests" / "fixtures" / "vpn" / "nonsecret-canary-auth.txt"


def _exec(container, command: list[str], *, environment: dict[str, str] | None = None):
    result = container.exec_run(command, environment=environment)
    output = result.output.decode("utf-8", errors="replace")
    return int(result.exit_code), output


@pytest.mark.skipif(
    not os.getenv("DROWAI_VPN_CANARY_IMAGE"),
    reason="Set DROWAI_VPN_CANARY_IMAGE to run the disposable real-Docker VPN canary.",
)
def test_managed_network_and_invalid_vpn_canary() -> None:
    """Exercise DNS, isolation, routing, VPN failure logs, and owned cleanup."""
    client = docker.from_env()
    client.ping()
    image = os.environ["DROWAI_VPN_CANARY_IMAGE"]
    suffix = uuid.uuid4().hex[:10]
    container_names = [f"drowai-vpn-canary-{suffix}-task-{index}" for index in (1, 2)]
    specs = [
        build_runtime_network_spec(
            container_name=name,
            runtime_identity=name,
            tenant_id="docker-canary",
            task_id=index,
            runtime_owner="docker-canary",
            pool=parse_runtime_network_pool(None),
        )
        for index, name in enumerate(container_names, start=1)
    ]
    manager = DockerTaskNetworkManager(lambda: client)
    containers = []
    provisioned_specs = []
    try:
        network_results = []
        for spec in specs:
            network_results.append(manager.ensure(spec))
            provisioned_specs.append(spec)

        first = client.containers.run(
            image,
            name=container_names[0],
            entrypoint="/bin/bash",
            command=["-lc", "sleep 120"],
            detach=True,
            network=network_results[0].name,
            cap_add=["NET_ADMIN"],
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            volumes={
                str(VPN_MANAGER): {"bind": "/tmp/vpn-manager.sh", "mode": "ro"},
                str(VPN_OBSERVABILITY): {
                    "bind": "/opt/drowai/runtime/python/runtime_shared/vpn_observability.py",
                    "mode": "ro",
                },
                str(INVALID_OVPN): {
                    "bind": "/workspace/invalid-public-endpoint.ovpn",
                    "mode": "ro",
                },
                str(CANARY_AUTH): {
                    "bind": "/workspace/nonsecret-canary-auth.txt",
                    "mode": "ro",
                },
            },
        )
        containers.append(first)
        second = client.containers.run(
            image,
            name=container_names[1],
            entrypoint="/bin/bash",
            command=["-lc", "sleep 120"],
            detach=True,
            network=network_results[1].name,
        )
        containers.append(second)

        network = client.networks.get(network_results[0].name)
        network.reload()
        assert network.attrs["Driver"] == "bridge"
        assert network.attrs["Internal"] is False
        assert all(
            network.attrs["Options"].get(key) == value
            for key, value in RUNTIME_NETWORK_OPTIONS.items()
        )

        rc, resolver = _exec(first, ["sh", "-lc", "cat /etc/resolv.conf"])
        assert rc == 0
        assert "nameserver 127.0.0.11" in resolver
        rc, resolved = _exec(first, ["getent", "ahostsv4", "example.com"])
        assert rc == 0
        assert resolved.strip()
        rc, default_route = _exec(first, ["sh", "-lc", "ip -4 route show default"])
        assert rc == 0
        assert " dev eth0" in default_route
        rc, nmap_output = _exec(first, ["nmap", "-sn", "-n", "127.0.0.1"])
        assert rc == 0
        assert "Nmap done" in nmap_output

        second.reload()
        second_ip = second.attrs["NetworkSettings"]["Networks"][
            network_results[1].name
        ]["IPAddress"]
        rc, _ = _exec(first, ["ping", "-c", "1", "-W", "1", second_ip])
        assert rc != 0

        vpn_env = {
            "VPN_CONFIG": "/workspace/invalid-public-endpoint.ovpn",
            "VPN_CREDENTIALS_FILE": "/workspace/nonsecret-canary-auth.txt",
            "VPN_ATTEMPT_DEADLINE_SECONDS": "8",
            "VPN_WATCH_POLL_SECONDS": "0.25",
        }
        rc, initiated = _exec(
            first, ["bash", "/tmp/vpn-manager.sh", "reconnect"], environment=vpn_env
        )
        assert rc == 0
        assert json.loads(initiated.splitlines()[-1])["status"] == "reconnecting"

        status = {}
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            rc, raw_status = _exec(first, ["bash", "/tmp/vpn-manager.sh", "status"])
            assert rc == 0
            status = json.loads(raw_status.splitlines()[-1])
            if status["status"] == "failed":
                break
            time.sleep(0.5)
        assert status["status"] == "failed"

        rc, raw_logs = _exec(first, ["tail", "-n", "100", "/vpn/connection.log"])
        assert rc == 0
        assert "RESOLVE: Cannot resolve" not in raw_logs
        assert "canary-placeholder" not in raw_logs
        terminal_rows = normalize_vpn_log_lines(raw_logs.splitlines())
        assert any(
            row["service"] == "vpn" and row["level"] == "error" for row in terminal_rows
        )

        rc, route_output = _exec(
            first,
            [
                "sh",
                "-lc",
                "ip tuntap add dev tun0 mode tun && ip link set tun0 up && "
                "ip addr add 10.99.0.2/30 dev tun0 && "
                "ip route add 203.0.113.0/24 dev tun0 && ip route get 203.0.113.10",
            ],
        )
        assert rc == 0
        assert " dev tun0" in route_output
    finally:
        for container in reversed(containers):
            try:
                container.remove(force=True)
            except Exception:
                pass
        for spec in reversed(provisioned_specs):
            manager.remove_empty(spec)

    for result in network_results:
        with pytest.raises(docker.errors.NotFound):
            client.networks.get(result.name)
