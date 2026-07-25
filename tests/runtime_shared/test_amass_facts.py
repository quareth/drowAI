"""Tests for shared normalized Amass DNS fact adaptation."""

from __future__ import annotations

import pytest

from runtime_shared.semantic.amass_facts import (
    collect_amass_facts,
    dns_record_type,
)


def test_collect_amass_facts_normalizes_and_freezes_compatible_metadata() -> None:
    """Duplicate and invalid rows produce one stable immutable fact view."""

    facts = collect_amass_facts(
        {
            "subdomains": [
                {
                    "subdomain": "API.Example.COM.",
                    "ip": ["2001:0db8::5", "192.0.2.20", "not-an-ip"],
                    "discovery_role": "scope_seed",
                    "result_scope": "input",
                },
                {
                    "subdomain": "api.example.com",
                    "ip": ["192.0.2.20"],
                    "discovery_role": "unsupported",
                    "result_scope": "updated",
                },
                {
                    "subdomain": "api.example.com",
                    "ip": [],
                    "discovery_role": "newly_discovered",
                    "result_scope": "",
                },
                {"subdomain": "unresolved.example.com", "ip": []},
                {"subdomain": "invalid name", "ip": ["192.0.2.99"]},
            ],
            "hosts": [
                {"hostname": "WWW.Example.COM.", "ip": ["192.0.2.10"]},
                {"hostname": "", "ip": ["192.0.2.98"]},
            ],
            "ips": ["2001:db8::5", "192.0.2.10", "invalid"],
        }
    )

    assert facts.names == (
        "api.example.com",
        "unresolved.example.com",
        "www.example.com",
    )
    assert facts.ips == ("192.0.2.10", "192.0.2.20", "2001:db8::5")
    assert facts.addresses_by_name == {
        "api.example.com": ("192.0.2.20", "2001:db8::5"),
        "unresolved.example.com": (),
        "www.example.com": ("192.0.2.10",),
    }
    assert facts.roles_by_name == {"api.example.com": "newly_discovered"}
    assert facts.result_scope_by_name == {"api.example.com": "updated"}

    with pytest.raises(TypeError):
        facts.addresses_by_name["api.example.com"] = ()  # type: ignore[index]


def test_dns_record_type_distinguishes_canonical_ipv4_and_ipv6() -> None:
    """The shared fact authority owns DNS address record-type derivation."""

    assert dns_record_type("192.0.2.20") == "A"
    assert dns_record_type("2001:db8::5") == "AAAA"
