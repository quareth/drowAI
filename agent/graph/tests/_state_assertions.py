"""Test-only assertions for compact tool-output state contracts."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

FORBIDDEN_RAW_OUTPUT_KEYS = frozenset({"stdout", "stderr", "stdout_excerpt", "stderr_excerpt"})
REQUIRED_COMPACT_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "tool",
        "status",
        "success",
        "exit_code",
        "summary",
        "key_findings",
        "errors",
        "report_recommendations",
        "structured_signals",
        "decision_evidence",
        "lossiness_risk",
        "artifact_refs",
        "compression",
    }
)


def _collect_forbidden_keys(payload: Any, path: str) -> list[str]:
    """Return all raw-output keys found recursively under a payload."""
    violations: list[str] = []

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_text = str(key)
            current_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_RAW_OUTPUT_KEYS:
                violations.append(current_path)
            violations.extend(_collect_forbidden_keys(value, current_path))
        return violations

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for index, item in enumerate(payload):
            current_path = f"{path}[{index}]"
            violations.extend(_collect_forbidden_keys(item, current_path))

    return violations


def assert_no_raw_tool_output_in_state(metadata: Mapping[str, Any]) -> None:
    """Raise when graph metadata contains raw tool output fields."""
    if not isinstance(metadata, Mapping):
        raise AssertionError("metadata must be a mapping")

    violations: list[str] = []
    if not metadata.get("tool_skipped"):
        violations.extend(
            _collect_forbidden_keys(metadata.get("last_tool_result"), "last_tool_result")
        )
    violations.extend(_collect_forbidden_keys(metadata.get("tool_history"), "tool_history"))

    if violations:
        joined_paths = ", ".join(sorted(set(violations)))
        raise AssertionError(
            "Raw tool output keys are forbidden in metadata. "
            f"Found forbidden keys at: {joined_paths}"
        )


def assert_compact_envelope_present(metadata: Mapping[str, Any]) -> None:
    """Raise when compact envelope is missing or malformed."""
    if not isinstance(metadata, Mapping):
        raise AssertionError("metadata must be a mapping")

    compact = metadata.get("last_tool_result_compact")
    if not isinstance(compact, Mapping):
        raise AssertionError(
            "metadata.last_tool_result_compact must exist and be a mapping"
        )

    missing_fields = sorted(REQUIRED_COMPACT_ENVELOPE_FIELDS - set(compact.keys()))
    if missing_fields:
        raise AssertionError(
            "metadata.last_tool_result_compact is missing required fields: "
            + ", ".join(missing_fields)
        )

    if not isinstance(compact.get("success"), bool):
        raise AssertionError("metadata.last_tool_result_compact.success must be a boolean")

    exit_code = compact.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise AssertionError(
            "metadata.last_tool_result_compact.exit_code must be an int or None"
        )

    list_fields = (
        "key_findings",
        "errors",
        "report_recommendations",
        "structured_signals",
        "decision_evidence",
        "artifact_refs",
    )
    for field_name in list_fields:
        if not isinstance(compact.get(field_name), list):
            raise AssertionError(
                f"metadata.last_tool_result_compact.{field_name} must be a list"
            )

    if compact.get("lossiness_risk") not in {"low", "medium", "high"}:
        raise AssertionError(
            "metadata.last_tool_result_compact.lossiness_risk must be one of: low, medium, high"
        )
