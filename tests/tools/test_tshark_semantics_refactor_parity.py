"""Baseline snapshots for the facade TShark semantics parser refactor."""

from __future__ import annotations

import copy
import json
import inspect
from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from agent.tools.sniffing_spoofing.network_sniffers import tshark_semantics
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing import (
    common,
    field_output,
    json_packets,
    parser,
    security,
    semantic_emitters,
    survey,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols import (
    dns,
    ftp,
    http,
    mail,
    tls,
)


Parser = Callable[..., dict[str, Any]]


EXPECTED_PUBLIC_API = {
    "build_tshark_semantic_evidence",
    "build_tshark_semantic_observations",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_SENSITIVE_PROOF_MODE",
    "fingerprint_secret",
    "normalize_tshark_field_extract_fields",
    "parse_tshark_output",
    "SECRET_FINGERPRINT_KEY_ENV",
    "TSHARK_FIELD_EXTRACT_ALLOWLIST",
    "TSHARK_FIELD_NAME_RE",
    "TSHARK_SCHEMA_VERSION",
}

EXPECTED_PUBLIC_CONSTANTS = {
    "DEFAULT_MAX_ROWS",
    "DEFAULT_SENSITIVE_PROOF_MODE",
    "SECRET_FINGERPRINT_KEY_ENV",
    "TSHARK_FIELD_EXTRACT_ALLOWLIST",
    "TSHARK_FIELD_NAME_RE",
    "TSHARK_SCHEMA_VERSION",
}


def _json_packets(*layers: Mapping[str, Any]) -> str:
    return json.dumps([{"_source": {"layers": layer}} for layer in layers])


def _facade_parse(stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
    facade_result = tshark_semantics.parse_tshark_output(stdout, stderr, **kwargs)
    refactored_result = parser.parse_tshark_output(stdout, stderr, **kwargs)
    assert_same_output(facade_result, refactored_result)
    return facade_result


def assert_same_output(expected_result: object, actual_result: object) -> None:
    assert actual_result == expected_result


def _snapshot(
    parser: Parser,
    stdout: str,
    stderr: str = "",
    *,
    keys: Iterable[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the stable keys a future refactor parser must match."""

    metadata = parser(stdout, stderr, **kwargs)
    return {key: metadata[key] for key in keys}


def _field_row(fields: list[str], **values: str) -> str:
    return "\t".join(str(values.get(field, "")) for field in fields)


JSON_PACKET_STDOUT = _json_packets(
    {
        "frame": {
            "frame.number": "1",
            "frame.time": "t1",
            "frame.time_epoch": "100.0",
            "frame.protocols": "eth:ip:udp:dns",
            "frame.len": "86",
        },
        "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.53"},
        "udp": {"udp.srcport": "53123", "udp.dstport": "53"},
        "dns": {
            "dns.qry.name": "www.example.test",
            "dns.qry.type": "1",
            "dns.a": ["198.51.100.10", "198.51.100.10"],
            "dns.flags.rcode": "0",
        },
    },
    {
        "frame": {
            "frame.number": "2",
            "frame.time": "t2",
            "frame.time_epoch": "101.0",
            "frame.protocols": "eth:ip:tcp:http",
            "frame.len": "512",
        },
        "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
        "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
        "http": {
            "http.host": "app.example.test",
            "http.request.method": "GET",
            "http.request.uri": "http://app.example.test/login?next=/admin",
            "http.response.code": "200",
            "http.user_agent": "SyntheticAgent/1.0",
            "http.authorization": "Bearer synthetic-http-token",
            "http.cookie": "session=synthetic-cookie-secret",
        },
    },
    {
        "frame": {
            "frame.number": "3",
            "frame.time": "t3",
            "frame.time_epoch": "102.0",
            "frame.protocols": "eth:ip:tcp:tls",
            "frame.len": "256",
        },
        "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.443"},
        "tcp": {"tcp.srcport": "49153", "tcp.dstport": "443", "tcp.stream": "8"},
        "tls": {
            "tls.handshake.extensions_server_name": "secure.example.test",
            "tls.handshake.extensions_alpn_str": "h2",
            "tls.handshake.version": "0x0303",
        },
        "x509sat": {
            "x509sat.uTF8String": "CN=secure.example.test",
            "x509sat.printableString": "Issuer Test CA",
            "x509sat.subject": "CN=secure.example.test",
            "x509sat.issuer": "CN=Issuer Test CA",
        },
    },
)


FIELD_PROFILE_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.protocols",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "udp.srcport",
    "udp.dstport",
    "frame.len",
    "dns.qry.name",
    "dns.qry.type",
    "dns.a",
    "dns.flags.rcode",
    "http.host",
    "http.request.method",
    "http.request.uri",
    "http.response.code",
    "http.authorization",
    "http.cookie",
    "tls.handshake.extensions_server_name",
    "tls.handshake.version",
    "x509sat.printableString",
    "ftp.request.command",
    "ftp.request.arg",
    "ftp.response.code",
    "ftp.response.arg",
    "smtp.req.command",
    "smtp.req.parameter",
    "pop.request.command",
    "pop.request.parameter",
    "imap.request.command",
    "imap.request",
    "data-text-lines",
]


FIELD_PROFILE_STDOUT = "\n".join(
    [
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "1",
                "frame.time_epoch": "100.0",
                "frame.protocols": "eth:ip:udp:dns",
                "ip.src": "192.0.2.10",
                "ip.dst": "198.51.100.53",
                "udp.srcport": "53123",
                "udp.dstport": "53",
                "frame.len": "86",
                "dns.qry.name": "www.example.test",
                "dns.qry.type": "1",
                "dns.a": "198.51.100.10",
                "dns.flags.rcode": "0",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "2",
                "frame.time_epoch": "101.0",
                "frame.protocols": "eth:ip:tcp:http",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.20",
                "tcp.srcport": "49152",
                "tcp.dstport": "80",
                "tcp.stream": "7",
                "frame.len": "512",
                "http.host": "app.example.test",
                "http.request.method": "POST",
                "http.request.uri": "/login",
                "http.response.code": "302",
                "http.authorization": "Basic abc123",
                "http.cookie": "session=clear",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "3",
                "frame.time_epoch": "102.0",
                "frame.protocols": "eth:ip:tcp:tls",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.30",
                "tcp.srcport": "49153",
                "tcp.dstport": "443",
                "tcp.stream": "8",
                "frame.len": "256",
                "tls.handshake.extensions_server_name": "secure.example.test",
                "tls.handshake.version": "0x0303",
                "x509sat.printableString": "CN=secure.example.test",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "4",
                "frame.time_epoch": "103.0",
                "frame.protocols": "eth:ip:tcp:ftp",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.21",
                "tcp.srcport": "49154",
                "tcp.dstport": "21",
                "tcp.stream": "9",
                "frame.len": "74",
                "ftp.request.command": "USER",
                "ftp.request.arg": "nathan",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "5",
                "frame.time_epoch": "104.0",
                "frame.protocols": "eth:ip:tcp:ftp",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.21",
                "tcp.srcport": "49154",
                "tcp.dstport": "21",
                "tcp.stream": "9",
                "frame.len": "86",
                "ftp.request.command": "PASS",
                "ftp.request.arg": "Buck3tH4TF0RM3!",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "6",
                "frame.time_epoch": "105.0",
                "frame.protocols": "eth:ip:tcp:ftp",
                "ip.src": "203.0.113.21",
                "ip.dst": "192.0.2.10",
                "tcp.srcport": "21",
                "tcp.dstport": "49154",
                "tcp.stream": "9",
                "frame.len": "70",
                "ftp.response.code": "230",
                "ftp.response.arg": "Login successful.",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "7",
                "frame.time_epoch": "106.0",
                "frame.protocols": "eth:ip:tcp:smtp",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.25",
                "tcp.srcport": "49155",
                "tcp.dstport": "25",
                "tcp.stream": "10",
                "frame.len": "90",
                "smtp.req.command": "AUTH",
                "smtp.req.parameter": "PLAIN smtp-secret",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "8",
                "frame.time_epoch": "107.0",
                "frame.protocols": "eth:ip:tcp:pop",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.110",
                "tcp.srcport": "49156",
                "tcp.dstport": "110",
                "tcp.stream": "11",
                "frame.len": "90",
                "pop.request.command": "PASS",
                "pop.request.parameter": "pop-secret",
            },
        ),
        _field_row(
            FIELD_PROFILE_FIELDS,
            **{
                "frame.number": "9",
                "frame.time_epoch": "108.0",
                "frame.protocols": "eth:ip:tcp:imap",
                "ip.src": "192.0.2.10",
                "ip.dst": "203.0.113.143",
                "tcp.srcport": "49157",
                "tcp.dstport": "143",
                "tcp.stream": "12",
                "frame.len": "90",
                "imap.request.command": "LOGIN",
                "imap.request": "user imap-secret",
            },
        ),
    ]
)


def test_tshark_semantics_public_api_contract() -> None:
    public_names = {
        name for name in EXPECTED_PUBLIC_API if hasattr(tshark_semantics, name)
    }
    public_constants = {
        name
        for name, value in vars(tshark_semantics).items()
        if not name.startswith("_") and name.isupper()
    }

    assert public_names == EXPECTED_PUBLIC_API
    assert public_constants == EXPECTED_PUBLIC_CONSTANTS

    assert callable(tshark_semantics.build_tshark_semantic_evidence)
    assert callable(tshark_semantics.build_tshark_semantic_observations)
    assert callable(tshark_semantics.fingerprint_secret)
    assert callable(tshark_semantics.normalize_tshark_field_extract_fields)
    assert callable(tshark_semantics.parse_tshark_output)

    assert tshark_semantics.TSHARK_SCHEMA_VERSION == "tshark.v1"
    assert tshark_semantics.DEFAULT_MAX_ROWS == 100
    assert (
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV
        == "DROWAI_TSHARK_SECRET_FINGERPRINT_KEY"
    )
    assert tshark_semantics.DEFAULT_SENSITIVE_PROOF_MODE == "proof_excerpt"
    assert tshark_semantics.TSHARK_FIELD_NAME_RE.fullmatch("http.request.uri")
    assert "frame.number" in tshark_semantics.TSHARK_FIELD_EXTRACT_ALLOWLIST
    assert "http.authorization" in tshark_semantics.TSHARK_FIELD_EXTRACT_ALLOWLIST

    assert tshark_semantics.normalize_tshark_field_extract_fields(
        [" frame.number ", "http.host", "http.host"]
    ) == ["frame.number", "http.host", "http.host"]
    assert tshark_semantics.normalize_tshark_field_extract_fields(None) == []

    with pytest.raises(ValueError, match="Invalid TShark field name: field"):
        tshark_semantics.normalize_tshark_field_extract_fields(["field"])
    with pytest.raises(ValueError, match="TShark field is not allowlisted"):
        tshark_semantics.normalize_tshark_field_extract_fields(["ip.addr"])


def test_refactored_common_public_constants_and_field_normalization_match_legacy() -> None:
    for name in EXPECTED_PUBLIC_CONSTANTS:
        assert_same_output(getattr(tshark_semantics, name), getattr(common, name))

    field_cases = [
        [" frame.number ", "http.host", "http.host"],
        None,
        [],
        ("dns.qry.name", "tls.handshake.version"),
    ]
    for fields in field_cases:
        assert_same_output(
            tshark_semantics.normalize_tshark_field_extract_fields(fields),
            common.normalize_tshark_field_extract_fields(fields),
        )

    for fields in (["field"], ["ip.addr"]):
        with pytest.raises(ValueError) as legacy_exc:
            tshark_semantics.normalize_tshark_field_extract_fields(fields)
        with pytest.raises(ValueError) as common_exc:
            common.normalize_tshark_field_extract_fields(fields)
        assert_same_output(str(legacy_exc.value), str(common_exc.value))


def test_refactored_common_limit_and_scalar_helpers_match_legacy() -> None:
    for value in (None, "", 0, "0", "5", 2.8, "bad", object()):
        assert_same_output(
            common._normalize_row_limit(value),
            common._normalize_row_limit(value),
        )

    for value, default in (
        (None, 0),
        ("", 7),
        (" 42 ", 0),
        ("bad", -1),
        (3.2, 0),
    ):
        assert_same_output(
            common._safe_int(value, default=default),
            common._safe_int(value, default=default),
        )

    for value in (None, "", "  text  ", 0, "3.14", "bad"):
        assert_same_output(common._none_if_empty(value), common._none_if_empty(value))
        assert_same_output(common._safe_float(value), common._safe_float(value))

    for value in (None, "", "metadata_only", " FINGERPRINT ", "raw"):
        assert_same_output(
            common._normalize_sensitive_proof_mode(value),
            common._normalize_sensitive_proof_mode(value),
        )


def test_refactored_security_sensitive_patterns_and_fingerprint_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )

    assert {
        kind: security._normalize_secret_kind(kind)
        for kind in ("authorization header", "api-key", "TOKEN", "", "protocol.auth argument")
    } == {
        "authorization header": "authorization_header",
        "api-key": "api_key",
        "TOKEN": "token",
        "": "secret",
        "protocol.auth argument": "protocol_auth_argument",
    }
    for kind in ("authorization header", "api-key", "TOKEN", "", "protocol.auth argument"):
        assert_same_output(
            tshark_semantics.fingerprint_secret("sensitive-value", kind=kind),
            security.fingerprint_secret("sensitive-value", kind=kind),
        )

    secret_cases = (
        ("http.authorization", "Bearer secret-token"),
        ("http.cookie", "session=abc"),
        ("request.command_parameter", "PASS password"),
        ("nested.payload", "password=abc123"),
        ("x_custom", "plain text"),
    )
    assert {
        (field_name, value): security.classify_secret(field_name, value)
        for field_name, value in secret_cases
    } == {
        ("http.authorization", "Bearer secret-token"): "authorization_header",
        ("http.cookie", "session=abc"): "cookie",
        ("request.command_parameter", "PASS password"): "protocol_auth_argument",
        ("nested.payload", "password=abc123"): "secret",
        ("x_custom", "plain text"): None,
    }

    nested = {
        "http": {"http.authorization": "Bearer secret-token"},
        "headers": [{"x-api-key": "abc123"}],
        "public": "hello",
    }
    assert list(security.iter_sensitive_values(nested)) == [
        ("http.http.authorization", "Bearer secret-token", "authorization_header"),
        ("headers.x-api-key", "abc123", "api_key"),
    ]


@pytest.mark.parametrize("proof_mode", ["metadata_only", "proof_excerpt", "fingerprint"])
def test_refactored_security_runtime_proof_shapes(
    monkeypatch: pytest.MonkeyPatch,
    proof_mode: str,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    raw_event = {
        "frame": "10",
        "time": "1.5",
        "stream": "3",
        "protocol": "http",
        "src": "192.0.2.10",
        "dst": "203.0.113.20",
        "field": "http.authorization",
        "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
        "extraction_filter": "http.authorization",
        "kind": "authorization header",
        "role": "secret",
        "command": "GET",
        "value": "Bearer runtime-token",
    }
    raw_sequence = {
        "stream": "3",
        "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
        "protocol": "http",
        "src": "192.0.2.10",
        "dst": "203.0.113.20",
        "frames": ["10", "11"],
        "event_count": "2",
        "username_count": "1",
        "secret_count": "1",
        "success_count": "1",
        "success_messages": ["200 OK"],
        "username_proofs": ["alice"],
        "secret_proofs": ["Bearer runtime-token"],
    }

    event = security.credential_event_with_proof(
        raw_event,
        artifact_sha256="sha",
        proof_mode=proof_mode,
    )
    assert event["kind"] == "authorization_header"
    assert event["role"] == "secret"
    assert event["proof_mode"] == proof_mode
    if proof_mode == "metadata_only":
        assert "proof_excerpt" not in event
        assert "fingerprint" not in event
    elif proof_mode == "proof_excerpt":
        assert event["proof_excerpt"] == "Bearer runtime-token"
        assert "fingerprint" not in event
    else:
        assert event["fingerprint"].startswith("hmac-sha256:authorization_header:")
        assert "proof_excerpt" not in event

    sequence = security.auth_sequence_with_proof(
        raw_sequence,
        [event],
        proof_mode=proof_mode,
    )
    assert sequence["stream"] == "3"
    assert sequence["event_count"] == 2
    assert sequence["success_count"] == 1
    assert sequence["proof_mode"] == proof_mode


@pytest.mark.parametrize("proof_mode", ["metadata_only", "proof_excerpt", "fingerprint"])
def test_refactored_security_json_parsers_emit_expected_shapes(
    monkeypatch: pytest.MonkeyPatch,
    proof_mode: str,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    rows = json.loads(JSON_PACKET_STDOUT)

    critical = security.parse_critical_signal_rows(
        rows,
        artifact_sha256="sha",
        sensitive_proof_mode=proof_mode,
    )
    auth = security.parse_auth_indicator_rows(rows, critical=critical)
    exposure = security.parse_secret_exposure_rows(
        rows,
        artifact_sha256="sha",
        sensitive_proof_mode=proof_mode,
        critical=critical,
    )
    assert len(auth["auth_indicators"]) == 2
    assert [item["field"] for item in exposure["secret_exposure"]] == [
        "http.authorization",
        "http.cookie",
    ]


@pytest.mark.parametrize("proof_mode", ["metadata_only", "proof_excerpt", "fingerprint"])
def test_refactored_security_field_rows_emit_expected_shapes(
    monkeypatch: pytest.MonkeyPatch,
    proof_mode: str,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    rows = [
        row["fields"]
        for row in field_output.parse_field_extract(FIELD_PROFILE_STDOUT, FIELD_PROFILE_FIELDS)[
            "field_extract"
        ]
    ]

    parsed = security.parse_security_field_rows(
        rows,
        artifact_sha256="sha",
        sensitive_proof_mode=proof_mode,
    )
    assert len(parsed["credential_events"]) == 10
    assert len(parsed["auth_sequences"]) == 5
    assert {item["protocol"] for item in parsed["credential_events"]} == {
        "ftp",
        "http",
        "imap",
        "pop",
        "smtp",
    }


def test_refactored_security_durable_masking_source_strings_match_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def mask(value: Any, *, source: str) -> Any:
        calls.append(source)
        if source == "tshark_secret_exposure_proof_context" and isinstance(value, Mapping):
            return {"credential": "<context-masked>"}
        return value

    monkeypatch.setattr(security, "mask_durable_secrets", mask)
    exposure = {
        "kind": "api_key",
        "field": "x-api-key",
        "proof_excerpt": "opaque-api-key",
    }

    assert (
        security.durable_mask_secret_exposure_proof_excerpt(exposure)
        == "<context-masked>"
    )
    assert calls == [
        "tshark_secret_exposure_proof",
        "tshark_secret_exposure_proof_context",
    ]


def test_refactored_semantic_observation_masking_source_string_matches_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def mask(value: Any, *, source: str) -> Any:
        calls.append(source)
        return value

    monkeypatch.setattr(semantic_emitters, "mask_durable_secrets", mask)

    metadata = {
        "analysis_mode": "pcap_summary",
        "pcap": {"input_file": "captures/example.pcap", "artifact_sha256": "sha"},
        "hosts": ["192.0.2.10"],
        "conversations": [],
    }

    assert tshark_semantics.build_tshark_semantic_observations is (
        semantic_emitters.build_tshark_semantic_observations
    )
    assert semantic_emitters.build_tshark_semantic_observations(metadata, args=None) == [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:192.0.2.10",
            "payload": {
                "source": "tshark",
                "source_tool": "tshark",
                "analysis_mode": "pcap_summary",
                "pcap_input_file": "captures/example.pcap",
                "pcap_artifact_sha256": "sha",
            },
        }
    ]
    assert calls == ["tshark_semantic_observations"]


def test_refactored_semantic_evidence_masking_source_string_matches_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def mask(value: Any, *, source: str) -> Any:
        calls.append(source)
        return value

    monkeypatch.setattr(semantic_emitters, "mask_durable_secrets", mask)

    metadata = {
        "analysis_mode": "pcap_summary",
        "pcap": {"input_file": "captures/example.pcap", "artifact_sha256": "sha"},
        "limits": {"max_rows": 20, "truncated": False, "lists": {}},
        "conversations": [],
        "secret_exposure": [],
    }

    assert tshark_semantics.build_tshark_semantic_evidence is (
        semantic_emitters.build_tshark_semantic_evidence
    )
    assert semantic_emitters.build_tshark_semantic_evidence(metadata, args=None) == [
        {
            "type": "variant",
            "name": "analysis_mode",
            "value": "pcap_summary",
            "source": "tshark",
        },
        {
            "type": "execution_parameter",
            "name": "input_file_mode",
            "value": "pcap_file",
            "source": "tshark",
        },
        {
            "type": "execution_parameter",
            "name": "max_rows",
            "value": 20,
            "source": "tshark",
            "detail": {"unit": "rows"},
        },
        {
            "type": "result_summary",
            "name": "packet_count",
            "value": 0,
            "detail": {"unit": "packets"},
            "source": "tshark",
        },
        {
            "type": "result_summary",
            "name": "conversation_count",
            "value": 0,
            "detail": {"unit": "conversations"},
            "source": "tshark",
        },
        {
            "type": "result_summary",
            "name": "secret_exposure_count",
            "value": 0,
            "detail": {"unit": "exposures"},
            "source": "tshark",
        },
    ]
    assert calls == ["tshark_semantic_evidence"]


def test_refactored_security_secret_exposure_diagnostics_and_findings() -> None:
    metadata = {
        "analysis_mode": "secret_exposure",
        "secret_exposure": [
            {
                "frame": "2",
                "time": "t2",
                "stream": "7",
                "protocol": "http",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "field": "http.authorization",
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "pcap_artifact_sha256": "sha",
                "extraction_filter": "http.authorization",
                "kind": "authorization_header",
                "proof_mode": "proof_excerpt",
                "proof_excerpt": "Bearer synthetic-http-token",
            },
            {
                "protocol": "http",
                "field": "http.cookie",
                "proof_mode": "proof_excerpt",
            },
        ],
    }
    pcap = {"input_file": "captures/example.pcap", "artifact_sha256": "sha"}
    base = security.semantic_base_payload(metadata, pcap)
    assert base == {
        "source": "tshark",
        "source_tool": "tshark",
        "analysis_mode": "secret_exposure",
        "pcap_input_file": "captures/example.pcap",
        "pcap_artifact_sha256": "sha",
    }

    finding = security.build_secret_exposure_finding(metadata["secret_exposure"][0], base)
    assert finding is not None
    assert finding["observation_type"] == "finding.vulnerability_detected"
    assert finding["payload"]["finding_subtype"] == "credential_exposure_detected"
    assert security.build_secret_exposure_finding(metadata["secret_exposure"][1], base) is None

    assert security.service_subject_from_secret_exposure(
        metadata["secret_exposure"][0]
    ) == ("service.socket:203.0.113.20/tcp/80", "203.0.113.20", "tcp", 80, None)
    assert security.secret_exposure_specificity_gap(metadata["secret_exposure"][1]) == (
        "missing_subject"
    )
    assert security.weak_secret_exposure_diagnostic(metadata["secret_exposure"][1]) == {
        "reason": "missing_subject",
        "field": "http.cookie",
        "protocol": "http",
        "frame": "",
        "stream": "",
        "source": "tshark",
    }
    assert security.compact_packet_proof(metadata) == (
        "protocol=http frame=2 stream=7 field=http.authorization "
        "proof_mode=proof_excerpt proof=Bearer <DURABLE_SECRET_MASK:token>"
    )


def test_refactored_semantic_observation_builder_matches_legacy_baseline_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    baseline_metadata = [
        _facade_parse(
            JSON_PACKET_STDOUT,
            "",
            analysis_mode="pcap_summary",
            artifact_sha256="sha",
            max_rows=20,
        ),
        _facade_parse(
            JSON_PACKET_STDOUT,
            "",
            analysis_mode="secret_exposure",
            artifact_sha256="sha",
            sensitive_proof_mode="proof_excerpt",
            max_rows=20,
        ),
        _facade_parse(
            JSON_PACKET_STDOUT,
            "",
            analysis_mode="secret_exposure",
            artifact_sha256="sha",
            sensitive_proof_mode="fingerprint",
            max_rows=20,
        ),
        _facade_parse(
            FIELD_PROFILE_STDOUT,
            "",
            analysis_mode="find_security_relevant_artifacts",
            fields=FIELD_PROFILE_FIELDS,
            artifact_sha256="sha",
            max_rows=20,
        ),
    ]

    for metadata in baseline_metadata:
        legacy_metadata = copy.deepcopy(metadata)
        refactored_metadata = copy.deepcopy(metadata)

        assert_same_output(
            tshark_semantics.build_tshark_semantic_observations(
                legacy_metadata,
                args=None,
            ),
            semantic_emitters.build_tshark_semantic_observations(
                refactored_metadata,
                args=None,
            ),
        )
        assert_same_output(legacy_metadata, refactored_metadata)


def test_refactored_semantic_evidence_builder_matches_legacy_baseline_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    baseline_metadata = [
        (
            _facade_parse(
                JSON_PACKET_STDOUT,
                "",
                analysis_mode="pcap_summary",
                artifact_sha256="sha",
                max_rows=20,
            ),
            SimpleNamespace(
                analysis_mode="pcap_summary",
                input_file="captures/example.pcap",
                max_rows=20,
                display_filter="http",
                capture_filter="tcp port 80",
                fields=["frame.number", "ip.src"],
            ),
        ),
        (
            _facade_parse(
                JSON_PACKET_STDOUT,
                "",
                analysis_mode="secret_exposure",
                artifact_sha256="sha",
                sensitive_proof_mode="proof_excerpt",
                max_rows=20,
            ),
            None,
        ),
        (
            _facade_parse(
                JSON_PACKET_STDOUT,
                "",
                analysis_mode="secret_exposure",
                artifact_sha256="sha",
                sensitive_proof_mode="fingerprint",
                max_rows=20,
            ),
            None,
        ),
        (
            _facade_parse(
                FIELD_PROFILE_STDOUT,
                "",
                analysis_mode="find_security_relevant_artifacts",
                fields=FIELD_PROFILE_FIELDS,
                artifact_sha256="sha",
                max_rows=20,
            ),
            None,
        ),
        (
            _facade_parse(
                JSON_PACKET_STDOUT,
                "",
                analysis_mode="pcap_summary",
                max_rows=2,
            ),
            None,
        ),
    ]

    for metadata, args in baseline_metadata:
        assert_same_output(
            tshark_semantics.build_tshark_semantic_evidence(
                copy.deepcopy(metadata),
                args=args,
            ),
            semantic_emitters.build_tshark_semantic_evidence(
                copy.deepcopy(metadata),
                args=args,
            ),
        )


def test_refactored_semantic_observation_helpers() -> None:
    conversation_cases = [
        {"dst": "203.0.113.20", "dst_port": "80", "protocol": "tcp"},
        {"dst": "203.0.113.20", "dst_port": "80", "protocol": "http"},
        {"dst": "203.0.113.53", "dst_port": "53", "protocol": "dns"},
        {"dst": "bad host", "dst_port": "80", "protocol": "tcp"},
        {"dst": "203.0.113.20", "dst_port": "bad", "protocol": "tcp"},
    ]
    assert [
        semantic_emitters.service_subject_from_conversation(conversation)
        for conversation in conversation_cases
    ] == [
        ("service.socket:203.0.113.20/tcp/80", "203.0.113.20", "tcp", 80, None),
        ("service.socket:203.0.113.20/tcp/80", "203.0.113.20", "tcp", 80, "http"),
        ("service.socket:203.0.113.53/udp/53", "203.0.113.53", "udp", 53, "dns"),
        None,
        None,
    ]

    observations = [{"b": 1, "a": 2}, {"a": 2, "b": 1}, {"a": 3}]
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for observation in observations:
        semantic_emitters.append_observation(output, seen, observation)

    assert output == [{"b": 1, "a": 2}, {"a": 3}]
    assert len(seen) == 2


def test_refactored_semantic_observation_diagnostics_mutation() -> None:
    diagnostics = [
        {"reason": f"weak_{index}", "source": "tshark"}
        for index in range(common.DEFAULT_MAX_ROWS + 5)
    ]
    metadata = {
        "semantic_observation_diagnostics": [{"reason": "existing", "source": "tshark"}]
    }

    semantic_emitters.store_semantic_observation_diagnostics(
        metadata,
        diagnostics,
    )

    stored = metadata["semantic_observation_diagnostics"]
    assert stored[0] == {"reason": "existing", "source": "tshark"}
    assert len(stored) == common.DEFAULT_MAX_ROWS
    assert stored[-1] == {"reason": "weak_98", "source": "tshark"}


def test_refactored_security_does_not_duplicate_pcap_analysis_logic() -> None:
    source = inspect.getsource(security)

    assert "from agent.tools.pcap_analysis import extract_critical_signals" in source
    assert "def extract_critical_signals" not in source
    assert "extract_critical_signals(rows)" in source


def test_refactored_common_bounded_list_helper_matches_legacy_side_effects() -> None:
    for rows, max_rows in ((range(5), 3), (["a", "b"], 5), ([], 1)):
        legacy_limits = {"max_rows": max_rows, "truncated": False, "lists": {}}
        common_limits = {"max_rows": max_rows, "truncated": False, "lists": {}}

        assert_same_output(
            common._bounded_list("rows", rows, max_rows, legacy_limits),
            common._bounded_list("rows", rows, max_rows, common_limits),
        )
        assert_same_output(legacy_limits, common_limits)


def test_refactored_common_packet_context_helpers_match_legacy() -> None:
    field_row = {
        "frame.number": " 7 ",
        "frame.time_epoch": "101.5",
        "frame.protocols": "eth:ip:tcp",
        "ip.src": "192.0.2.10",
        "ip.dst": "198.51.100.20",
        "tcp.srcport": "51515",
        "tcp.dstport": "80",
        "tcp.stream": "2",
        "tcp.len": "0",
        "frame.len": "128",
        "http.host": "example.test",
        "http.request.uri": "/login",
    }
    layers = {
        "frame": {
            "frame.number": "7",
            "frame.time_relative": "0.200",
            "frame.time_epoch": "101.5",
            "frame.protocols": "eth:ip:tcp:http",
            "frame.len": "128",
        },
        "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.20"},
        "tcp": {"tcp.srcport": "51515", "tcp.dstport": "80", "tcp.stream": "2"},
        "http": {"http.host": "example.test"},
    }
    row = {"_source": {"layers": layers}}

    for helper, args in (
        ("_field_row_context", (field_row,)),
        ("_field_rows_to_packet_rows", ([field_row],)),
        ("_packet_layers", (row,)),
        ("_packet_context", (layers,)),
    ):
        assert_same_output(
            getattr(common, helper)(*args),
            getattr(common, helper)(*args),
        )

    context = common._packet_context(layers)
    legacy_context = common._packet_context(layers)
    for helper, args in (
        ("_application_protocol", (context,)),
        ("_flow_key", (context,)),
        ("_normalize_tshark_field_path", ("http.http.host",)),
        ("_field_values", (layers["http"], "http.host")),
        ("_first_field_value", (layers["http"], "http.host")),
        ("_first_available_field", (layers["http"], "http.missing", "http.host")),
    ):
        legacy_args = (legacy_context,) if args == (context,) else args
        assert_same_output(
            getattr(common, helper)(*legacy_args),
            getattr(common, helper)(*args),
        )


def test_refactored_field_extract_warnings_and_row_numbering_match_legacy() -> None:
    fields = ["frame.number", "field", "ip.addr", "http.host"]
    stdout = "\n".join(
        [
            "Warning: decoder note before fields",
            "1\tignored-invalid\tignored-unsupported\tapp.example.test\textra",
            "Malformed line from TShark",
            "2",
        ]
    )

    assert_same_output(
        field_output.parse_field_extract(stdout, fields),
        field_output.parse_field_extract(stdout, fields),
    )
    assert field_output.parse_field_extract(stdout, fields) == {
        "packet_count": 2,
        "field_extract": [
            {
                "row": 1,
                "fields": {
                    "frame.number": "1",
                    "http.host": "app.example.test",
                },
            },
            {
                "row": 2,
                "fields": {
                    "frame.number": "2",
                    "http.host": None,
                },
            },
        ],
        "warnings": [
            "Invalid TShark field name: field",
            "TShark field is not allowlisted: ip.addr",
            "discarded non-data field row 1: Warning: decoder note before fields",
            "field_extract row 2 has 5 columns for 4 fields.",
            "discarded non-data field row 3: Malformed line from TShark",
            "field_extract row 4 has 1 columns for 4 fields.",
        ],
    }


def test_refactored_field_extract_empty_and_non_frame_diagnostics_match_legacy() -> None:
    assert_same_output(
        field_output.parse_field_extract("plain diagnostic", []),
        field_output.parse_field_extract("plain diagnostic", []),
    )
    assert_same_output(
        field_output.parse_field_extract("plain diagnostic", ["http.host"]),
        field_output.parse_field_extract("plain diagnostic", ["http.host"]),
    )


def test_refactored_profile_field_output_orchestration_matches_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )

    for mode, proof_mode in (
        ("survey", "proof_excerpt"),
        ("extract_evidence", "proof_excerpt"),
        ("find_security_relevant_artifacts", "fingerprint"),
        ("investigate_protocol", "metadata_only"),
    ):
        assert_same_output(
            field_output.parse_profile_field_output(
                FIELD_PROFILE_STDOUT,
                FIELD_PROFILE_FIELDS,
                mode=mode,
                artifact_sha256="sha",
                sensitive_proof_mode=proof_mode,
            ),
            field_output.parse_profile_field_output(
                FIELD_PROFILE_STDOUT,
                FIELD_PROFILE_FIELDS,
                mode=mode,
                artifact_sha256="sha",
                sensitive_proof_mode=proof_mode,
            ),
        )


def test_refactored_parse_orchestrator_matches_facade_phase1_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )

    scenarios: list[tuple[str, str, dict[str, Any]]] = [
        (
            "",
            "",
            {
                "analysis_mode": "http",
                "input_file": "captures/example.pcap",
                "max_rows": 5,
            },
        ),
        (
            '[{"_source": ',
            "Warning: decoder warning\nError: decoder error",
            {"analysis_mode": "http", "max_rows": 5},
        ),
        (
            JSON_PACKET_STDOUT,
            "",
            {
                "analysis_mode": "pcap_summary",
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "dns", "max_rows": 20}),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "http", "max_rows": 20}),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "tls", "max_rows": 20}),
        (
            JSON_PACKET_STDOUT,
            "",
            {"analysis_mode": "investigate_protocol", "max_rows": 20},
        ),
        (
            JSON_PACKET_STDOUT,
            "",
            {
                "analysis_mode": "find_security_relevant_artifacts",
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (
            JSON_PACKET_STDOUT,
            "",
            {
                "analysis_mode": "secret_exposure",
                "artifact_sha256": "sha",
                "sensitive_proof_mode": "metadata_only",
                "max_rows": 20,
            },
        ),
        (
            JSON_PACKET_STDOUT,
            "",
            {
                "analysis_mode": "secret_exposure",
                "artifact_sha256": "sha",
                "sensitive_proof_mode": "proof_excerpt",
                "max_rows": 20,
            },
        ),
        (
            JSON_PACKET_STDOUT,
            "",
            {
                "analysis_mode": "secret_exposure",
                "artifact_sha256": "sha",
                "sensitive_proof_mode": "fingerprint",
                "max_rows": 20,
            },
        ),
        (
            FIELD_PROFILE_STDOUT,
            "",
            {
                "analysis_mode": "survey",
                "fields": FIELD_PROFILE_FIELDS,
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (
            FIELD_PROFILE_STDOUT,
            "",
            {
                "analysis_mode": "extract_evidence",
                "fields": FIELD_PROFILE_FIELDS,
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (
            FIELD_PROFILE_STDOUT,
            "",
            {
                "analysis_mode": "find_security_relevant_artifacts",
                "fields": FIELD_PROFILE_FIELDS,
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (
            FIELD_PROFILE_STDOUT,
            "",
            {
                "analysis_mode": "investigate_protocol",
                "fields": FIELD_PROFILE_FIELDS,
                "artifact_sha256": "sha",
                "max_rows": 20,
            },
        ),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "pcap_summary", "max_rows": 2}),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "pcap_summary", "max_rows": "bad"}),
        (JSON_PACKET_STDOUT, "", {"analysis_mode": "pcap_summary", "max_rows": 0}),
        (
            "1 0.000000 192.0.2.10 -> 198.51.100.53 DNS Standard query",
            "Warning: text warning",
            {"analysis_mode": "dns", "max_rows": 20},
        ),
    ]

    for stdout, stderr, kwargs in scenarios:
        assert_same_output(
            tshark_semantics.parse_tshark_output(stdout, stderr, **kwargs),
            parser.parse_tshark_output(stdout, stderr, **kwargs),
        )


def test_refactored_parse_orchestrator_import_boundary() -> None:
    source = inspect.getsource(parser)

    forbidden = (
        "tshark.py",
        "backend.",
        "workspace_root",
        "resolve_workspace",
        "subprocess",
        "BaseTool",
        "ToolResult",
    )
    for marker in forbidden:
        assert marker not in source


def test_refactored_json_packet_loader_matches_legacy_for_complete_and_truncated_arrays() -> None:
    for stdout, max_rows in (
        (JSON_PACKET_STDOUT, 20),
        (JSON_PACKET_STDOUT, 2),
        (_json_packets({"frame": {"frame.number": "1"}}, {"bad": "row"}), 1),
        ("[]", 5),
        (json.dumps({"_source": {"layers": {}}}), 5),
    ):
        assert_same_output(
            json_packets.load_json_packets(stdout, max_rows=max_rows),
            json_packets.load_json_packets(stdout, max_rows=max_rows),
        )


def test_refactored_json_packet_loader_matches_legacy_for_diagnostic_fallback_text() -> None:
    stdout = "\n".join(
        [
            "    1 0.000000 192.0.2.10 -> 198.51.100.53 DNS Standard query",
            "    2 0.010000 192.0.2.10 -> 203.0.113.20 TCP 51515 -> 80",
            "Warning: Short frame ignored",
        ]
    )

    assert_same_output(
        json_packets.load_json_packets(stdout, max_rows=5),
        json_packets.load_json_packets(stdout, max_rows=5),
    )
    assert_same_output(
        json_packets.parse_text_packets(stdout),
        json_packets.parse_text_packets(stdout),
    )


def test_refactored_json_packet_loader_matches_legacy_for_multiline_partial_json() -> None:
    stdout = "\n".join(
        [
            "[",
            json.dumps({"_source": {"layers": {"frame": {"frame.number": "1"}}}}),
            ",",
            "Warning: capture ended while decoding packet",
        ]
    )

    assert_same_output(
        json_packets.load_json_packets(stdout, max_rows=5),
        json_packets.load_json_packets(stdout, max_rows=5),
    )


def test_refactored_json_packet_summary_matches_legacy_packet_shape() -> None:
    rows, truncated = json_packets.load_json_packets(JSON_PACKET_STDOUT, max_rows=20)

    assert truncated is False
    assert rows is not None
    assert_same_output(
        json_packets.load_json_packets(JSON_PACKET_STDOUT, max_rows=20),
        (rows, truncated),
    )

    legacy_summary = json_packets.parse_json_packet_summary(rows)
    refactored_summary = json_packets.parse_json_packet_summary(rows)

    for key in ("packet_count", "duration_seconds", "protocols", "hosts", "conversations"):
        assert_same_output(legacy_summary[key], refactored_summary[key])


def test_refactored_json_conversation_and_text_parsers_match_legacy() -> None:
    rows, _ = json_packets.load_json_packets(JSON_PACKET_STDOUT, max_rows=20)
    assert rows is not None

    assert_same_output(
        json_packets.parse_conversation_rows(rows),
        json_packets.parse_conversation_rows(rows),
    )

    text_output = "\n".join(
        [
            "1 0.000000 192.0.2.10 -> 198.51.100.53 DNS Standard query",
            "2 0.010000 203.0.113.20 -> 192.0.2.10 HTTP/1.1 200 OK",
        ]
    )
    assert_same_output(
        json_packets.parse_text_packets(text_output),
        json_packets.parse_text_packets(text_output),
    )


def test_refactored_dns_json_parser_matches_legacy_records_and_warnings() -> None:
    rows = [
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "10",
                        "frame.time": "t10",
                        "frame.protocols": "eth:ip:udp:dns",
                    },
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.53"},
                    "udp": {"udp.srcport": "53000", "udp.dstport": "53"},
                    "dns": {
                        "dns.qry.name": ["a.example.test", "b.example.test"],
                        "dns.qry.type": ["1"],
                        "dns.a": ["198.51.100.20", "198.51.100.20"],
                        "dns.aaaa": "2001:db8::20",
                        "dns.cname": "alias.example.test",
                        "dns.flags.rcode": "3",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "11",
                        "frame.time_relative": "0.200",
                        "frame.protocols": "eth:ip:udp:dns",
                    },
                    "ip": {"ip.src": "198.51.100.53", "ip.dst": "192.0.2.10"},
                    "udp": {"udp.srcport": "53", "udp.dstport": "53000"},
                    "dns": {
                        "dns.resp.name": ["fallback.example.test", "198.51.100.30"],
                        "dns.flags.rcode": "0",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {"frame.number": "12", "frame.protocols": "eth:ip:tcp"},
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
                    "tcp": {"tcp.srcport": "51515", "tcp.dstport": "80"},
                }
            }
        },
    ]

    assert_same_output(
        dns.parse_dns_rows(rows),
        dns.parse_dns_rows(rows),
    )
    assert_same_output(
        dns.parse_dns_rows([]),
        dns.parse_dns_rows([]),
    )
    no_dns_rows = [rows[-1]]
    assert_same_output(
        dns.parse_dns_rows(no_dns_rows),
        dns.parse_dns_rows(no_dns_rows),
    )
    assert dns.parse_dns_rows(no_dns_rows)["warnings"] == [
        "No DNS records found in TShark JSON output."
    ]


def test_refactored_dns_field_parser_matches_legacy_records() -> None:
    rows = [
        {
            "frame.number": "1",
            "frame.time_epoch": "100.0",
            "frame.protocols": "eth:ip:udp:dns",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.53",
            "udp.srcport": "53123",
            "udp.dstport": "53",
            "dns.qry.name": "www.example.test",
            "dns.qry.type": "1",
            "dns.a": "198.51.100.10",
            "dns.aaaa": "2001:db8::10",
            "dns.cname": "alias.example.test",
            "dns.flags.rcode": "0",
        },
        {
            "frame.number": "1",
            "frame.time_epoch": "100.0",
            "frame.protocols": "eth:ip:udp:dns",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.53",
            "udp.srcport": "53123",
            "udp.dstport": "53",
            "dns.qry.name": "www.example.test",
            "dns.qry.type": "1",
            "dns.a": "198.51.100.10",
            "dns.aaaa": "2001:db8::10",
            "dns.cname": "alias.example.test",
            "dns.flags.rcode": "0",
        },
        {
            "frame.number": "2",
            "frame.time": "t2",
            "frame.protocols": "eth:ip:udp:dns",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.53",
            "udp.srcport": "53124",
            "udp.dstport": "53",
            "dns.qry.type": "28",
            "dns.aaaa": "2001:db8::20",
            "dns.flags.rcode": "2",
        },
        {
            "frame.number": "3",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.20",
            "http.host": "app.example.test",
        },
    ]

    assert_same_output(
        dns.parse_dns_field_rows(rows),
        dns.parse_dns_field_rows(rows),
    )


def test_refactored_http_json_parser_matches_legacy_records_headers_and_warnings() -> None:
    rows = [
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "20",
                        "frame.time": "t20",
                        "frame.protocols": "eth:ip:tcp:http",
                    },
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.80"},
                    "tcp": {"tcp.srcport": "51515", "tcp.dstport": "80", "tcp.stream": "3"},
                    "http": {
                        "http.host": "app.example.test",
                        "http.request.method": "POST",
                        "http.request.uri": "http://app.example.test/login?next=/admin",
                        "http.response.code": "302",
                        "http.user_agent": "SyntheticAgent/2.0",
                        "http.server": "nginx",
                        "http.authorization": ["Basic abc123", "Bearer token456"],
                        "http.proxy_authorization": "Proxy secret",
                        "http.cookie": {"cookie_a": "session=one", "cookie_b": "theme=dark"},
                        "http.set_cookie": "session=two; Path=/",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "21",
                        "frame.time_relative": "0.300",
                        "frame.protocols": "eth:ip:tcp:http",
                    },
                    "ip": {"ip.src": "203.0.113.80", "ip.dst": "192.0.2.10"},
                    "tcp": {"tcp.srcport": "80", "tcp.dstport": "51515", "tcp.stream": "3"},
                    "http": {
                        "http.request.uri": "*",
                        "http.response.code": "204",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {"frame.number": "22", "frame.protocols": "eth:ip:udp:dns"},
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.53"},
                    "udp": {"udp.srcport": "53000", "udp.dstport": "53"},
                }
            }
        },
    ]

    legacy = http.parse_http_rows(rows)
    refactored = http.parse_http_rows(rows)

    assert_same_output(legacy, refactored)
    assert_same_output(
        legacy["http"][0]["headers"],
        {
            "http.authorization": ["Basic abc123", "Bearer token456"],
            "http.proxy_authorization": ["Proxy secret"],
            "http.cookie": ["session=one", "theme=dark"],
            "http.set_cookie": ["session=two; Path=/"],
        },
    )
    assert_same_output(legacy["http"][0]["path"], "/login?next=/admin")
    assert_same_output(legacy["http"][1]["path"], "*")
    assert_same_output(
        http.parse_http_rows([]),
        http.parse_http_rows([]),
    )
    no_http_rows = [rows[-1]]
    assert_same_output(
        http.parse_http_rows(no_http_rows),
        http.parse_http_rows(no_http_rows),
    )
    assert http.parse_http_rows(no_http_rows)["warnings"] == [
        "No HTTP records found in TShark JSON output."
    ]


def test_refactored_http_field_parser_matches_legacy_records_and_header_values() -> None:
    rows = [
        {
            "frame.number": "30",
            "frame.time_epoch": "200.0",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.80",
            "tcp.srcport": "51515",
            "tcp.dstport": "80",
            "tcp.stream": "4",
            "frame.len": "512",
            "http.host": "app.example.test",
            "http.request.method": "GET",
            "http.request.uri": "http://app.example.test/search?q=token",
            "http.response.code": "",
            "http.user_agent": "SyntheticAgent/3.0",
            "http.content_type": "text/html",
            "http.authorization": "Bearer field-token",
            "http.cookie": "session=field-cookie",
            "http.set_cookie": "pref=field; Path=/",
        },
        {
            "frame.number": "31",
            "frame.time": "t31",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "203.0.113.80",
            "ip.dst": "192.0.2.10",
            "tcp.srcport": "80",
            "tcp.dstport": "51515",
            "tcp.stream": "4",
            "http.request.uri": "/relative/path",
            "http.response.code": "200",
        },
        {
            "frame.number": "32",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.80",
            "tcp.srcport": "51516",
            "tcp.dstport": "80",
            "tcp.stream": "5",
            "http.authorization": "Bearer skipped-without-http-row-key",
            "http.cookie": "session=skipped",
        },
        {
            "frame.number": "33",
            "frame.protocols": "eth:ip:udp:dns",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.53",
            "udp.srcport": "53000",
            "udp.dstport": "53",
            "dns.qry.name": "www.example.test",
        },
    ]

    legacy = http.parse_http_field_rows(rows)
    refactored = http.parse_http_field_rows(rows)

    assert_same_output(legacy, refactored)
    assert_same_output(
        legacy[0]["headers"],
        {
            "http.authorization": "Bearer field-token",
            "http.cookie": "session=field-cookie",
            "http.set_cookie": "pref=field; Path=/",
        },
    )
    assert_same_output(legacy[0]["path"], "/search?q=token")
    assert_same_output(legacy[1]["path"], "/relative/path")
    assert len(legacy) == 2


def test_refactored_tls_json_parser_matches_legacy_records_warnings_and_ssl_fields() -> None:
    rows = [
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "40",
                        "frame.time": "t40",
                        "frame.protocols": "eth:ip:tcp:tls",
                    },
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.443"},
                    "tcp": {"tcp.srcport": "51517", "tcp.dstport": "443", "tcp.stream": "6"},
                    "tls": {
                        "tls.handshake.extensions_server_name": "secure.example.test",
                        "tls.handshake.extensions_alpn_str": ["h2", "http/1.1"],
                        "tls.handshake.version": ["0x0303", "0x0303"],
                        "tls.record.version": "0x0301",
                        "ssl.handshake.version": "0x0300",
                        "tls.alert_message.desc": "close_notify",
                    },
                    "x509sat": {
                        "x509sat.subject": "CN=secure.example.test",
                        "x509sat.issuer": "CN=Issuer Test CA",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {
                        "frame.number": "41",
                        "frame.time_relative": "0.400",
                        "frame.protocols": "eth:ip:tcp:ssl",
                    },
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.443"},
                    "tcp": {"tcp.srcport": "51518", "tcp.dstport": "443", "tcp.stream": "7"},
                    "ssl": {
                        "ssl.handshake.extensions_server_name": "legacy-ssl.example.test",
                        "ssl.handshake.version": "0x0301",
                        "ssl.record.version": "0x0300",
                    },
                    "x509sat": {
                        "x509sat.issuer": "CN=Legacy Issuer",
                        "x509sat.subject": "CN=legacy-ssl.example.test",
                    },
                }
            }
        },
        {
            "_source": {
                "layers": {
                    "frame": {"frame.number": "42", "frame.protocols": "eth:ip:tcp:http"},
                    "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.80"},
                    "tcp": {"tcp.srcport": "51519", "tcp.dstport": "80"},
                }
            }
        },
    ]

    legacy = tls.parse_tls_rows(rows)
    refactored = tls.parse_tls_rows(rows)

    assert_same_output(legacy, refactored)
    assert_same_output(
        legacy["tls"][0],
        {
            "frame": "40",
            "time": "t40",
            "stream": "6",
            "sni": "secure.example.test",
            "alpn": ["h2", "http/1.1"],
            "subject": "CN=secure.example.test",
            "issuer": "CN=Issuer Test CA",
            "versions": ["0x0300", "0x0301", "0x0303"],
            "src": "192.0.2.10",
            "dst": "203.0.113.443",
        },
    )
    assert_same_output(
        legacy["tls"][1],
        {
            "frame": "41",
            "time": "0.400",
            "stream": "7",
            "sni": "legacy-ssl.example.test",
            "alpn": [],
            "subject": "CN=legacy-ssl.example.test",
            "issuer": "CN=Legacy Issuer",
            "versions": ["0x0300", "0x0301"],
            "src": "192.0.2.10",
            "dst": "198.51.100.443",
        },
    )
    assert_same_output(
        tls.parse_tls_rows([]),
        tls.parse_tls_rows([]),
    )
    no_tls_rows = [rows[-1]]
    assert_same_output(
        tls.parse_tls_rows(no_tls_rows),
        tls.parse_tls_rows(no_tls_rows),
    )
    assert tls.parse_tls_rows(no_tls_rows)["warnings"] == [
        "No TLS records found in TShark JSON output."
    ]


def test_refactored_tls_field_parser_matches_legacy_records_alerts_and_ssl_skips() -> None:
    rows = [
        {
            "frame.number": "50",
            "frame.time_epoch": "300.0",
            "frame.protocols": "eth:ip:tcp:tls",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.443",
            "tcp.srcport": "51520",
            "tcp.dstport": "443",
            "tcp.stream": "8",
            "tls.handshake.extensions_server_name": "secure.example.test",
            "tls.handshake.version": "0x0303",
            "x509sat.printableString": "CN=secure.example.test",
        },
        {
            "frame.number": "51",
            "frame.time": "t51",
            "frame.protocols": "eth:ip:tcp:tls",
            "ip.src": "203.0.113.443",
            "ip.dst": "192.0.2.10",
            "tcp.srcport": "443",
            "tcp.dstport": "51520",
            "tcp.stream": "8",
            "tls.alert_message.desc": "close_notify",
        },
        {
            "frame.number": "52",
            "frame.protocols": "eth:ip:tcp:tls",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.443",
            "tcp.srcport": "51521",
            "tcp.dstport": "443",
            "tcp.stream": "9",
            "tls.handshake.type": "1",
        },
        {
            "frame.number": "53",
            "frame.protocols": "eth:ip:tcp:ssl",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.443",
            "tcp.srcport": "51522",
            "tcp.dstport": "443",
            "tcp.stream": "10",
            "ssl.handshake.extensions_server_name": "legacy-ssl.example.test",
            "ssl.handshake.version": "0x0301",
        },
    ]

    legacy = tls.parse_tls_field_rows(rows)
    refactored = tls.parse_tls_field_rows(rows)

    assert_same_output(legacy, refactored)
    assert legacy == [
        {
            "frame": "50",
            "time": "300.0",
            "stream": "8",
            "sni": "secure.example.test",
            "subject": "CN=secure.example.test",
            "issuer": None,
            "versions": ["0x0303"],
            "src": "192.0.2.10",
            "dst": "203.0.113.443",
        },
        {
            "frame": "51",
            "time": "t51",
            "stream": "8",
            "sni": None,
            "subject": None,
            "issuer": None,
            "versions": [],
            "src": "203.0.113.443",
            "dst": "192.0.2.10",
        },
        {
            "frame": "52",
            "time": None,
            "stream": "9",
            "sni": None,
            "subject": None,
            "issuer": None,
            "versions": [],
            "src": "192.0.2.10",
            "dst": "203.0.113.443",
        },
    ]


def test_refactored_ftp_field_parser_matches_legacy_request_response_metadata() -> None:
    rows = [
        {
            "frame.number": "60",
            "frame.time_epoch": "400.0",
            "frame.protocols": "eth:ip:tcp:ftp",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.21",
            "tcp.srcport": "51521",
            "tcp.dstport": "21",
            "tcp.stream": "12",
            "tcp.len": "18",
            "frame.len": "74",
            "ftp.request.command": "USER",
            "ftp.request.arg": "nathan",
        },
        {
            "frame.number": "61",
            "frame.time": "t61",
            "frame.protocols": "eth:ip:tcp:ftp",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.21",
            "tcp.srcport": "51521",
            "tcp.dstport": "21",
            "tcp.stream": "12",
            "tcp.len": "22",
            "frame.len": "86",
            "ftp.request.command": "PASS",
            "ftp.request.arg": "Buck3tH4TF0RM3!",
        },
        {
            "frame.number": "62",
            "frame.time_epoch": "402.0",
            "frame.protocols": "eth:ip:tcp:ftp",
            "ip.src": "203.0.113.21",
            "ip.dst": "192.0.2.10",
            "tcp.srcport": "21",
            "tcp.dstport": "51521",
            "tcp.stream": "12",
            "frame.len": "70",
            "ftp.response.code": "230",
            "ftp.response.arg": "Login successful.",
        },
        {
            "frame.number": "63",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.80",
            "tcp.srcport": "51522",
            "tcp.dstport": "80",
            "tcp.stream": "13",
            "http.host": "app.example.test",
        },
    ]

    legacy = ftp.parse_ftp_field_rows(rows)
    refactored = ftp.parse_ftp_field_rows(rows)

    assert_same_output(legacy, refactored)
    assert legacy == [
        {
            "frame": "60",
            "time": "400.0",
            "stream": "12",
            "src": "192.0.2.10",
            "dst": "203.0.113.21",
            "src_port": "51521",
            "dst_port": "21",
            "request_command": "USER",
            "request_arg": "nathan",
            "response_code": None,
            "response_arg": None,
            "tcp_len": "18",
            "frame_len": "74",
        },
        {
            "frame": "61",
            "time": "t61",
            "stream": "12",
            "src": "192.0.2.10",
            "dst": "203.0.113.21",
            "src_port": "51521",
            "dst_port": "21",
            "request_command": "PASS",
            "request_arg": "Buck3tH4TF0RM3!",
            "response_code": None,
            "response_arg": None,
            "tcp_len": "22",
            "frame_len": "86",
        },
        {
            "frame": "62",
            "time": "402.0",
            "stream": "12",
            "src": "203.0.113.21",
            "dst": "192.0.2.10",
            "src_port": "21",
            "dst_port": "51521",
            "request_command": None,
            "request_arg": None,
            "response_code": "230",
            "response_arg": "Login successful.",
            "tcp_len": None,
            "frame_len": "70",
        },
    ]


def test_refactored_ftp_survey_command_signals_match_legacy_subset() -> None:
    rows = [
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": "USER"},
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": "PASS"},
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": "SYST"},
        {"frame.protocols": "eth:ip:tcp:http", "ftp.request.command": "PASS"},
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": ""},
    ]

    for fields in rows:
        assert_same_output(
            [
                signal
                for signal in survey.survey_row_signals(fields)
                if signal in {"cleartext_ftp_auth_command", "ftp_control_command"}
            ],
            ftp.survey_ftp_command_signals(fields),
        )


def test_refactored_mail_survey_auth_command_signals_match_legacy_subset() -> None:
    rows = [
        {"frame.protocols": "eth:ip:tcp:smtp", "smtp.req.command": "AUTH"},
        {"frame.protocols": "eth:ip:tcp:smtp", "smtp.req.command": "mail"},
        {"frame.protocols": "eth:ip:tcp:pop", "pop.request.command": "USER"},
        {"frame.protocols": "eth:ip:tcp:pop", "pop.request.command": "PASS"},
        {"frame.protocols": "eth:ip:tcp:imap", "imap.request.command": "LOGIN"},
        {"frame.protocols": "eth:ip:tcp:imap", "imap.request.command": "AUTHENTICATE"},
        {"frame.protocols": "eth:ip:tcp:imap", "imap.request.command": "SELECT"},
    ]

    for fields in rows:
        assert_same_output(
            [
                signal
                for signal in survey.survey_row_signals(fields)
                if signal in {"smtp_auth_command", "pop_auth_command", "imap_auth_command"}
            ],
            mail.survey_mail_auth_command_signals(fields),
        )


def test_refactored_mail_protocol_hints_match_legacy_for_mail_protocols() -> None:
    values = [
        {"protocols": ["eth", "ip", "tcp", "smtp"], "dst_port": "25"},
        {"protocols": {"eth", "ip", "tcp", "pop"}, "dst_port": "110"},
        {"protocols": ["eth", "ip", "tcp", "imap"], "dst_port": "143"},
        {"protocols": [], "dst_port": "465"},
        {"protocols": [], "dst_port": "587"},
        {"protocols": [], "dst_port": "993"},
        {"protocols": [], "dst_port": "995"},
        {"protocols": [], "dst_port": "2525"},
    ]

    for value in values:
        legacy_hint = survey.survey_protocol_hint(value)
        if legacy_hint in mail.MAIL_PROTOCOLS or legacy_hint is None:
            assert_same_output(legacy_hint, mail.mail_protocol_hint(value))


def test_refactored_mail_survey_reason_and_intent_match_legacy_subset() -> None:
    values = [
        ("smtp", ["smtp_auth_command"]),
        ("pop", ["pop_auth_command"]),
        ("imap", ["imap_auth_command"]),
        ("smtp", []),
        ("pop", []),
        ("imap", []),
        (None, ["smtp_auth_command"]),
        (None, []),
    ]

    for protocol_hint, signals in values:
        assert_same_output(
            survey.survey_reason_and_intent(protocol_hint, signals),
            mail.survey_mail_reason_and_intent(protocol_hint, signals),
        )


def test_refactored_survey_profile_summary_matches_legacy() -> None:
    rows = [
        row["fields"]
        for row in field_output.parse_field_extract(FIELD_PROFILE_STDOUT, FIELD_PROFILE_FIELDS)[
            "field_extract"
        ]
    ]

    assert_same_output(
        survey.parse_profile_field_summary(rows),
        survey.parse_profile_field_summary(rows),
    )


def test_refactored_survey_protocol_inference_from_field_rows_matches_legacy() -> None:
    rows = [
        {
            "frame.number": "1",
            "frame.time_epoch": "1.0",
            "frame.protocols": "",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.20",
            "tcp.srcport": "51515",
            "tcp.dstport": "80",
            "tcp.stream": "2",
            "frame.len": "128",
            "http.host": "example.test",
            "http.response.code": "404",
        },
        {
            "frame.number": "2",
            "frame.time_epoch": "2.0",
            "frame.protocols": "eth:ip:tcp",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.443",
            "tcp.srcport": "51516",
            "tcp.dstport": "443",
            "tcp.stream": "3",
            "frame.len": "96",
            "x509sat.printableString": "CN=example.test",
        },
        {
            "frame.number": "3",
            "frame.time_epoch": "3.0",
            "frame.protocols": "",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.53",
            "udp.srcport": "51517",
            "udp.dstport": "53",
            "frame.len": "74",
            "dns.flags.rcode": "3",
        },
        {
            "frame.number": "4",
            "frame.time_epoch": "4.0",
            "frame.protocols": "",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.25",
            "tcp.srcport": "51518",
            "tcp.dstport": "25",
            "tcp.stream": "4",
            "frame.len": "88",
            "smtp.req.command": "AUTH",
        },
        {
            "frame.number": "5",
            "frame.time_epoch": "5.0",
            "frame.protocols": "",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.110",
            "tcp.srcport": "51519",
            "tcp.dstport": "110",
            "tcp.stream": "5",
            "frame.len": "88",
            "pop.request.command": "USER",
        },
        {
            "frame.number": "6",
            "frame.time_epoch": "6.0",
            "frame.protocols": "",
            "ip.src": "192.0.2.10",
            "ip.dst": "198.51.100.143",
            "tcp.srcport": "51520",
            "tcp.dstport": "143",
            "tcp.stream": "6",
            "frame.len": "88",
            "imap.request.command": "LOGIN",
        },
    ]

    legacy = survey.parse_profile_field_summary(rows)
    refactored = survey.parse_profile_field_summary(rows)

    assert_same_output(legacy["protocols"], refactored["protocols"])
    assert_same_output(legacy["services"], refactored["services"])
    assert_same_output(legacy["interesting_streams"], refactored["interesting_streams"])
    assert_same_output(
        legacy["recommended_next_queries"],
        refactored["recommended_next_queries"],
    )


def test_refactored_survey_anomaly_signals_match_legacy() -> None:
    rows = [
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": "USER"},
        {"frame.protocols": "eth:ip:tcp:ftp", "ftp.request.command": "SYST"},
        {"frame.protocols": "eth:ip:tcp:http", "http.response.code": "500"},
        {"frame.protocols": "eth:ip:udp:dns", "dns.flags.rcode": "3"},
        {"frame.protocols": "eth:ip:tcp:tls", "tls.alert_message.desc": "Fatal"},
        {"frame.protocols": "eth:ip:tcp", "tcp.analysis.retransmission": "1"},
        {"frame.protocols": "eth:ip:icmp", "icmp.type": "3"},
        {"frame.protocols": "eth:ip:tcp:smtp", "smtp.req.command": "AUTH"},
        {"frame.protocols": "eth:ip:tcp:pop", "pop.request.command": "PASS"},
        {"frame.protocols": "eth:ip:tcp:imap", "imap.request.command": "LOGIN"},
        {"frame.protocols": "eth:ip:tcp:http", "http.response.code": "200"},
    ]

    for fields in rows:
        assert_same_output(
            survey.survey_row_signals(fields),
            survey.survey_row_signals(fields),
        )


def test_refactored_survey_protocol_and_port_hints_match_legacy() -> None:
    values = [
        {"protocols": ["eth", "ip", "tcp", "ftp"], "dst_port": "21"},
        {"protocols": ["eth", "ip", "udp", "dns"], "dst_port": "53"},
        {"protocols": ["eth", "ip", "tcp", "http"], "dst_port": "80"},
        {"protocols": ["eth", "ip", "tcp", "ssl"], "dst_port": "443"},
        {"protocols": ["eth", "ip", "tcp", "smtp"], "dst_port": "25"},
        {"protocols": ["eth", "ip", "tcp", "pop"], "dst_port": "110"},
        {"protocols": ["eth", "ip", "tcp", "imap"], "dst_port": "143"},
        {"protocols": {"tcp", "smtp"}, "dst_port": "587"},
        {"protocols": [], "dst_port": "465"},
        {"protocols": [], "dst_port": "993"},
        {"protocols": [], "dst_port": "995"},
        {"protocols": [], "dst_port": "8443"},
    ]

    for value in values:
        assert_same_output(
            survey.survey_protocol_hint(value),
            survey.survey_protocol_hint(value),
        )
        assert_same_output(
            survey.well_known_protocol_for_port(value["dst_port"]),
            survey.well_known_protocol_for_port(value["dst_port"]),
        )


def test_refactored_survey_sort_order_and_dedupe_match_legacy() -> None:
    conversations = [
        {
            "src": "192.0.2.10",
            "dst": "203.0.113.80",
            "protocol": "tcp",
            "src_port": "51515",
            "dst_port": "80",
            "packet_count": 2,
            "bytes": 200,
        },
        {
            "src": "192.0.2.11",
            "dst": "203.0.113.80",
            "protocol": "tcp",
            "src_port": "51516",
            "dst_port": "80",
            "packet_count": 3,
            "bytes": 300,
        },
        {
            "src": "192.0.2.12",
            "dst": "203.0.113.21",
            "protocol": "ftp",
            "src_port": "51517",
            "dst_port": "21",
            "packet_count": 1,
            "bytes": 100,
        },
    ]
    streams = [
        {
            "stream": "10",
            "src": "192.0.2.10",
            "dst": "203.0.113.80",
            "src_port": "51515",
            "dst_port": "80",
            "protocols": {"tcp", "http"},
            "frames": ["7"],
            "packet_count": 1,
            "signals": {"http_error_status"},
        },
        {
            "stream": "2",
            "src": "192.0.2.12",
            "dst": "203.0.113.21",
            "src_port": "51517",
            "dst_port": "21",
            "protocols": {"tcp", "ftp"},
            "frames": ["1", "2", "3", "4", "5", "6"],
            "packet_count": 6,
            "signals": {"cleartext_ftp_auth_command"},
        },
        {
            "stream": "2",
            "src": "192.0.2.12",
            "dst": "203.0.113.21",
            "src_port": "51517",
            "dst_port": "21",
            "protocols": {"tcp", "ftp"},
            "frames": ["8"],
            "packet_count": 1,
            "signals": {"cleartext_ftp_auth_command"},
        },
        {
            "stream": "alpha",
            "src": "192.0.2.13",
            "dst": "203.0.113.53",
            "src_port": "51518",
            "dst_port": "53",
            "protocols": {"udp", "dns"},
            "frames": ["9"],
            "packet_count": 1,
            "signals": set(),
        },
    ]

    legacy_services = survey.survey_services(conversations, streams)
    refactored_services = survey.survey_services(conversations, streams)
    assert_same_output(legacy_services, refactored_services)

    legacy_streams = survey.survey_interesting_streams(streams)
    refactored_streams = survey.survey_interesting_streams(streams)
    assert_same_output(legacy_streams, refactored_streams)

    legacy_queries = survey.survey_recommended_next_queries(
        services=legacy_services,
        interesting_streams=legacy_streams,
    )
    refactored_queries = survey.survey_recommended_next_queries(
        services=refactored_services,
        interesting_streams=refactored_streams,
    )
    assert_same_output(legacy_queries, refactored_queries)
    assert_same_output(len(legacy_queries), len({json.dumps(item, sort_keys=True) for item in legacy_queries}))


@pytest.mark.parametrize(
    "proof_mode",
    ["metadata_only", "proof_excerpt", "fingerprint"],
)
def test_canonical_security_field_rows_cover_mail_filtered_signals(
    monkeypatch: pytest.MonkeyPatch,
    proof_mode: str,
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )
    rows = [
        {
            "frame.number": "70",
            "frame.time_epoch": "500.0",
            "frame.protocols": "eth:ip:tcp:smtp",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.25",
            "tcp.srcport": "51530",
            "tcp.dstport": "25",
            "tcp.stream": "20",
            "smtp.req.command": "AUTH",
            "smtp.req.parameter": "PLAIN smtp-secret",
        },
        {
            "frame.number": "71",
            "frame.time_epoch": "501.0",
            "frame.protocols": "eth:ip:tcp:smtp",
            "ip.src": "203.0.113.25",
            "ip.dst": "192.0.2.10",
            "tcp.srcport": "25",
            "tcp.dstport": "51530",
            "tcp.stream": "20",
            "smtp.response.code": "235",
        },
        {
            "frame.number": "72",
            "frame.time_epoch": "502.0",
            "frame.protocols": "eth:ip:tcp:pop",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.110",
            "tcp.srcport": "51531",
            "tcp.dstport": "110",
            "tcp.stream": "21",
            "pop.request.command": "PASS",
            "pop.request.parameter": "pop-secret",
        },
        {
            "frame.number": "73",
            "frame.time_epoch": "503.0",
            "frame.protocols": "eth:ip:tcp:imap",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.143",
            "tcp.srcport": "51532",
            "tcp.dstport": "143",
            "tcp.stream": "22",
            "imap.request.command": "LOGIN",
            "imap.request": "user imap-secret",
        },
        {
            "frame.number": "74",
            "frame.time_epoch": "504.0",
            "frame.protocols": "eth:ip:tcp:smtp",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.25",
            "tcp.srcport": "51533",
            "tcp.dstport": "25",
            "tcp.stream": "23",
            "smtp.auth.password": "smtp-direct-secret",
        },
        {
            "frame.number": "75",
            "frame.time_epoch": "505.0",
            "frame.protocols": "eth:ip:tcp:http",
            "ip.src": "192.0.2.10",
            "ip.dst": "203.0.113.80",
            "tcp.srcport": "51534",
            "tcp.dstport": "80",
            "tcp.stream": "24",
            "http.authorization": "Bearer http-secret",
        },
    ]

    canonical_mail_security = _mail_security_subset(
        security.parse_security_field_rows(
            rows,
            artifact_sha256="sha",
            sensitive_proof_mode=proof_mode,
        )
    )

    assert {item["protocol"] for item in canonical_mail_security["credential_events"]} == {
        "imap",
        "pop",
        "smtp",
    }
    assert {key: len(value) for key, value in canonical_mail_security.items()} == {
        "credential_events": 6,
        "auth_indicators": 7,
        "secret_exposure": 4,
        "auth_sequences": 4,
    }
    assert all(
        item.get("protocol") in mail.MAIL_PROTOCOLS
        for rows_for_key in canonical_mail_security.values()
        for item in rows_for_key
    )
    assert all(
        item.get("proof_mode") == proof_mode
        for item in (
            canonical_mail_security["credential_events"]
            + canonical_mail_security["secret_exposure"]
            + canonical_mail_security["auth_sequences"]
        )
    )
    if proof_mode == "metadata_only":
        assert all("proof_excerpt" not in item for item in canonical_mail_security["credential_events"])
        assert all("fingerprint" not in item for item in canonical_mail_security["credential_events"])
    elif proof_mode == "proof_excerpt":
        assert all("proof_excerpt" in item for item in canonical_mail_security["credential_events"])
        assert all("fingerprint" not in item for item in canonical_mail_security["credential_events"])
    else:
        assert all("fingerprint" in item for item in canonical_mail_security["credential_events"])
        assert all("proof_excerpt" not in item for item in canonical_mail_security["credential_events"])


def test_refactored_mail_helpers_do_not_add_mail_top_level_sections() -> None:
    metadata = _facade_parse(
        FIELD_PROFILE_STDOUT,
        "",
        analysis_mode="find_security_relevant_artifacts",
        fields=FIELD_PROFILE_FIELDS,
        artifact_sha256="sha",
        max_rows=20,
    )

    assert "smtp" not in metadata
    assert "pop" not in metadata
    assert "imap" not in metadata


def _mail_security_subset(
    security: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        "credential_events": [
            dict(item)
            for item in security["credential_events"]
            if item.get("protocol") in mail.MAIL_PROTOCOLS
        ],
        "auth_indicators": [
            dict(item)
            for item in security["auth_indicators"]
            if item.get("protocol") in mail.MAIL_PROTOCOLS
        ],
        "secret_exposure": [
            dict(item)
            for item in security["secret_exposure"]
            if item.get("protocol") in mail.MAIL_PROTOCOLS
        ],
        "auth_sequences": [
            dict(item)
            for item in security["auth_sequences"]
            if item.get("protocol") in mail.MAIL_PROTOCOLS
        ],
    }


def test_facade_snapshot_empty_and_malformed_diagnostics() -> None:
    empty = _facade_parse(
        "",
        "",
        analysis_mode="http",
        input_file="captures/example.pcap",
        max_rows=5,
    )

    assert empty == {
        "schema_version": "tshark.v1",
        "analysis_mode": "http",
        "pcap": {
            "input_file": "captures/example.pcap",
            "artifact_sha256": None,
            "packet_count": 0,
            "duration_seconds": None,
        },
        "protocols": [],
        "hosts": [],
        "conversations": [],
        "services": [],
        "interesting_streams": [],
        "recommended_next_queries": [],
        "dns": [],
        "http": [],
        "tls": [],
        "ftp": [],
        "auth_indicators": [],
        "secret_exposure": [],
        "credential_events": [],
        "auth_sequences": [],
        "field_extract": [],
        "limits": {"max_rows": 5, "truncated": False, "lists": {}},
        "warnings": [],
        "errors": [],
    }

    assert _snapshot(
        _facade_parse,
        '[{"_source": ',
        "Warning: decoder warning\nError: decoder error",
        keys=("pcap", "protocols", "hosts", "conversations", "warnings", "errors"),
        max_rows=5,
    ) == {
        "pcap": {
            "input_file": None,
            "artifact_sha256": None,
            "packet_count": 0,
            "duration_seconds": None,
        },
        "protocols": [],
        "hosts": [],
        "conversations": [],
        "warnings": ["Warning: decoder warning"],
        "errors": ["Error: decoder error"],
    }


def test_facade_snapshot_json_protocol_rows_and_security_shapes() -> None:
    summary = _facade_parse(
        JSON_PACKET_STDOUT,
        "",
        analysis_mode="pcap_summary",
        artifact_sha256="sha",
        max_rows=20,
    )
    dns = _facade_parse(JSON_PACKET_STDOUT, "", analysis_mode="dns", max_rows=20)
    http = _facade_parse(JSON_PACKET_STDOUT, "", analysis_mode="http", max_rows=20)
    tls = _facade_parse(JSON_PACKET_STDOUT, "", analysis_mode="tls", max_rows=20)

    assert summary["pcap"] == {
        "input_file": None,
        "artifact_sha256": "sha",
        "packet_count": 3,
        "duration_seconds": 2.0,
    }
    assert summary["protocols"] == ["dns", "eth", "http", "ip", "tcp", "tls", "udp"]
    assert summary["hosts"] == [
        "192.0.2.10",
        "198.51.100.53",
        "203.0.113.20",
        "203.0.113.443",
    ]
    assert summary["conversations"] == [
        {
            "flow_key": "udp:192.0.2.10:53123->198.51.100.53:53",
            "src": "192.0.2.10",
            "dst": "198.51.100.53",
            "protocol": "udp",
            "src_port": "53123",
            "dst_port": "53",
            "packet_count": 1,
            "bytes": 86,
        },
        {
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "protocol": "tcp",
            "src_port": "49152",
            "dst_port": "80",
            "packet_count": 1,
            "bytes": 512,
        },
        {
            "flow_key": "tcp:192.0.2.10:49153->203.0.113.443:443",
            "src": "192.0.2.10",
            "dst": "203.0.113.443",
            "protocol": "tcp",
            "src_port": "49153",
            "dst_port": "443",
            "packet_count": 1,
            "bytes": 256,
        },
    ]
    assert dns["dns"] == [
        {
            "frame": "1",
            "time": "t1",
            "query": "www.example.test",
            "qtype": "1",
            "answers": ["198.51.100.10"],
            "rcode": "0",
            "src": "192.0.2.10",
            "dst": "198.51.100.53",
        }
    ]
    assert http["http"] == [
        {
            "frame": "2",
            "time": "t2",
            "stream": "7",
            "host": "app.example.test",
            "method": "GET",
            "path": "/login?next=/admin",
            "status": "200",
            "user_agent": "SyntheticAgent/1.0",
            "server": None,
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "headers": {
                "http.authorization": ["Bearer synthetic-http-token"],
                "http.cookie": ["session=synthetic-cookie-secret"],
            },
        }
    ]
    assert tls["tls"] == [
        {
            "frame": "3",
            "time": "t3",
            "stream": "8",
            "sni": "secure.example.test",
            "alpn": ["h2"],
            "subject": "CN=secure.example.test",
            "issuer": "CN=Issuer Test CA",
            "versions": ["0x0303"],
            "src": "192.0.2.10",
            "dst": "203.0.113.443",
        }
    ]
    assert summary["credential_events"] == [
        {
            "frame": "2",
            "time": "t2",
            "stream": "7",
            "protocol": "http",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "field": "http.authorization",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "pcap_artifact_sha256": "sha",
            "extraction_filter": "http.authorization",
            "kind": "authorization_header",
            "role": "secret",
            "proof_mode": "proof_excerpt",
            "proof_excerpt": "Bearer synthetic-http-token",
        },
        {
            "frame": "2",
            "time": "t2",
            "stream": "7",
            "protocol": "http",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "field": "http.cookie",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "pcap_artifact_sha256": "sha",
            "extraction_filter": "http.cookie",
            "kind": "cookie",
            "role": "secret",
            "proof_mode": "proof_excerpt",
            "proof_excerpt": "session=synthetic-cookie-secret",
        },
    ]


@pytest.mark.parametrize(
    ("proof_mode", "expected"),
    [
        (
            "metadata_only",
            [
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.authorization",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.authorization",
                    "kind": "authorization_header",
                    "proof_mode": "metadata_only",
                },
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.cookie",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.cookie",
                    "kind": "cookie",
                    "proof_mode": "metadata_only",
                },
            ],
        ),
        (
            "proof_excerpt",
            [
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.authorization",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.authorization",
                    "kind": "authorization_header",
                    "proof_mode": "proof_excerpt",
                    "proof_excerpt": "Bearer synthetic-http-token",
                },
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.cookie",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.cookie",
                    "kind": "cookie",
                    "proof_mode": "proof_excerpt",
                    "proof_excerpt": "session=synthetic-cookie-secret",
                },
            ],
        ),
        (
            "fingerprint",
            [
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.authorization",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.authorization",
                    "kind": "authorization_header",
                    "proof_mode": "fingerprint",
                    "fingerprint": (
                        "hmac-sha256:authorization_header:"
                        "a31005c1458294b6287a5c0e5dc2f4d2"
                    ),
                },
                {
                    "frame": "2",
                    "time": "t2",
                    "stream": "7",
                    "protocol": "http",
                    "src": "192.0.2.10",
                    "dst": "203.0.113.20",
                    "field": "http.cookie",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "pcap_artifact_sha256": "sha",
                    "extraction_filter": "http.cookie",
                    "kind": "cookie",
                    "proof_mode": "fingerprint",
                    "fingerprint": (
                        "hmac-sha256:cookie:cd8b51587473359828429133d978fedc"
                    ),
                },
            ],
        ),
    ],
)
def test_facade_snapshot_secret_exposure_proof_modes(
    monkeypatch: pytest.MonkeyPatch,
    proof_mode: str,
    expected: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv(
        tshark_semantics.SECRET_FINGERPRINT_KEY_ENV,
        "unit-test-hmac-key",
    )

    metadata = _facade_parse(
        JSON_PACKET_STDOUT,
        "",
        analysis_mode="secret_exposure",
        artifact_sha256="sha",
        sensitive_proof_mode=proof_mode,
        max_rows=20,
    )

    assert metadata["secret_exposure"] == expected


def test_facade_snapshot_field_survey_and_protocol_rows() -> None:
    survey = _facade_parse(
        FIELD_PROFILE_STDOUT,
        "",
        analysis_mode="survey",
        fields=FIELD_PROFILE_FIELDS,
        artifact_sha256="sha",
        max_rows=20,
    )
    extract = _facade_parse(
        FIELD_PROFILE_STDOUT,
        "",
        analysis_mode="extract_evidence",
        fields=FIELD_PROFILE_FIELDS,
        artifact_sha256="sha",
        max_rows=20,
    )

    assert survey["pcap"] == {
        "input_file": None,
        "artifact_sha256": "sha",
        "packet_count": 9,
        "duration_seconds": 8.0,
        "shape": {
            "packet_count": 9,
            "time_start": 100.0,
            "time_end": 108.0,
            "duration_seconds": 8.0,
        },
    }
    assert survey["services"] == [
        {
            "host": "198.51.100.53",
            "port": 53,
            "transport": "dns",
            "protocol_hint": "dns",
            "packet_count": 1,
            "bytes": 86,
            "streams": [],
        },
        {
            "host": "203.0.113.110",
            "port": 110,
            "transport": "pop",
            "protocol_hint": "pop",
            "packet_count": 1,
            "bytes": 90,
            "streams": ["11"],
        },
        {
            "host": "203.0.113.143",
            "port": 143,
            "transport": "imap",
            "protocol_hint": "imap",
            "packet_count": 1,
            "bytes": 90,
            "streams": ["12"],
        },
        {
            "host": "203.0.113.20",
            "port": 80,
            "transport": "http",
            "protocol_hint": "http",
            "packet_count": 1,
            "bytes": 512,
            "streams": ["7"],
        },
        {
            "host": "203.0.113.21",
            "port": 21,
            "transport": "ftp",
            "protocol_hint": "ftp",
            "packet_count": 2,
            "bytes": 160,
            "streams": ["9"],
        },
        {
            "host": "203.0.113.25",
            "port": 25,
            "transport": "smtp",
            "protocol_hint": "smtp",
            "packet_count": 1,
            "bytes": 90,
            "streams": ["10"],
        },
        {
            "host": "203.0.113.30",
            "port": 443,
            "transport": "tls",
            "protocol_hint": "tls",
            "packet_count": 1,
            "bytes": 256,
            "streams": ["8"],
        },
    ]
    assert survey["interesting_streams"][0] == {
        "stream": "9",
        "protocol_hint": "ftp",
        "src": "192.0.2.10",
        "dst": "203.0.113.21",
        "src_port": "49154",
        "dst_port": "21",
        "packet_count": 3,
        "frames": ["4", "5", "6"],
        "signals": ["cleartext_ftp_auth_command"],
        "reason": "FTP authentication command observed.",
        "recommended_intent": "find_security_relevant_artifacts",
    }
    assert extract["dns"] == [
        {
            "frame": "1",
            "time": "100.0",
            "query": "www.example.test",
            "qtype": "1",
            "answers": ["198.51.100.10"],
            "rcode": "0",
            "src": "192.0.2.10",
            "dst": "198.51.100.53",
        }
    ]
    assert extract["http"] == [
        {
            "frame": "2",
            "time": "101.0",
            "stream": "7",
            "host": "app.example.test",
            "method": "POST",
            "path": "/login",
            "status": "302",
            "user_agent": None,
            "content_type": None,
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "headers": {
                "http.authorization": "Basic abc123",
                "http.cookie": "session=clear",
            },
        }
    ]
    assert extract["tls"] == [
        {
            "frame": "3",
            "time": "102.0",
            "stream": "8",
            "sni": "secure.example.test",
            "subject": "CN=secure.example.test",
            "issuer": None,
            "versions": ["0x0303"],
            "src": "192.0.2.10",
            "dst": "203.0.113.30",
        }
    ]
    assert extract["ftp"] == [
        {
            "frame": "4",
            "time": "103.0",
            "stream": "9",
            "src": "192.0.2.10",
            "dst": "203.0.113.21",
            "src_port": "49154",
            "dst_port": "21",
            "request_command": "USER",
            "request_arg": "nathan",
            "response_code": None,
            "response_arg": None,
            "tcp_len": None,
            "frame_len": "74",
        },
        {
            "frame": "5",
            "time": "104.0",
            "stream": "9",
            "src": "192.0.2.10",
            "dst": "203.0.113.21",
            "src_port": "49154",
            "dst_port": "21",
            "request_command": "PASS",
            "request_arg": "Buck3tH4TF0RM3!",
            "response_code": None,
            "response_arg": None,
            "tcp_len": None,
            "frame_len": "86",
        },
        {
            "frame": "6",
            "time": "105.0",
            "stream": "9",
            "src": "203.0.113.21",
            "dst": "192.0.2.10",
            "src_port": "21",
            "dst_port": "49154",
            "request_command": None,
            "request_arg": None,
            "response_code": "230",
            "response_arg": "Login successful.",
            "tcp_len": None,
            "frame_len": "70",
        },
    ]


def test_facade_snapshot_mail_field_rows_emit_auth_security_signals() -> None:
    metadata = _facade_parse(
        FIELD_PROFILE_STDOUT,
        "",
        analysis_mode="find_security_relevant_artifacts",
        fields=FIELD_PROFILE_FIELDS,
        artifact_sha256="sha",
        max_rows=20,
    )

    assert [
        {
            "protocol": item["protocol"],
            "field": item["field"],
            "kind": item["kind"],
            "role": item["role"],
            "proof_excerpt": item["proof_excerpt"],
        }
        for item in metadata["credential_events"]
        if item["protocol"] in {"smtp", "pop", "imap"}
    ] == [
        {
            "protocol": "smtp",
            "field": "smtp.req.parameter",
            "kind": "username",
            "role": "username",
            "proof_excerpt": "PLAIN smtp-secret",
        },
        {
            "protocol": "smtp",
            "field": "smtp.req.parameter",
            "kind": "protocol_auth_argument",
            "role": "secret",
            "proof_excerpt": "PLAIN smtp-secret",
        },
        {
            "protocol": "pop",
            "field": "pop.request.parameter",
            "kind": "password",
            "role": "password",
            "proof_excerpt": "pop-secret",
        },
        {
            "protocol": "imap",
            "field": "imap.request",
            "kind": "username",
            "role": "username",
            "proof_excerpt": "user imap-secret",
        },
    ]
    assert [
        {
            "protocol": item["protocol"],
            "field": item["field"],
            "mechanism": item["mechanism"],
            "value": item["value"],
        }
        for item in metadata["auth_indicators"]
        if item["protocol"] in {"smtp", "pop", "imap"}
    ] == [
        {
            "protocol": "smtp",
            "field": "smtp.req.parameter",
            "mechanism": "username",
            "value": "PLAIN smtp-secret",
        },
        {
            "protocol": "smtp",
            "field": "smtp.req.parameter",
            "mechanism": "protocol_auth_argument",
            "value": "PLAIN smtp-secret",
        },
        {
            "protocol": "pop",
            "field": "pop.request.parameter",
            "mechanism": "password",
            "value": "pop-secret",
        },
        {
            "protocol": "imap",
            "field": "imap.request",
            "mechanism": "username",
            "value": "user imap-secret",
        },
    ]


def test_facade_snapshot_truncates_json_rows_before_metadata_parsing() -> None:
    metadata = _facade_parse(
        JSON_PACKET_STDOUT,
        "",
        analysis_mode="pcap_summary",
        max_rows=2,
    )

    assert metadata["pcap"]["packet_count"] == 2
    assert metadata["hosts"] == ["192.0.2.10", "198.51.100.53"]
    assert metadata["limits"]["input_rows_truncated"] is True
    assert metadata["limits"]["truncated"] is True
    assert metadata["warnings"] == [
        "TShark JSON packet rows were capped at max_rows=2 before parsing."
    ]


def test_facade_snapshot_semantic_observations_and_evidence_are_masked() -> None:
    metadata = _facade_parse(
        JSON_PACKET_STDOUT,
        "",
        analysis_mode="secret_exposure",
        artifact_sha256="sha",
        max_rows=20,
    )

    observations = tshark_semantics.build_tshark_semantic_observations(metadata, args=None)
    evidence = tshark_semantics.build_tshark_semantic_evidence(metadata, args=None)
    findings = [
        item for item in observations if item["observation_type"] == "finding.vulnerability_detected"
    ]

    assert [item["subject_key"] for item in observations] == [
        "host.ip:192.0.2.10",
        "host.ip:198.51.100.53",
        "host.ip:203.0.113.20",
        "host.ip:203.0.113.443",
        "service.socket:198.51.100.53/udp/53",
        "service.socket:203.0.113.20/tcp/80",
        (
            "finding.vulnerability:service.socket:203.0.113.20/tcp/80:"
            "tshark/credential_exposure_detected/http.authorization"
        ),
        "service.socket:203.0.113.20/tcp/80",
        (
            "finding.vulnerability:service.socket:203.0.113.20/tcp/80:"
            "tshark/credential_exposure_detected/http.cookie"
        ),
    ]
    assert [item["payload"]["proof_excerpt"] for item in findings] == [
        "Bearer <DURABLE_SECRET_MASK:token>",
        "<DURABLE_SECRET_MASK:secret>",
    ]
    assert evidence == [
        {
            "type": "variant",
            "name": "analysis_mode",
            "value": "secret_exposure",
            "source": "tshark",
        },
        {
            "type": "execution_parameter",
            "name": "input_file_mode",
            "value": "live_capture",
            "source": "tshark",
        },
        {
            "type": "execution_parameter",
            "name": "max_rows",
            "value": 20,
            "source": "tshark",
            "detail": {"unit": "rows"},
        },
        {
            "type": "result_summary",
            "name": "packet_count",
            "value": 3,
            "detail": {"unit": "packets"},
            "source": "tshark",
        },
        {
            "type": "result_summary",
            "name": "conversation_count",
            "value": 3,
            "detail": {"unit": "conversations"},
            "source": "tshark",
        },
        {
            "type": "result_summary",
            "name": "secret_exposure_count",
            "value": 2,
            "detail": {"unit": "exposures"},
            "source": "tshark",
        },
        {
            "type": "diagnostic",
            "name": "packet_proof",
            "value": (
                "protocol=http frame=2 stream=7 field=http.authorization "
                "proof_mode=proof_excerpt proof=Bearer <DURABLE_SECRET_MASK:token>"
            ),
            "detail": {"severity": "info", "note": "secret_exposure"},
            "source": "tshark",
        },
    ]
    serialized = str([observations, evidence])
    assert "synthetic-http-token" not in serialized
    assert "synthetic-cookie-secret" not in serialized
