"""Tool-output compression boundary that builds the compact state envelope."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from agent.context.tool_processor import UniversalToolProcessor
from agent.providers.llm.core.exceptions import LLMRefusalError
from agent.semantic.enrichment import extract_runtime_semantic_inputs_with_fallback
from core.prompts.constants import COMPACT_ERROR_ENTRY_MAX_CHARS
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts

from .deterministic.common import (
    _metadata_compact_decision_evidence,
    _metadata_compact_key_findings,
    _metadata_compact_summary,
    _metadata_compact_structured_signals,
    as_int,
    build_deterministic_summary,
    build_usage_record,
    dedupe_string_list,
    extract_token_usage,
)
from .deterministic.envelope import (
    derive_compact_errors,
    extract_artifact_refs,
    merge_decision_evidence,
)
from .pentest_facts import CompactFactContext, project_compact_facts
from .schema import (
    CompactToolOutput,
    CompressionMetadata,
    ToolOutputCompressionResult,
)

_SENSITIVE_PARAMETER_KEY_TOKENS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "pass",
    "password",
    "secret",
    "token",
)
_DURABLE_SECRET_MASK_RE = re.compile(r"<DURABLE_SECRET_MASK:[^>]+>")
_SENSITIVE_ERROR_ASSIGNMENT_RE = re.compile(
    r"(?i)\b"
    r"(authorization|bearer|cookie|password|passwd|pwd|secret|token|api[_-]?key)"
    r"(\s*[:=]\s*)"
    r"([^\s,;]+)"
)


def _build_processor_input(raw_result: Mapping[str, Any]) -> str:
    stdout_text = str(raw_result.get("stdout") or "")
    stderr_text = str(raw_result.get("stderr") or "")
    combined = []
    if stdout_text:
        combined.append(stdout_text)
    if stderr_text:
        combined.append(stderr_text)
    return "\n".join(combined)


def _build_processor_metadata(
    *,
    status: str,
    raw_result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Assemble processor metadata while carrying shared semantic envelope fields."""
    metadata: Dict[str, Any] = {
        "status": status,
        "tool_params": _sanitize_processor_metadata_value(raw_result.get("parameters")),
        "stdout": raw_result.get("stdout"),
        "stderr": raw_result.get("stderr"),
    }

    tool_intent = raw_result.get("tool_intent")
    if isinstance(tool_intent, str) and tool_intent.strip():
        metadata["tool_intent"] = tool_intent.strip()

    runtime_metadata = raw_result.get("metadata")
    semantic_inputs = extract_runtime_semantic_inputs_with_fallback(
        runtime_metadata if isinstance(runtime_metadata, Mapping) else None,
        fallback_metadata=raw_result,
    )

    semantic_observations = semantic_inputs["semantic_observations"]
    if semantic_observations:
        metadata["semantic_observations"] = list(semantic_observations)

    semantic_evidence = semantic_inputs["semantic_evidence"]
    if semantic_evidence:
        metadata["semantic_evidence"] = list(semantic_evidence)

    capability_family = semantic_inputs["capability_family"]
    if isinstance(capability_family, str) and capability_family.strip():
        metadata["capability_family"] = capability_family

    semantic_schema_version = semantic_inputs["semantic_schema_version"]
    if isinstance(semantic_schema_version, str) and semantic_schema_version.strip():
        metadata["semantic_schema_version"] = semantic_schema_version

    return metadata


def _sanitize_processor_metadata_value(value: Any) -> Any:
    """Redact sensitive parameter values before prompt rendering."""

    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(token in lowered for token in _SENSITIVE_PARAMETER_KEY_TOKENS):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = _sanitize_processor_metadata_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_processor_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_processor_metadata_value(item) for item in value)
    return value


def _build_canonical_pentest_fact_projection(
    *,
    tool_name: str,
    raw_result: Mapping[str, Any],
    artifact_path: Optional[str],
    execution_id: Optional[str],
    status: str,
    success: bool,
    exit_code: Optional[int],
) -> Optional[CompactToolOutput]:
    """Build compact output from compiled canonical pentest facts only."""

    try:
        semantic_inputs = extract_runtime_semantic_inputs_with_fallback(
            raw_result.get("metadata")
            if isinstance(raw_result.get("metadata"), Mapping)
            else None,
            fallback_metadata=raw_result,
        )
        envelope = SemanticFactEnvelope(
            semantic_schema_version=semantic_inputs["semantic_schema_version"],
            capability_family=semantic_inputs["capability_family"],
            observations=tuple(semantic_inputs["semantic_observations"]),
            evidence=tuple(semantic_inputs["semantic_evidence"]),
        )
        compiled = compile_facts(envelope)
        context = _build_canonical_pentest_fact_context(
            tool_name=tool_name,
            raw_result=raw_result,
            artifact_path=artifact_path,
            execution_id=execution_id,
            status=status,
            success=success,
            exit_code=exit_code,
        )
        projected = project_compact_facts(compiled, context)
    except (TypeError, ValueError):
        return None
    if projected is None:
        return None
    return projected.compact_output


def _build_canonical_pentest_fact_context(
    *,
    tool_name: str,
    raw_result: Mapping[str, Any],
    artifact_path: Optional[str],
    execution_id: Optional[str],
    status: str,
    success: bool,
    exit_code: Optional[int],
) -> CompactFactContext:
    """Return explicit non-semantic context allowed by compact fact projection."""

    compact_key_findings = tuple(_metadata_compact_key_findings(raw_result))
    return CompactFactContext(
        tool=tool_name,
        status=status,
        success=success,
        exit_code=exit_code,
        errors=tuple(
            _derive_canonical_pentest_context_errors(
                raw_result,
                compact_key_findings=compact_key_findings,
            )
        ),
        artifact_refs=tuple(
            extract_artifact_refs(
                artifact_path=artifact_path,
                raw_result=raw_result,
                execution_id=execution_id,
            )
        ),
        compact_summary=_metadata_compact_summary(raw_result) or None,
        compact_key_findings=compact_key_findings,
        compact_structured_signals=tuple(
            _metadata_compact_structured_signals(raw_result)
        ),
        compact_decision_evidence=tuple(
            _metadata_compact_decision_evidence(raw_result)
        ),
    )


def _derive_canonical_pentest_context_errors(
    raw_result: Mapping[str, Any],
    *,
    compact_key_findings: Iterable[Any],
) -> List[str]:
    """Return bounded execution errors without accepting compact_errors metadata."""

    candidates: List[Any] = []

    for finding in compact_key_findings:
        text = str(finding or "").strip()
        if text.lower().startswith("error:"):
            candidates.append(text.split(":", 1)[1].strip())

    runtime_metadata = raw_result.get("metadata")
    if isinstance(runtime_metadata, Mapping):
        candidates.extend(_error_value_candidates(runtime_metadata.get("error")))
        timeout = runtime_metadata.get("timeout")
        if isinstance(timeout, Mapping):
            candidates.extend(_error_value_candidates(timeout.get("message")))

    return dedupe_string_list(
        (
            _sanitize_context_error(value)
            for value in candidates
            if str(value or "").strip()
        ),
        limit=5,
    )


def _error_value_candidates(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _sanitize_context_error(value: Any) -> str:
    text = " ".join(str(value or "").split())
    masked = str(
        mask_durable_secrets(text, source="canonical_pentest_compact_context")
    )
    masked = _DURABLE_SECRET_MASK_RE.sub("<redacted>", masked)
    masked = _SENSITIVE_ERROR_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        masked,
    )
    return masked[:COMPACT_ERROR_ENTRY_MAX_CHARS]


def _deterministic_catalog_role_skip_reason(tool_name: str) -> Optional[str]:
    """Return why deterministic compression should be skipped for a catalog role."""

    try:
        from agent.tools.catalog_policy import get_tool_catalog_role

        role = get_tool_catalog_role(tool_name)
    except Exception:
        return "catalog_role_resolution_error"

    role_value = str(getattr(role, "value", role) or "").strip().lower()
    if role_value == "pentest":
        return None
    if role_value:
        return f"non_pentest_catalog_role_{role_value}"
    return "catalog_role_resolution_error"


def _processed_analysis_source(processed: Any) -> str:
    """Return normalized processor analysis source metadata."""

    return str(getattr(processed, "analysis_source", "") or "").strip().lower()


def _processed_analysis_reason(processed: Any) -> str:
    """Return normalized processor fallback reason metadata."""

    return str(getattr(processed, "analysis_reason", "") or "").strip()


def _build_metadata_compact_output(
    *,
    tool_name: str,
    raw_result: Mapping[str, Any],
    artifact_path: Optional[str],
    execution_id: Optional[str],
    status: str,
    success: bool,
    exit_code: Optional[int],
) -> Optional[CompactToolOutput]:
    """Build generic metadata-only compact output after adapter retirement."""

    metadata_summary = _metadata_compact_summary(raw_result)
    summary = metadata_summary

    metadata_key_findings = _metadata_compact_key_findings(raw_result)
    key_findings = metadata_key_findings

    metadata_structured_signals = _metadata_compact_structured_signals(raw_result)
    structured_signals = metadata_structured_signals

    decision_evidence = merge_decision_evidence(
        raw_result=raw_result,
        processed_evidence=(),
        limit=5,
    )

    errors: List[str] = []
    has_deterministic_payload = bool(
        summary
        or key_findings
        or structured_signals
        or decision_evidence
        or errors
    )
    if not has_deterministic_payload:
        return None

    return CompactToolOutput(
        tool=tool_name,
        status=status,
        success=success,
        exit_code=exit_code,
        summary=summary,
        key_findings=key_findings,
        errors=errors,
        report_recommendations=[],
        structured_signals=structured_signals,
        decision_evidence=decision_evidence,
        lossiness_risk="medium",
        artifact_refs=extract_artifact_refs(
            artifact_path=artifact_path,
            raw_result=raw_result,
            execution_id=execution_id,
        ),
        compression=CompressionMetadata(
            source="deterministic",
            model=None,
            token_usage=None,
            fallback_reason="deterministic_adapter_retired",
        ),
    )


async def compress_tool_output(
    tool_name: str,
    raw_result: Dict[str, Any],
    artifact_path: Optional[str],
    execution_id: Optional[str],
    llm_client: Any,
) -> ToolOutputCompressionResult:
    """Compress raw tool output into the canonical compact envelope."""
    status = str(raw_result.get("status") or "")
    success = bool(raw_result.get("success", status == "success"))
    if not status:
        status = "success" if success else "error"
    exit_code = as_int(raw_result.get("exit_code"))
    deterministic_skip_reason = _deterministic_catalog_role_skip_reason(tool_name)

    processed = None
    fallback_reason: Optional[str] = None
    processor_ran = False

    processor_ran = True
    processor = UniversalToolProcessor(llm_client=llm_client, logger=None)
    try:
        processed = await processor.process_output(
            tool_name=tool_name,
            raw_output=_build_processor_input(raw_result),
            metadata=_build_processor_metadata(
                status=status,
                raw_result=raw_result,
            ),
        )
    except LLMRefusalError:
        raise
    except Exception:
        fallback_reason = "processor_exception"

    llm_usage = extract_token_usage(getattr(processed, "usage", None) if processed else None)
    processor_used_llm = bool(
        processed is not None
        and (
            llm_usage is not None
            or _processed_analysis_source(processed) == "llm"
        )
    )
    processed_summary = (
        str(getattr(processed, "summary", "")).strip()
        if processed is not None
        else ""
    )
    summary = processed_summary
    if not summary:
        summary = build_deterministic_summary(
            raw_result,
            combined_output=_build_processor_input(raw_result),
        )

    processed_key_findings = dedupe_string_list(
        getattr(processed, "key_findings", []) if processed else [],
        limit=None,
    )
    key_findings = processed_key_findings
    processed_structured_signals = list(
        getattr(processed, "structured_signals", []) if processed else []
    )
    structured_signals = processed_structured_signals
    processed_decision_evidence = (
        list(getattr(processed, "decision_evidence", []) if processed else [])
    )
    decision_evidence = dedupe_string_list(processed_decision_evidence, limit=5)
    lossiness_risk = str(getattr(processed, "lossiness_risk", "") or "").strip()
    if lossiness_risk not in {"low", "medium", "high"}:
        lossiness_risk = "medium"
    # Compression is extraction-only: do not generate predictive recommendations.
    report_recommendations: List[str] = []

    errors = derive_compact_errors(
        processed=processed,
        summary=summary,
        success=success,
    )
    if not errors and not success and summary:
        errors = [summary]

    processed_reason = _processed_analysis_reason(processed) if processed is not None else ""
    compression_source = "llm" if processor_used_llm else "deterministic"
    if fallback_reason is None:
        if llm_client is None and processor_ran:
            fallback_reason = "llm_client_unavailable"
        elif processed_reason:
            fallback_reason = processed_reason
        elif processor_ran and processed is None:
            fallback_reason = "llm_processing_failed"

    model_name_raw = getattr(llm_client, "model", None)
    model_name = str(model_name_raw) if isinstance(model_name_raw, str) and model_name_raw else None

    llm_compact_output = CompactToolOutput(
        tool=tool_name,
        status=status,
        success=success,
        exit_code=exit_code,
        summary=summary,
        key_findings=key_findings,
        errors=errors,
        report_recommendations=report_recommendations,
        structured_signals=structured_signals,
        decision_evidence=decision_evidence,
        lossiness_risk=lossiness_risk,
        artifact_refs=extract_artifact_refs(
            artifact_path=artifact_path,
            raw_result=raw_result,
            execution_id=execution_id,
        ),
        compression=CompressionMetadata(
            source=compression_source,
            model=model_name,
            token_usage=llm_usage,
            fallback_reason=fallback_reason,
        ),
    )
    if deterministic_skip_reason is None:
        deterministic_compact_output = _build_canonical_pentest_fact_projection(
            tool_name=tool_name,
            raw_result=raw_result,
            artifact_path=artifact_path,
            execution_id=execution_id,
            status=status,
            success=success,
            exit_code=exit_code,
        )
    else:
        deterministic_compact_output = _build_metadata_compact_output(
            tool_name=tool_name,
            raw_result=raw_result,
            artifact_path=artifact_path,
            execution_id=execution_id,
            status=status,
            success=success,
            exit_code=exit_code,
        )
    usage_record = (
        build_usage_record(getattr(processed, "usage", None))
        if processor_ran and llm_usage is not None
        else None
    )
    return ToolOutputCompressionResult(
        compact_output=llm_compact_output,
        llm_compact_output=llm_compact_output,
        deterministic_compact_output=deterministic_compact_output,
        usage_record=usage_record,
    )


def compact_output_size_bytes(compact_output: CompactToolOutput) -> int:
    """Return UTF-8 byte size for compact envelope observability metrics."""
    payload = json.dumps(
        compact_output.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return len(payload.encode("utf-8"))
