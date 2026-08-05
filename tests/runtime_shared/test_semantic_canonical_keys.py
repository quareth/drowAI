"""Runtime-shared semantic canonical key contract tests."""

from __future__ import annotations

import pytest

from runtime_shared.semantic.canonical_keys import (
    build_host_dns_key,
    build_host_ip_key,
    build_relationship_edge_key,
    canonicalize_subject_key,
)


def test_host_dns_key_normalizes_case_trailing_dot_and_idna() -> None:
    assert build_host_dns_key("App.Example.COM.") == "host.dns:app.example.com"
    assert build_host_dns_key("T\u00e4st.Example.") == build_host_dns_key(
        "xn--tst-qla.example"
    )


def test_host_ip_key_canonicalizes_equivalent_ipv6_spellings() -> None:
    assert build_host_ip_key("2001:0db8:0:0:0:0:0:1") == build_host_ip_key(
        "2001:db8::1"
    )
    assert build_host_ip_key("2001:db8::1") == "host.ip:2001:db8::1"


def test_relationship_edge_key_normalizes_relationship_type_and_endpoint_keys() -> None:
    assert (
        build_relationship_edge_key(
            source_subject_key=" HOST.DNS:App.Example.COM ",
            relationship_type="RESOLVES_TO",
            target_subject_key=" HOST.IP:2001:DB8::1 ",
        )
        == "relationship.edge:host.dns:app.example.com:resolves_to:host.ip:2001:db8::1"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (
            " WEB.PATH:HTTPS://[2001:DB8::1]/reports/final%20report ",
            "web.path:https://[2001:db8::1]/reports/final%20report",
        ),
        (
            "web.path:https://example.test/a~b!$&'()*+,;=@c",
            "web.path:https://example.test/a~b!$&'()*+,;=@c",
        ),
    ),
)
def test_subject_key_canonicalization_preserves_canonical_url_characters(
    value: str,
    expected: str,
) -> None:
    assert canonicalize_subject_key(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        "web.path:https://example.test/\x1b[2kadmin",
        "web.path:https://example.test/admin path",
        "web.path:https://example.test/<admin>",
        "",
    ),
)
def test_subject_key_canonicalization_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_subject_key(value)


@pytest.mark.parametrize(
    "builder_call",
    [
        lambda: build_host_dns_key("bad host"),
        lambda: build_host_dns_key(""),
        lambda: build_host_ip_key("not-an-ip"),
        lambda: build_host_ip_key(""),
        lambda: build_relationship_edge_key(
            source_subject_key="",
            relationship_type="resolves_to",
            target_subject_key="host.ip:10.0.0.1",
        ),
        lambda: build_relationship_edge_key(
            source_subject_key="host.dns:example.com",
            relationship_type="resolves to",
            target_subject_key="host.ip:10.0.0.1",
        ),
        lambda: build_relationship_edge_key(
            source_subject_key="host.dns:example.com",
            relationship_type="resolves_to",
            target_subject_key="",
        ),
    ],
)
def test_canonical_key_builders_reject_invalid_inputs(builder_call) -> None:
    with pytest.raises(ValueError):
        builder_call()
