"""Canonical compact tool-output envelope schema for graph state usage.

This module owns the compact tool-result contract and the canonical
normalization helpers used by producers and transport layers. Keep compact
payload coercion here so compressor, streaming, and consumer code do not drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional


TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE = "tool_output_compressor"

CompressionSource = Literal["llm", "deterministic", "hybrid"]
LossinessRisk = Literal["low", "medium", "high"]

_STRUCTURED_SIGNAL_TYPES = frozenset(
    {
        "service",
        "header",
        "redirect",
        "path",
        "ui_link",
        "form",
        "endpoint",
        "error_context",
        "kv_pair",
    }
)
_LOSSINESS_RISKS = frozenset({"low", "medium", "high"})
_STRUCTURED_SIGNAL_FIELDS = frozenset(
    {
        "port",
        "protocol",
        "state",
        "service",
        "version",
        "name",
        "key",
        "value",
        "status",
        "size",
        "path",
        "label",
        "target",
        "method",
        "action",
        "fields",
        "redirect_target",
        "message",
        "code",
        "parameter_conflict",
    }
)


def normalize_string_list(values: Any, *, limit: Optional[int] = None) -> List[str]:
    """Coerce arbitrary input into a bounded list of non-empty strings."""
    if isinstance(values, str):
        items: Iterable[Any] = [values]
    elif isinstance(values, Mapping):
        return []
    elif isinstance(values, Iterable):
        items = values
    else:
        return []

    result: List[str] = []
    seen: set[str] = set()
    for value in items:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def normalize_lossiness_risk(value: Any, *, default: LossinessRisk = "medium") -> LossinessRisk:
    """Return a valid lossiness risk label."""
    normalized = str(value or "").strip().lower()
    if normalized in _LOSSINESS_RISKS:
        return normalized  # type: ignore[return-value]
    return default


def normalize_structured_signals(value: Any, *, limit: int = 25) -> List[Dict[str, Any]]:
    """Normalize extracted structured signals into canonical compact payload items."""
    if not isinstance(value, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue

        signal_type = str(item.get("type") or "").strip()
        if signal_type not in _STRUCTURED_SIGNAL_TYPES:
            continue

        signal: Dict[str, Any] = {"type": signal_type}
        for key, raw in item.items():
            key_text = str(key).strip()
            if (
                not key_text
                or key_text == "type"
                or raw is None
                or key_text not in _STRUCTURED_SIGNAL_FIELDS
            ):
                continue
            signal[key_text] = raw

        normalized.append(signal)
        if len(normalized) >= limit:
            break

    return normalized


@dataclass(slots=True)
class ArtifactReference:
    """Reference to persisted raw-output artifacts for drill-down."""

    path: str
    artifact_id: Optional[str] = None
    execution_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    artifact_kind: Optional[str] = None
    label: Optional[str] = None
    relative_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize artifact reference to a JSON-safe dictionary."""
        return {
            "path": self.path,
            "artifact_id": self.artifact_id,
            "execution_id": self.execution_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "artifact_kind": self.artifact_kind,
            "label": self.label,
            "relative_path": self.relative_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactReference":
        """Deserialize an artifact reference from a dictionary payload."""
        return cls(
            path=str(data.get("path", "")),
            artifact_id=(
                str(data["artifact_id"]) if data.get("artifact_id") is not None else None
            ),
            execution_id=(
                str(data["execution_id"]) if data.get("execution_id") is not None else None
            ),
            tool_call_id=(
                str(data["tool_call_id"]) if data.get("tool_call_id") is not None else None
            ),
            tool_name=(
                str(data["tool_name"]) if data.get("tool_name") is not None else None
            ),
            artifact_kind=(
                str(data["artifact_kind"]) if data.get("artifact_kind") is not None else None
            ),
            label=(
                str(data["label"]) if data.get("label") is not None else None
            ),
            relative_path=(
                str(data["relative_path"]) if data.get("relative_path") is not None else None
            ),
        )


@dataclass(slots=True)
class CompressionMetadata:
    """Metadata describing how compact output was produced."""

    source: CompressionSource
    model: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None
    fallback_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize compression metadata to a JSON-safe dictionary."""
        return {
            "source": self.source,
            "model": self.model,
            "token_usage": self.token_usage,
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompressionMetadata":
        """Deserialize compression metadata from a dictionary payload."""
        source = str(data.get("source", "deterministic"))
        if source not in {"llm", "deterministic", "hybrid"}:
            source = "deterministic"

        token_usage: Optional[Dict[str, int]] = None
        raw_token_usage = data.get("token_usage")
        if isinstance(raw_token_usage, dict):
            token_usage = {}
            for key, value in raw_token_usage.items():
                try:
                    token_usage[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

        return cls(
            source=source,  # type: ignore[arg-type]
            model=str(data["model"]) if data.get("model") is not None else None,
            token_usage=token_usage,
            fallback_reason=(
                str(data["fallback_reason"]) if data.get("fallback_reason") is not None else None
            ),
        )


@dataclass(slots=True)
class CompactToolOutput:
    """Canonical compact envelope passed through graph state and prompts."""

    tool: str
    status: str
    success: bool
    exit_code: Optional[int]
    summary: str
    key_findings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    report_recommendations: List[str] = field(default_factory=list)
    structured_signals: List[Dict[str, Any]] = field(default_factory=list)
    decision_evidence: List[str] = field(default_factory=list)
    lossiness_risk: LossinessRisk = "medium"
    artifact_refs: List[ArtifactReference] = field(default_factory=list)
    compression: Optional[CompressionMetadata] = None
    schema_version: str = "2.0"

    def __post_init__(self) -> None:
        """Normalize compact payload fields at construction time."""
        self.tool = str(self.tool or "")
        self.status = str(self.status or "")
        self.summary = str(self.summary or "")
        self.key_findings = normalize_string_list(self.key_findings)
        self.errors = normalize_string_list(self.errors, limit=5)
        self.report_recommendations = normalize_string_list(
            self.report_recommendations,
            limit=5,
        )
        self.structured_signals = normalize_structured_signals(self.structured_signals)
        self.decision_evidence = normalize_string_list(self.decision_evidence, limit=5)
        self.lossiness_risk = normalize_lossiness_risk(self.lossiness_risk)
        self.schema_version = str(self.schema_version or "2.0")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize compact envelope to a JSON-safe dictionary."""
        return {
            "schema_version": self.schema_version,
            "tool": self.tool,
            "status": self.status,
            "success": self.success,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "key_findings": list(self.key_findings),
            "errors": list(self.errors),
            "report_recommendations": list(self.report_recommendations),
            "structured_signals": list(self.structured_signals),
            "decision_evidence": list(self.decision_evidence),
            "lossiness_risk": self.lossiness_risk,
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "compression": self.compression.to_dict() if self.compression else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompactToolOutput":
        """Deserialize compact envelope from a dictionary payload."""
        raw_artifact_refs = data.get("artifact_refs", [])
        artifact_refs = [
            ArtifactReference.from_dict(item)
            for item in raw_artifact_refs
            if isinstance(item, dict)
        ]

        compression_payload = data.get("compression")
        compression = (
            CompressionMetadata.from_dict(compression_payload)
            if isinstance(compression_payload, dict)
            else None
        )

        raw_exit_code = data.get("exit_code")
        exit_code: Optional[int]
        if raw_exit_code is None:
            exit_code = None
        else:
            try:
                exit_code = int(raw_exit_code)
            except (TypeError, ValueError):
                exit_code = None

        return cls(
            schema_version=str(data.get("schema_version", "2.0")),
            tool=str(data.get("tool", "")),
            status=str(data.get("status", "")),
            success=bool(data.get("success", False)),
            exit_code=exit_code,
            summary=str(data.get("summary", "")),
            key_findings=normalize_string_list(data.get("key_findings")),
            errors=normalize_string_list(data.get("errors"), limit=5),
            report_recommendations=normalize_string_list(
                data.get("report_recommendations"),
                limit=5,
            ),
            structured_signals=normalize_structured_signals(data.get("structured_signals")),
            decision_evidence=normalize_string_list(data.get("decision_evidence"), limit=5),
            lossiness_risk=normalize_lossiness_risk(data.get("lossiness_risk")),
            artifact_refs=artifact_refs,
            compression=compression,
        )


@dataclass(slots=True)
class ToolOutputCompressionResult:
    """Result of compact compression plus optional canonical LLM usage."""

    compact_output: CompactToolOutput
    llm_compact_output: Optional[CompactToolOutput] = None
    deterministic_compact_output: Optional[CompactToolOutput] = None
    usage_record: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Keep the legacy compact output as the default LLM lane."""
        if self.llm_compact_output is None:
            self.llm_compact_output = self.compact_output
