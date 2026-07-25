"""Validate secret-safe JSON evidence for one DrowAI tool mechanics run."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


EXPECTED_PRESET = "nvidia_nim_openai_compatible_chat"
EXPECTED_MODEL = "openai/gpt-oss-20b"
FINAL_STATUSES = {
    "PASS",
    "FAIL",
    "INCONCLUSIVE",
    "NEEDS_CLEANUP",
    "NEEDS_CLARIFICATION",
}
FORBIDDEN_QUALITY_KEYS = {
    "answer_quality",
    "helpfulness",
    "prompt_quality",
    "prose_quality",
    "response_quality",
    "tone_quality",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~-]{8,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(
        r"(?i)\bapi[_-]?key\b\s*[:=]\s*(?![\"']?<(?:KEY_SET|NO_KEY)>)[\"']?[A-Za-z0-9._~-]{8,}"
    ),
)


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _has_forbidden_quality_key(value: Any) -> bool:
    """Return whether nested report data attempts to score answer prose."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = (
                str(key).strip().lower().replace("-", "_").replace(" ", "_")
            )
            if normalized_key in FORBIDDEN_QUALITY_KEYS:
                return True
            if _has_forbidden_quality_key(nested):
                return True
    elif isinstance(value, list):
        return any(_has_forbidden_quality_key(item) for item in value)
    return False


def _is_loopback(value: str) -> bool:
    """Return whether a host, address, range, or subnet is loopback-only."""

    normalized = value.strip().lower().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        pass
    try:
        network = ipaddress.ip_network(normalized, strict=False)
        return network.network_address.is_loopback and network.broadcast_address.is_loopback
    except ValueError:
        pass
    if "-" in normalized:
        start, _, end = normalized.partition("-")
        try:
            return ipaddress.ip_address(start).is_loopback and ipaddress.ip_address(end).is_loopback
        except ValueError:
            return False
    return False


def _is_allowed_target(value: Any) -> bool:
    """Return whether a real-execution target is loopback or RFC-reserved."""

    raw = str(value or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        return _is_loopback(parsed.hostname)
    normalized = raw.lower().rstrip(".")
    return _is_loopback(normalized) or normalized.endswith(
        (".test", ".invalid", ".localhost")
    )


def _status(section: Mapping[str, Any]) -> str:
    """Return one normalized section status."""

    return str(section.get("status") or "").strip().lower()


def validate_report(report: Mapping[str, Any], *, raw_text: str = "") -> list[str]:
    """Return stable error codes for a mechanical report contract."""

    errors: list[str] = []
    final_status = str(report.get("final_status") or "").strip().upper()
    tool_id = str(report.get("tool_id") or "").strip()

    if report.get("schema_version") != 1:
        errors.append("schema_version")
    if not tool_id:
        errors.append("tool_id")
    if final_status not in FINAL_STATUSES:
        errors.append("final_status")
    if report.get("mechanics_only") is not True:
        errors.append("mechanics_only")
    if _has_forbidden_quality_key(report):
        errors.append("quality_scoring_forbidden")
    if any(pattern.search(raw_text) for pattern in SECRET_PATTERNS):
        errors.append("secret_material")

    model = _mapping(report.get("model"))
    if model.get("preset_id") != EXPECTED_PRESET:
        errors.append("model.preset_id")
    if model.get("model_id") != EXPECTED_MODEL:
        errors.append("model.model_id")
    if model.get("connection_status") != "existing_verified":
        errors.append("model.connection_status")
    if model.get("secret_marker") not in {"<KEY_SET>", "<NO_KEY>"}:
        errors.append("model.secret_marker")

    safe_target = _mapping(report.get("safe_target"))
    if not _is_allowed_target(safe_target.get("value")):
        errors.append("safe_target.value")
    resolver = safe_target.get("controlled_resolver")
    if resolver and not _is_loopback(str(resolver)):
        errors.append("safe_target.controlled_resolver")

    schema_runs = _mapping(report.get("schema_runs"))
    minimal = _mapping(schema_runs.get("minimal"))
    full = _mapping(schema_runs.get("full"))
    if not minimal:
        errors.append("schema_runs.minimal")
    if not full:
        errors.append("schema_runs.full")
    if final_status in {"PASS", "INCONCLUSIVE"}:
        if _status(minimal) != "pass":
            errors.append("schema_runs.minimal.status")
        if _status(full) != "pass":
            errors.append("schema_runs.full.status")

    cases = _mapping(report.get("cases"))
    for name in ("success", "empty", "partial_timeout", "failure"):
        case = _mapping(cases.get(name))
        if not case:
            errors.append(f"cases.{name}")
            continue
        if final_status == "PASS" and case.get("required") is True:
            if _status(case) != "pass":
                errors.append(f"cases.{name}.status")
            if not str(case.get("evidence_ref") or "").strip():
                errors.append(f"cases.{name}.evidence_ref")

    compression = _mapping(report.get("compression"))
    counts = [compression.get(name) for name in ("total", "shown", "omitted")]
    if not all(isinstance(value, int) and value >= 0 for value in counts):
        errors.append("compression.counts")
    elif counts[0] != counts[1] + counts[2]:
        errors.append("compression.accounting")
    if compression.get("deterministic") is not True:
        errors.append("compression.deterministic")
    if compression.get("omission_marker_in_budget") is not True:
        errors.append("compression.omission_marker_in_budget")

    for name in ("artifacts", "knowledge"):
        section = _mapping(report.get(name))
        if not section:
            errors.append(name)
        elif final_status == "PASS" and section.get("required") is True:
            if _status(section) != "pass":
                errors.append(f"{name}.status")
            if not str(section.get("evidence_ref") or "").strip():
                errors.append(f"{name}.evidence_ref")

    gui = _mapping(report.get("gui"))
    attempts = gui.get("selection_attempts")
    if not isinstance(attempts, int) or attempts < 1 or attempts > 2:
        errors.append("gui.selection_attempts")
    if final_status == "PASS":
        if gui.get("selected_tool_id") != tool_id:
            errors.append("gui.selected_tool_id")
        if gui.get("parameters_preserved") is not True:
            errors.append("gui.parameters_preserved")
        if gui.get("tool_result_rendered") is not True:
            errors.append("gui.tool_result_rendered")
    if final_status == "INCONCLUSIVE":
        if gui.get("selection_classification") != "INCONCLUSIVE_MODEL_SELECTION":
            errors.append("gui.selection_classification")
        if attempts != 2:
            errors.append("gui.inconclusive_attempts")

    cleanup = _mapping(report.get("cleanup"))
    cleanup_status = _status(cleanup)
    if final_status == "PASS" and cleanup_status != "pass":
        errors.append("cleanup.status")
    if final_status == "NEEDS_CLEANUP" and cleanup_status not in {"failed", "unknown"}:
        errors.append("cleanup.needs_cleanup_status")

    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Validate a DrowAI mechanical JSON report.")
    parser.add_argument("report", type=Path, help="Path to the JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate one report without network calls or file writes."""

    args = build_parser().parse_args(argv)
    try:
        raw_text = args.report.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("[mechanical-report] invalid_json")
        return 1
    if not isinstance(payload, Mapping):
        print("[mechanical-report] root_object_required")
        return 1

    errors = validate_report(payload, raw_text=raw_text)
    if errors:
        print("[mechanical-report] FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("[mechanical-report] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
