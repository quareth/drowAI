"""Tests for shared runtime environment-info parsers and collectors."""

from __future__ import annotations

from runtime_shared.environment_info import (
    collect_environment_info_from_executor,
    create_empty_env_info,
    parse_dns,
    parse_ip_addr,
    parse_os_release,
    parse_routes,
)


def test_environment_info_parsers_preserve_existing_shape() -> None:
    os_info = parse_os_release('PRETTY_NAME="Kali GNU/Linux Rolling"\nVERSION_ID="2026.1"')
    interfaces = parse_ip_addr(
        "1: lo: <LOOPBACK,UP> mtu 65536\n"
        "    inet 127.0.0.1/8 scope host lo\n"
        "2: eth0@if274: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 65535\n"
        "    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0\n"
    )
    routes = parse_routes("default via 172.17.0.1 dev eth0\n172.17.0.0/16 dev eth0")

    assert create_empty_env_info()["network"]["interfaces"] == []
    assert os_info == {
        "name": "Kali GNU/Linux Rolling",
        "version": "2026.1",
        "kernel": "unknown",
    }
    assert interfaces == [
        {"name": "lo", "state": "UP", "ipv4": "127.0.0.1/8"},
        {"name": "eth0", "state": "UP", "ipv4": "172.17.0.2/16"},
    ]
    assert routes[0] == {"destination": "default", "gateway": "172.17.0.1", "interface": "eth0"}
    assert parse_dns("nameserver 192.168.65.7\nsearch local") == ["192.168.65.7"]


def test_collect_environment_info_from_executor_returns_partial_data() -> None:
    outputs = {
        "hostname": "runner-task",
        "cat /etc/os-release": 'PRETTY_NAME="Kali"\nVERSION_ID="2026.1"',
        "uname -r": "6.12-kali",
        "ip addr show": "2: eth0: <UP>\n    inet 10.0.0.5/24",
        "ip route": "default via 10.0.0.1 dev eth0",
    }

    env_info = collect_environment_info_from_executor(
        lambda command: outputs.get(command, ""),
        collected_at="2026-05-27T00:00:00Z",
    )

    assert env_info["hostname"] == "runner-task"
    assert env_info["os"]["name"] == "Kali"
    assert env_info["os"]["kernel"] == "6.12-kali"
    assert env_info["network"]["interfaces"][0]["ipv4"] == "10.0.0.5/24"
    assert env_info["network"]["default_gateway"] == "10.0.0.1"
    assert env_info["network"]["dns_servers"] == []
    assert env_info["collection_errors"] == ["Failed to read /etc/resolv.conf"]
