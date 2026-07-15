"""Tests for the shared service identity authority used by knowledge services."""

from __future__ import annotations

import pytest

from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    build_service_socket_key_from_application,
    default_port_for_application_protocol,
    infer_transport_from_application_protocol,
    parse_service_socket_key,
)


def test_build_and_parse_service_socket_key_accepts_canonical_transport_identity() -> None:
    key = build_service_socket_key(ip="10.10.10.5", protocol="TCP", port="80")
    parts = parse_service_socket_key(key)

    assert key == "service.socket:10.10.10.5/tcp/80"
    assert parts is not None
    assert parts.ip == "10.10.10.5"
    assert parts.protocol == "tcp"
    assert parts.port == 80
    assert parts.subject_key == key


def test_service_socket_key_rejects_application_protocol_transport() -> None:
    with pytest.raises(ValueError, match="protocol must be tcp or udp"):
        build_service_socket_key(ip="10.10.10.5", protocol="ftp", port=21)

    assert parse_service_socket_key("service.socket:10.10.10.5/ftp/21") is None


def test_application_protocol_resolves_metadata_transport_and_default_port() -> None:
    assert infer_transport_from_application_protocol("HTTPS") == "tcp"
    assert default_port_for_application_protocol("HTTPS") == 443
    assert (
        build_service_socket_key_from_application(
            ip="10.10.10.5",
            application_protocol="https",
        )
        == "service.socket:10.10.10.5/tcp/443"
    )


def test_unknown_application_protocol_is_not_guessed() -> None:
    assert infer_transport_from_application_protocol("customproto") is None
    assert default_port_for_application_protocol("customproto") is None
    assert (
        build_service_socket_key_from_application(
            ip="10.10.10.5",
            application_protocol="customproto",
            port=31337,
        )
        is None
    )
