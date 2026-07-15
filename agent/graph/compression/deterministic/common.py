"""Family-neutral deterministic compression helpers.

This module is reserved for pure normalization and metadata projection helpers
shared by deterministic compression families. It must not import the graph
compressor entrypoint, call LLMs, or perform filesystem or runtime side effects.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

from core.prompts.constants import (
    COMPACT_DECISION_EVIDENCE_MAX_CHARS,
    COMPACT_SUMMARY_MAX_CHARS,
)

from .contracts import CompressionInput, DeterministicCompressionResult
from ..schema import TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE, normalize_structured_signals

_NO_METADATA_COMPACT_REASON = "no_compact_metadata"
_ARTIFACT_REF_ALLOWED_KEYS = frozenset(
    {
        "path",
        "artifact_id",
        "execution_id",
        "tool_call_id",
        "tool_name",
        "artifact_kind",
        "label",
        "relative_path",
    }
)
_SIGNED_URL_QUERY_KEYS = frozenset(
    {
        "awsaccesskeyid",
        "expires",
        "signature",
        "sig",
        "signed",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-signature",
        "x-goog-signedheaders",
        "se",
        "sp",
        "sr",
        "sv",
    }
)
_OBJECT_STORE_SCHEMES = frozenset({"s3", "gs", "gcs", "az", "azure", "blob"})
_OBJECT_STORE_ARTIFACT_KINDS = frozenset(
    {"object_store", "object-store", "objectstore"}
)
_OBJECT_KEY_PREFIXES = ("tenant-", "tenant_", "tenants/", "object-store/", "object_store/")


def as_int(value: Any) -> Optional[int]:
    """Coerce an integer-like value, returning None for absent or invalid input."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dedupe_string_list(values: Iterable[Any], *, limit: Optional[int] = 5) -> List[str]:
    """Return trimmed, non-empty, de-duplicated strings in first-seen order."""
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def compact_evidence_line(value: Any) -> str:
    """Normalize one evidence excerpt without changing locator syntax."""
    text = str(value or "").strip()
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(text) <= COMPACT_DECISION_EVIDENCE_MAX_CHARS:
        return text
    return text[: max(COMPACT_DECISION_EVIDENCE_MAX_CHARS - 3, 0)].rstrip() + "..."


def extract_token_usage(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Return integer token counters from processor usage metadata."""
    if not isinstance(usage, dict):
        return None
    token_usage: Dict[str, int] = {}
    for key, value in usage.items():
        if value is None:
            continue
        try:
            token_usage[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return token_usage or None


def build_usage_record(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return canonical usage metadata for persistence through graph state."""
    if not isinstance(usage, dict):
        return None
    usage_record = dict(usage)
    usage_record["source"] = TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE
    usage_record["request_mode"] = "non_streaming"
    return usage_record


def sanitize_artifact_refs(
    refs: Iterable[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    """Return prompt-safe artifact refs before compact envelope construction."""

    sanitized: List[Dict[str, str]] = []
    seen_paths: set[str] = set()

    for raw_ref in refs:
        if not isinstance(raw_ref, Mapping):
            continue

        ref: Dict[str, str] = {}
        for key, value in raw_ref.items():
            key_text = str(key)
            if key_text not in _ARTIFACT_REF_ALLOWED_KEYS:
                continue
            value_text = _clean_artifact_ref_text(value)
            if value_text is not None:
                ref[key_text] = value_text

        artifact_kind = ref.get("artifact_kind")
        artifact_id = ref.get("artifact_id")
        raw_path = ref.get("path")
        relative_path = ref.get("relative_path")

        path = raw_path
        if path and _is_unsafe_artifact_path(path, artifact_kind=artifact_kind):
            path = None

        if not path and relative_path:
            if _is_safe_relative_artifact_path(
                relative_path,
                artifact_kind=artifact_kind,
            ):
                path = relative_path
            else:
                ref.pop("relative_path", None)

        if not path and artifact_id:
            path = f"artifact://{artifact_id}"

        if not path:
            continue
        if path in seen_paths:
            continue

        seen_paths.add(path)
        ref["path"] = path
        sanitized.append(ref)

    return sanitized


def _clean_artifact_ref_text(value: Any) -> Optional[str]:
    """Return stripped text for stable artifact ref fields."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _looks_signed_or_credential_url(value: str) -> bool:
    """Return True when a URL carries signed object-store credential material."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    query = parsed.query.lower()
    if not query:
        return False
    for token in query.replace(";", "&").split("&"):
        key = token.split("=", 1)[0].strip()
        if key in _SIGNED_URL_QUERY_KEYS:
            return True
    return False


def _looks_object_store_key(value: str) -> bool:
    """Return True for object-store keys that are not workspace artifact paths."""
    normalized = value.strip().replace("\\", "/")
    lowered = normalized.lower()
    return lowered.startswith(_OBJECT_KEY_PREFIXES) or "/private/" in lowered


def _is_workspace_local_path(value: str) -> bool:
    """Return True when value is a container/workspace artifact path."""
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/workspace/"):
        return True
    if normalized.startswith("/") or "://" in normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    return bool(parts) and all(part not in {".", ".."} for part in parts)


def _is_safe_relative_artifact_path(
    value: str,
    *,
    artifact_kind: Optional[str],
) -> bool:
    """Return True when relative_path can be exposed as a stable workspace handle."""
    if not _is_workspace_local_path(value) or value.startswith("/"):
        return False
    kind = str(artifact_kind or "").strip().lower()
    if kind in _OBJECT_STORE_ARTIFACT_KINDS and _looks_object_store_key(value):
        return False
    return True


def _is_unsafe_artifact_path(
    value: str,
    *,
    artifact_kind: Optional[str],
) -> bool:
    """Return True when a raw artifact path must not enter CompactToolOutput."""
    text = value.strip()
    if not text:
        return True
    try:
        parsed = urlsplit(text)
    except ValueError:
        return True

    scheme = parsed.scheme.lower()
    if scheme == "artifact":
        return False
    if _looks_signed_or_credential_url(text):
        return True
    if scheme in _OBJECT_STORE_SCHEMES:
        return True
    if text.startswith("/") and not text.startswith("/workspace/"):
        return True

    kind = str(artifact_kind or "").strip().lower()
    if kind in _OBJECT_STORE_ARTIFACT_KINDS and _looks_object_store_key(text):
        return True

    return False


def build_deterministic_summary(
    raw_result: Mapping[str, Any],
    *,
    combined_output: str,
) -> str:
    """Return the current bounded fallback summary from deterministic fields."""
    observation = str(raw_result.get("observation") or "").strip()
    if observation:
        return observation[:COMPACT_SUMMARY_MAX_CHARS]

    stdout_excerpt = str(raw_result.get("stdout_excerpt") or "").strip()
    if stdout_excerpt:
        return stdout_excerpt[:COMPACT_SUMMARY_MAX_CHARS]

    stderr_excerpt = str(raw_result.get("stderr_excerpt") or "").strip()
    if stderr_excerpt:
        return stderr_excerpt[:COMPACT_SUMMARY_MAX_CHARS]

    combined = str(combined_output or "").strip()
    if combined:
        return combined[:COMPACT_SUMMARY_MAX_CHARS]

    return "Tool execution completed without textual output."


def _metadata_compact_summary(raw_result: Mapping[str, Any]) -> str:
    """Return a tool-authored compact summary from runtime metadata."""
    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return ""
    return str(runtime_metadata.get("compact_summary") or "").strip()


def _metadata_compact_key_findings(raw_result: Mapping[str, Any]) -> List[str]:
    """Return tool-authored compact findings from runtime metadata."""
    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return []
    values = runtime_metadata.get("compact_key_findings")
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []
    return dedupe_string_list(values, limit=None)


def _metadata_compact_decision_evidence(raw_result: Mapping[str, Any]) -> List[str]:
    """Return tool-authored compact decision evidence from runtime metadata."""
    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return []
    values = runtime_metadata.get("compact_decision_evidence")
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []
    return [compact_evidence_line(value) for value in dedupe_string_list(values, limit=None)]


def _metadata_compact_structured_signals(raw_result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return tool-authored compact structured signals from runtime metadata."""
    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return []
    values = runtime_metadata.get("compact_structured_signals")
    if values is None:
        values = runtime_metadata.get("structured_signals")
    return normalize_structured_signals(values)


def metadata_compact_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project tool-authored compact metadata into a partial adapter result."""

    summary = _metadata_compact_summary(input_data.raw_result) or None
    key_findings = tuple(_metadata_compact_key_findings(input_data.raw_result))
    structured_signals = tuple(
        _metadata_compact_structured_signals(input_data.raw_result)
    )
    decision_evidence = tuple(
        _metadata_compact_decision_evidence(input_data.raw_result)
    )

    if (
        summary is None
        and not key_findings
        and not structured_signals
        and not decision_evidence
    ):
        return DeterministicCompressionResult.none(
            fallback_reason=_NO_METADATA_COMPACT_REASON,
        )

    return DeterministicCompressionResult(
        summary=summary,
        key_findings=key_findings,
        structured_signals=structured_signals,
        decision_evidence=decision_evidence,
        completeness="partial",
    )


def register_metadata_compact_adapter(tool_id: str) -> None:
    """Register the metadata compact adapter for an exact tool id or family."""

    from .registry import register_adapter

    register_adapter(tool_id, metadata_compact_adapter)
