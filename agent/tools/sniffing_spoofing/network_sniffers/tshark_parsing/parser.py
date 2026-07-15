"""TShark output parser orchestration.

This module composes JSON, field-output, protocol, survey, and security parsers
into the metadata contract exposed by tshark_semantics.parse_tshark_output.
Command construction, execution, and workspace resolution remain outside this
parser package.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    DEFAULT_MAX_ROWS,
    DEFAULT_SENSITIVE_PROOF_MODE,
    TSHARK_SCHEMA_VERSION,
    _bounded_list,
    _normalize_field_names,
    _normalize_row_limit,
    _normalize_sensitive_proof_mode,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.field_output import (
    parse_field_extract,
    parse_profile_field_output,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.json_packets import (
    load_json_packets,
    parse_conversation_rows,
    parse_json_packet_summary,
    parse_text_packets,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols.dns import (
    parse_dns_rows,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols.http import (
    parse_http_rows,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols.tls import (
    parse_tls_rows,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.security import (
    parse_auth_indicator_rows,
    parse_critical_signal_rows,
    parse_secret_exposure_rows,
)


def parse_tshark_output(
    stdout: str,
    stderr: str,
    *,
    analysis_mode: str = "pcap_summary",
    input_file: str | None = None,
    artifact_sha256: str | None = None,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    fields: Iterable[str] | None = None,
    sensitive_proof_mode: str = DEFAULT_SENSITIVE_PROOF_MODE,
) -> Dict[str, Any]:
    """Parse TShark output into legacy-compatible structured metadata."""

    row_limit = _normalize_row_limit(max_rows)
    field_names = _normalize_field_names(fields)
    metadata = _empty_metadata(
        analysis_mode=analysis_mode,
        input_file=input_file,
        artifact_sha256=artifact_sha256,
        max_rows=row_limit,
    )

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    try:
        stdout_text = stdout or ""
        stderr_text = stderr or ""
        packet_rows, packet_rows_truncated = load_json_packets(stdout_text, max_rows=row_limit)
        if packet_rows_truncated:
            metadata["limits"]["input_rows_truncated"] = True
            metadata["limits"]["truncated"] = True
        mode = str(analysis_mode or "pcap_summary")
        proof_mode = _normalize_sensitive_proof_mode(sensitive_proof_mode)
        mode_warnings: list[str] = []

        if mode == "field_extract":
            parsed = parse_field_extract(stdout_text, field_names)
            metadata["pcap"]["packet_count"] = parsed["packet_count"]
            metadata["field_extract"] = _bounded_list(
                "field_extract",
                parsed["field_extract"],
                row_limit,
                metadata["limits"],
            )
            mode_warnings.extend(parsed["warnings"])
        elif field_names and packet_rows is None:
            parsed = parse_profile_field_output(
                stdout_text,
                field_names,
                mode=mode,
                artifact_sha256=artifact_sha256,
                sensitive_proof_mode=proof_mode,
            )
            metadata["pcap"]["packet_count"] = parsed["packet_count"]
            metadata["protocols"] = _bounded_list(
                "protocols",
                parsed["protocols"],
                row_limit,
                metadata["limits"],
            )
            metadata["hosts"] = _bounded_list(
                "hosts",
                parsed["hosts"],
                row_limit,
                metadata["limits"],
            )
            metadata["conversations"] = _bounded_list(
                "conversations",
                parsed["conversations"],
                row_limit,
                metadata["limits"],
            )
            for key in ("services", "interesting_streams", "recommended_next_queries"):
                if key in parsed:
                    metadata[key] = _bounded_list(
                        key,
                        parsed[key],
                        row_limit,
                        metadata["limits"],
                    )
            if parsed.get("pcap_shape"):
                metadata["pcap"]["shape"] = parsed["pcap_shape"]
                metadata["pcap"]["duration_seconds"] = parsed["pcap_shape"].get(
                    "duration_seconds"
                )
            for key in (
                "dns",
                "http",
                "tls",
                "ftp",
                "auth_indicators",
                "secret_exposure",
                "credential_events",
                "auth_sequences",
                "field_extract",
            ):
                if key in parsed:
                    metadata[key] = _bounded_list(
                        key,
                        parsed[key],
                        row_limit,
                        metadata["limits"],
                    )
            mode_warnings.extend(parsed["warnings"])
        elif packet_rows is not None:
            parsed = parse_json_packet_summary(packet_rows)
            metadata["pcap"]["packet_count"] = parsed["packet_count"]
            metadata["pcap"]["duration_seconds"] = parsed["duration_seconds"]
            metadata["protocols"] = _bounded_list(
                "protocols",
                parsed["protocols"],
                row_limit,
                metadata["limits"],
            )
            metadata["hosts"] = _bounded_list(
                "hosts",
                parsed["hosts"],
                row_limit,
                metadata["limits"],
            )
            metadata["conversations"] = _bounded_list(
                "conversations",
                parsed["conversations"],
                row_limit,
                metadata["limits"],
            )
            critical = parse_critical_signal_rows(
                packet_rows,
                artifact_sha256=artifact_sha256,
                sensitive_proof_mode=proof_mode,
            )
            metadata["credential_events"] = _bounded_list(
                "credential_events",
                critical["credential_events"],
                row_limit,
                metadata["limits"],
            )
            metadata["auth_sequences"] = _bounded_list(
                "auth_sequences",
                critical["auth_sequences"],
                row_limit,
                metadata["limits"],
            )
            mode_parsed = _parse_mode_rows(
                mode,
                packet_rows,
                artifact_sha256=artifact_sha256,
                sensitive_proof_mode=proof_mode,
                critical=critical,
            )
            for key in ("dns", "http", "tls", "auth_indicators", "secret_exposure"):
                if key in mode_parsed:
                    metadata[key] = _bounded_list(
                        key,
                        mode_parsed[key],
                        row_limit,
                        metadata["limits"],
                    )
            mode_warnings.extend(mode_parsed["warnings"])
            if packet_rows_truncated:
                mode_warnings.append(
                    f"TShark JSON packet rows were capped at max_rows={row_limit} before parsing."
                )
        else:
            parsed = parse_text_packets(stdout_text)
            metadata["pcap"]["packet_count"] = parsed["packet_count"]
            for key in ("protocols", "hosts", "conversations"):
                metadata[key] = _bounded_list(key, parsed[key], row_limit, metadata["limits"])
            if mode not in {"pcap_summary", "conversations"} and stdout_text.strip():
                mode_warnings.append(
                    f"{mode} parser expected TShark JSON output; returned summary-only metadata."
                )

        diagnostics = stderr_text.splitlines()
        if stdout_text and packet_rows is None:
            diagnostics = [*diagnostics, *stdout_text.splitlines()]
        warnings: list[str] = []
        errors: list[str] = []
        for line in diagnostics:
            lowered = line.lower()
            if "error" in lowered:
                errors.append(line.strip())
            elif "warning" in lowered:
                warnings.append(line.strip())
        metadata["warnings"] = _bounded_list(
            "warnings",
            [*mode_warnings, *warnings],
            row_limit,
            metadata["limits"],
        )
        metadata["errors"] = _bounded_list("errors", errors, row_limit, metadata["limits"])
    except Exception as exc:
        metadata["errors"] = _bounded_list(
            "errors",
            [f"Failed to parse output: {exc}"],
            row_limit,
            metadata["limits"],
        )

    return metadata


def _empty_metadata(
    *,
    analysis_mode: str,
    input_file: str | None,
    artifact_sha256: str | None,
    max_rows: int,
) -> Dict[str, Any]:
    """Build the deterministic TShark metadata envelope."""

    return {
        "schema_version": TSHARK_SCHEMA_VERSION,
        "analysis_mode": str(analysis_mode or "pcap_summary"),
        "pcap": {
            "input_file": input_file,
            "artifact_sha256": artifact_sha256,
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
        "limits": {
            "max_rows": max_rows,
            "truncated": False,
            "lists": {},
        },
        "warnings": [],
        "errors": [],
    }


def _parse_mode_rows(
    mode: str,
    rows: list[Mapping[str, Any]],
    *,
    artifact_sha256: str | None = None,
    sensitive_proof_mode: str = DEFAULT_SENSITIVE_PROOF_MODE,
    critical: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    if mode == "survey":
        mode = "pcap_summary"
    elif mode == "find_security_relevant_artifacts":
        mode = "secret_exposure"
    elif mode == "extract_evidence":
        return {
            "field_extract": [],
            "warnings": ["extract_evidence parser received JSON output; expected bounded field output."],
        }
    elif mode == "anomaly_detection":
        return {"warnings": []}
    elif mode == "investigate_protocol":
        dns = parse_dns_rows(rows)
        http = parse_http_rows(rows)
        tls = parse_tls_rows(rows)
        warnings = [
            *dns.get("warnings", []),
            *http.get("warnings", []),
            *tls.get("warnings", []),
        ]
        return {
            "dns": dns.get("dns", []),
            "http": http.get("http", []),
            "tls": tls.get("tls", []),
            "warnings": warnings,
        }
    parser = {
        "pcap_summary": _parse_pcap_summary_rows,
        "conversations": parse_conversation_rows,
        "dns": parse_dns_rows,
        "http": parse_http_rows,
        "tls": parse_tls_rows,
        "auth_indicators": parse_auth_indicator_rows,
        "secret_exposure": parse_secret_exposure_rows,
    }.get(mode)
    if parser is None:
        return {"warnings": [f"Unknown TShark analysis mode: {mode}"]}
    if mode == "auth_indicators":
        return parse_auth_indicator_rows(rows, critical=critical)
    if mode == "secret_exposure":
        return parse_secret_exposure_rows(
            rows,
            artifact_sha256=artifact_sha256,
            sensitive_proof_mode=sensitive_proof_mode,
            critical=critical,
        )
    return parser(rows)


def _parse_pcap_summary_rows(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    return {"warnings": []}


__all__ = ("parse_tshark_output",)
