"""Tests for prompt-facing capability surfaces built from visible tools."""

from __future__ import annotations

from agent.tools.capability_surface import (
    build_capability_surface,
    render_capability_surface,
)


def test_uses_caller_provided_visible_tools() -> None:
    surface = build_capability_surface(
        ["information_gathering.network_discovery.nmap"]
    )

    assert "network_discovery" in surface.families
    assert surface.families["network_discovery"] == [
        "information_gathering.network_discovery.nmap"
    ]


def test_render_identifies_surface_as_agent_advertised_capabilities() -> None:
    text = render_capability_surface(
        ["information_gathering.network_discovery.nmap"]
    )

    assert "currently advertised to this agent" in text
    assert "visible tool set for this run" in text
    assert "exact tool choice remains owned by the tool selector/builder" in text


def test_excludes_hidden_shell_tools_and_does_not_advertise_shell_execution() -> None:
    text = render_capability_surface(["shell.exec", "shell.script"])

    assert text == ""
    assert "shell_execution" not in text
    assert "shell.exec" not in text
    assert "shell.script" not in text


def test_visible_metasploit_is_advertised_as_exploitation_framework() -> None:
    text = render_capability_surface(
        ["exploitation_tools.metasploit.run_exploit"]
    )

    assert "exploitation_framework" in text
    assert "exploitation_tools.metasploit.run_exploit" in text
    assert "session_interaction" not in text


def test_mixed_visible_and_hidden_tools_only_render_visible_capabilities() -> None:
    text = render_capability_surface(
        [
            "shell.exec",
            "web_applications.web_crawlers.ffuf",
        ]
    )

    assert "http_web_testing" in text
    assert "web_applications.web_crawlers.ffuf" in text
    assert "shell_execution" not in text
    assert "shell.exec" not in text


def test_default_surface_uses_shared_visible_tool_source(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.capability_surface.visible_available_tools",
        lambda: ["web_applications.web_crawlers.ffuf"],
    )

    text = render_capability_surface()

    assert "http_web_testing" in text
    assert "web_applications.web_crawlers.ffuf" in text


def test_default_surface_reflects_mvp_visible_tools() -> None:
    surface = build_capability_surface()
    surfaced_tools = {
        tool_id
        for tools in surface.families.values()
        for tool_id in tools
    }

    assert "filesystem.read_file" in surfaced_tools
    assert "information_gathering.network_discovery.fping" in surfaced_tools
    assert "information_gathering.network_discovery.masscan" not in surfaced_tools
    assert "information_gathering.network_discovery.nmap" in surfaced_tools
    assert "password_attacks.online_attacks.hydra" not in surfaced_tools
    assert "exploitation_tools.metasploit.run_exploit" in surfaced_tools
    assert "web_applications.web_crawlers.ffuf" in surfaced_tools
    assert "shell.exec" not in surfaced_tools


def test_default_surface_does_not_assign_unrelated_capabilities() -> None:
    surface = build_capability_surface()

    assert "password_attacks.online_attacks.hydra" not in surface.families.get(
        "http_web_testing",
        [],
    )
    assert "networking_utilities.network" not in surface.families.get(
        "http_web_testing",
        [],
    )
    assert "sniffing_spoofing.network_sniffers.tshark" not in surface.families.get(
        "credential_attack",
        [],
    )
    assert "sniffing_spoofing.network_sniffers.tshark" not in surface.families.get(
        "reporting",
        [],
    )
