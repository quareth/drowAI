"""Legacy-compatible TShark field-output parsing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing import (
    security,
    survey,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_rows_to_packet_rows,
    normalize_tshark_field_extract_fields,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols import (
    dns,
    ftp,
    http,
    tls,
)


def parse_field_extract(
    stdout: str,
    field_names: list[str],
) -> Dict[str, Any]:
    """Parse bounded `-T fields` output into legacy field_extract rows."""

    warnings: list[str] = []
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not field_names:
        warnings.append("field_extract parser did not receive allowlisted field names.")

    accepted_field_names: list[str | None] = []
    for field_name in field_names:
        try:
            accepted_field_names.append(normalize_tshark_field_extract_fields([field_name])[0])
        except ValueError as exc:
            accepted_field_names.append(None)
            warnings.append(str(exc))

    rows: list[dict[str, Any]] = []
    for physical_row_number, line in enumerate(lines, start=1):
        columns = line.split("\t")
        if _is_non_data_field_line(columns, accepted_field_names):
            warnings.append(
                f"discarded non-data field row {physical_row_number}: {_truncate_parser_line(line)}"
            )
            continue
        values: dict[str, Any] = {}
        for index, field_name in enumerate(accepted_field_names):
            if field_name is None:
                continue
            raw_value = columns[index] if index < len(columns) else None
            if raw_value in (None, ""):
                values[field_name] = None
                continue
            values[field_name] = raw_value
        if len(columns) != len(accepted_field_names):
            warnings.append(
                f"field_extract row {physical_row_number} has {len(columns)} columns for {len(accepted_field_names)} fields."
            )
        rows.append({"row": len(rows) + 1, "fields": values})

    return {
        "packet_count": len(rows),
        "field_extract": rows,
        "warnings": warnings,
    }


def parse_profile_field_output(
    stdout: str,
    field_names: list[str],
    *,
    mode: str,
    artifact_sha256: str | None,
    sensitive_proof_mode: str,
) -> Dict[str, Any]:
    """Parse deterministic `-T fields` profiles into the stable metadata contract."""

    parsed = parse_field_extract(stdout, field_names)
    rows = _extract_field_rows(parsed)
    summary = survey.parse_profile_field_summary(rows)
    result: Dict[str, Any] = {
        "packet_count": parsed["packet_count"],
        "pcap_shape": summary["pcap_shape"],
        "protocols": summary["protocols"],
        "hosts": summary["hosts"],
        "conversations": summary["conversations"],
        "services": summary["services"],
        "interesting_streams": summary["interesting_streams"],
        "recommended_next_queries": summary["recommended_next_queries"],
        "warnings": list(parsed["warnings"]),
    }
    if mode == "extract_evidence":
        result["field_extract"] = parsed["field_extract"]

    dns_rows = dns.parse_dns_field_rows(rows)
    http_rows = http.parse_http_field_rows(rows)
    tls_rows = tls.parse_tls_field_rows(rows)
    ftp_rows = ftp.parse_ftp_field_rows(rows)
    if dns_rows:
        result["dns"] = dns_rows
    if http_rows:
        result["http"] = http_rows
    if tls_rows:
        result["tls"] = tls_rows
    if ftp_rows:
        result["ftp"] = ftp_rows

    security_result = security.parse_security_field_rows(
        rows,
        artifact_sha256=artifact_sha256,
        sensitive_proof_mode=sensitive_proof_mode,
    )
    if security_result["credential_events"]:
        result["credential_events"] = security_result["credential_events"]
    if security_result["auth_indicators"]:
        result["auth_indicators"] = security_result["auth_indicators"]
    if mode == "extract_evidence" and security_result["secret_exposure"]:
        result["secret_exposure"] = security_result["secret_exposure"]
    if security_result["auth_sequences"]:
        result["auth_sequences"] = security_result["auth_sequences"]

    if mode == "investigate_protocol" and not any((dns_rows, http_rows, tls_rows, ftp_rows)):
        result["warnings"].append("No protocol-specific rows found in TShark field output.")
    if mode == "find_security_relevant_artifacts" and not security_result["auth_indicators"]:
        result["warnings"].append("No security-relevant artifact rows found in TShark field output.")
    return result


def field_rows_to_packet_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Adapt deterministic `-T fields` rows to decoded-row shape for shared signal extraction."""

    return _field_rows_to_packet_rows(rows)


def _extract_field_rows(parsed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    field_extract = parsed.get("field_extract")
    if not isinstance(field_extract, list):
        return []
    return [
        row.get("fields")
        for row in field_extract
        if isinstance(row, Mapping) and isinstance(row.get("fields"), Mapping)
    ]


def _is_non_data_field_line(columns: list[str], field_names: list[str | None]) -> bool:
    """Return True for process diagnostics accidentally mixed into field stdout."""

    try:
        frame_index = field_names.index("frame.number")
    except ValueError:
        return False
    if frame_index >= len(columns):
        return False
    frame_value = str(columns[frame_index] or "").strip()
    return bool(frame_value) and not frame_value.isdigit()


def _truncate_parser_line(line: str, *, limit: int = 160) -> str:
    normalized = " ".join(str(line or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


__all__ = (
    "field_rows_to_packet_rows",
    "parse_field_extract",
    "parse_profile_field_output",
)
