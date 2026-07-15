import pathlib
import sys

import pytest

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from agent.tools import ContextualParameterGenerator
from agent.tools.information_gathering.network_discovery.nmap import (
    ScanType,
    TimingTemplate,
)


class DummyConfig:
    def __init__(self) -> None:
        self.wordlists = {
            "web_directories": "web.txt",
            "subdomains": "subs.txt",
            "dns": "dns.txt",
        }


def test_gobuster_parameter_generation():
    generator = ContextualParameterGenerator(DummyConfig())
    context = {"target_responsive": False}

    params = generator.generate_parameters(
        "web_applications.web_crawlers.gobuster", "enumeration", context
    )

    assert params["wordlist"] == "web.txt"
    assert params["threads"] == 5  # default 10 halved


def test_nmap_phase_specific_parameters():
    """generate_parameters returns schema defaults only, ignoring context."""
    generator = ContextualParameterGenerator(DummyConfig())
    context = {"current_phase": "reconnaissance"}

    params = generator.generate_parameters(
        "information_gathering.network_discovery.nmap", "scan", context
    )

    # Schema defaults only — context is no longer used.
    # default_factory fields (scan_types) are not in JSON schema "default",
    # so they are not extracted — Pydantic applies them at validation time.
    assert "scan_types" not in params
    assert "ports" not in params
    assert params["timing"] == TimingTemplate.AGGRESSIVE


@pytest.mark.parametrize("target_type,expected", [("domain", "subs.txt"), ("ip", "dns.txt")])
def test_dnsrecon_wordlist_selection(target_type, expected):
    generator = ContextualParameterGenerator(DummyConfig())
    context = {"target_type": target_type}

    params = generator.generate_parameters(
        "information_gathering.dns.dnsrecon", "dns_enum", context
    )

    assert params["wordlist"] == expected
