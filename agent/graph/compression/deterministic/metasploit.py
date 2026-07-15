"""Metasploit deterministic compression helpers.

This module formats existing msfconsole analysis facts for compact prompt
augmentation. It does not execute Metasploit, inspect artifacts, call LLMs, or
introduce parser behavior beyond the tool-layer Metasploit analysis module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS
from agent.tools.exploitation_tools.metasploit.analysis import (
    MetasploitAnalysis,
    analyze_msfconsole_metadata,
    analyze_msfconsole_output,
)

from .common import compact_evidence_line, dedupe_string_list, sanitize_artifact_refs
from .contracts import CompressionInput, DeterministicCompressionResult

MSF_SEARCH_MODULES_TOOL_ID = "exploitation_tools.metasploit.search_modules"
MSF_INSPECT_MODULE_TOOL_ID = "exploitation_tools.metasploit.inspect_module"
MSF_RUN_EXPLOIT_TOOL_ID = "exploitation_tools.metasploit.run_exploit"

_REGISTERED_METASPLOIT_TOOL_IDS: tuple[str, ...] = (
    MSF_SEARCH_MODULES_TOOL_ID,
    MSF_INSPECT_MODULE_TOOL_ID,
    MSF_RUN_EXPLOIT_TOOL_ID,
)
_MODULE_LIMIT = 5
_ERROR_LIMIT = 5
_ARTIFACT_REF_LIMIT = 3


def metasploit_adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
    """Project existing msfconsole analysis facts into compact evidence."""

    if input_data.tool_name not in _REGISTERED_METASPLOIT_TOOL_IDS:
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_metasploit_tool",
        )

    analysis = _metasploit_analysis(input_data.raw_result)
    if analysis is None:
        return DeterministicCompressionResult.none(
            fallback_reason="no_metasploit_metadata",
        )

    findings = _key_findings(
        analysis,
        tool_name=input_data.tool_name,
        raw_result=input_data.raw_result,
    )
    evidence = _decision_evidence(analysis, tool_name=input_data.tool_name)
    return DeterministicCompressionResult(
        summary=_summary(
            _summary_text(analysis=analysis, tool_name=input_data.tool_name)
        ),
        key_findings=tuple(findings),
        errors=tuple(compact_evidence_line(error) for error in analysis.errors[:_ERROR_LIMIT]),
        structured_signals=tuple(
            _structured_signals(
                analysis,
                tool_name=input_data.tool_name,
                raw_result=input_data.raw_result,
            )
        ),
        decision_evidence=tuple(evidence),
        completeness="partial",
        lossiness_risk="low",
    )


def registered_metasploit_tool_ids() -> tuple[str, ...]:
    """Return visible Metasploit tool ids registered for deterministic coverage."""

    return _REGISTERED_METASPLOIT_TOOL_IDS


def register_metasploit_adapters() -> None:
    """Register deterministic adapters for visible narrow msfconsole tools."""

    from .registry import register_adapter

    for tool_id in _REGISTERED_METASPLOIT_TOOL_IDS:
        register_adapter(tool_id, metasploit_adapter)


def _metasploit_analysis(raw_result: Mapping[str, Any]) -> Optional[MetasploitAnalysis]:
    metadata = raw_result.get("metadata")
    if isinstance(metadata, Mapping) and _looks_like_msfconsole_metadata(metadata):
        return analyze_msfconsole_metadata(metadata)

    stdout = str(raw_result.get("stdout") or "")
    stderr = str(raw_result.get("stderr") or "")
    if stdout.strip():
        return analyze_msfconsole_output(stdout=stdout, stderr=stderr)
    return None


def _looks_like_msfconsole_metadata(metadata: Mapping[str, Any]) -> bool:
    return any(
        key in metadata
        for key in (
            "parsed_output",
            "sessions_created",
            "modules_loaded",
            "exploits_executed",
            "exploit_succeeded",
            "execution_mode",
        )
    )


def _summary_text(*, analysis: MetasploitAnalysis, tool_name: str) -> str:
    tool_label = tool_name.rsplit(".", 1)[-1]
    if tool_name == MSF_RUN_EXPLOIT_TOOL_ID:
        outcome = (
            "succeeded"
            if analysis.exploit_succeeded is True
            else "did not create a session"
        )
        return (
            f"Metasploit {tool_label} {outcome}; "
            f"sessions={analysis.sessions_created}, modules={len(analysis.modules_loaded)}."
        )
    return (
        f"Metasploit {tool_label} parsed msfconsole output; "
        f"sessions={analysis.sessions_created}, modules={len(analysis.modules_loaded)}."
    )


def _key_findings(
    analysis: MetasploitAnalysis,
    *,
    tool_name: str,
    raw_result: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    if analysis.execution_mode:
        findings.append(f"execution mode: {analysis.execution_mode}")
    if analysis.modules_loaded:
        modules = dedupe_string_list(analysis.modules_loaded, limit=_MODULE_LIMIT)
        findings.append(f"modules loaded: {', '.join(modules)}")
    findings.append(f"sessions created: {analysis.sessions_created}")
    if tool_name == MSF_RUN_EXPLOIT_TOOL_ID and analysis.exploit_succeeded is not None:
        findings.append(f"exploit_succeeded: {str(analysis.exploit_succeeded).lower()}")
    raw_excerpt = _raw_output_excerpt(analysis)
    if raw_excerpt:
        findings.append(f"raw excerpt: {raw_excerpt}")
    for info in analysis.info[:_ERROR_LIMIT]:
        findings.append(f"info: {compact_evidence_line(info)}")
    for warning in analysis.warnings[:_ERROR_LIMIT]:
        findings.append(f"warning: {compact_evidence_line(warning)}")
    for error in analysis.errors[:_ERROR_LIMIT]:
        findings.append(f"error: {compact_evidence_line(error)}")
    findings.extend(_artifact_findings(raw_result))
    return findings


def _decision_evidence(
    analysis: MetasploitAnalysis,
    *,
    tool_name: str,
) -> list[str]:
    tool_label = tool_name.rsplit(".", 1)[-1]
    evidence = [
        compact_evidence_line(
            f"metasploit {tool_label}: sessions={analysis.sessions_created} "
            f"modules={len(analysis.modules_loaded)}"
        )
    ]
    for module in analysis.modules_loaded[:_MODULE_LIMIT]:
        evidence.append(compact_evidence_line(f"metasploit module: {module}"))
    for info in analysis.info[:_ERROR_LIMIT]:
        evidence.append(compact_evidence_line(f"metasploit info: {info}"))
    for error in analysis.errors[:_ERROR_LIMIT]:
        evidence.append(compact_evidence_line(f"metasploit error: {error}"))
    return evidence


def _structured_signals(
    analysis: MetasploitAnalysis,
    *,
    tool_name: str,
    raw_result: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    tool_label = tool_name.rsplit(".", 1)[-1]
    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": "metasploit_tool", "value": tool_label},
        {
            "type": "kv_pair",
            "key": "metasploit_sessions_created",
            "value": analysis.sessions_created,
        },
        {
            "type": "kv_pair",
            "key": "metasploit_modules_loaded_count",
            "value": len(analysis.modules_loaded),
        },
    ]
    if analysis.exploit_succeeded is not None:
        signals.append(
            {
                "type": "kv_pair",
                "key": "metasploit_exploit_succeeded",
                "value": analysis.exploit_succeeded,
            }
        )
    if analysis.execution_mode:
        signals.append(
            {
                "type": "kv_pair",
                "key": "metasploit_execution_mode",
                "value": analysis.execution_mode,
            }
        )
    for module in analysis.modules_loaded[:_MODULE_LIMIT]:
        signals.append({"type": "kv_pair", "key": "metasploit_module", "value": module})
    for ref in _artifact_refs(raw_result):
        signals.append(
            {
                "type": "kv_pair",
                "key": "metasploit_artifact_ref",
                "value": ref["path"],
            }
        )
    return signals


def _artifact_findings(raw_result: Mapping[str, Any]) -> list[str]:
    return [f"artifact: {ref['path']}" for ref in _artifact_refs(raw_result)]


def _raw_output_excerpt(analysis: MetasploitAnalysis) -> Optional[str]:
    raw_output = analysis.parsed_output.get("raw_output")
    if not isinstance(raw_output, str):
        return None
    for line in raw_output.splitlines():
        excerpt = compact_evidence_line(line)
        if excerpt:
            return excerpt
    return None


def _artifact_refs(raw_result: Mapping[str, Any]) -> list[dict[str, str]]:
    candidates: list[Mapping[str, Any]] = []
    raw_artifacts = raw_result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for artifact in raw_artifacts:
            if isinstance(artifact, Mapping):
                candidates.append(artifact)
            elif isinstance(artifact, str):
                candidates.append({"path": artifact})
    return sanitize_artifact_refs(candidates)[:_ARTIFACT_REF_LIMIT]


def _summary(value: str) -> str:
    return value[:COMPACT_SUMMARY_MAX_CHARS]


register_metasploit_adapters()
