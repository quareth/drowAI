"""Read-only last-tool projection helper for prompt builders.

This module exposes :func:`extract_last_tool_sections`, which projects the
canonical last-tool runtime state onto a fixed set of formatted prompt
section bodies, plus :func:`iter_renderable_last_tool_sections`, the shared
section-ordering helper used by PTR current-tool prompts and stored tool
phase snapshots.

Responsibilities:
    * Defensively read ``metadata['last_tool_result_compact']`` and
      ``metadata['last_tool_result']`` plus a small set of ``facts``
      attributes to produce a stable, narrow dict of section bodies.
    * Reuse last-tool formatting primitives from
      :mod:`core.prompts.builders.post_tool._formatting` so that
      truncation limits, parameter rendering, sequence/structured-signal
      formatting, and artifact-ref formatting remain consistent with PTR.
    * Never mutate ``metadata``, ``facts``, or ``synthesized``; never log.

Returned keys (always present, defaulting to ``""`` when data is missing):

    * ``tool_executed`` — ``"Tool: <name>"`` (with optional ``Parameters: ...``)
    * ``tool_output_summary`` — truncated tool summary text
    * ``batch_tool_results`` — aggregate batch status and per-call summaries
    * ``key_findings`` — bulleted compact key findings
    * ``tool_errors`` — bulleted compact tool errors
    * ``structured_signals`` — bulleted structured signals
    * ``decision_evidence`` — bulleted decision evidence
    * ``artifact_refs`` — formatted compact artifact references
    * ``compression_lossiness`` — compact lossiness risk marker
    * ``output_info`` — bounded output-condensation/artifact hint

Compact-only fields (``tool_errors``, ``structured_signals``,
``decision_evidence``, ``artifact_refs``, ``compression_lossiness``)
intentionally do not fall back to ``synthesized``. Only
``tool_output_summary`` and ``key_findings`` may fall back to
``synthesized``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from ._formatting import (
    MAX_SUMMARY_CHARS,
    as_mapping,
    as_sequence,
    format_artifact_refs,
    format_parameters,
    format_sequence,
    format_structured_signals,
    get_field,
    truncate,
)
from .evidence import read_compact_evidence

LAST_TOOL_SECTION_HEADINGS: Mapping[str, str] = {
    "tool_executed": "Tool Executed",
    "tool_output_summary": "Tool Output Summary",
    "batch_tool_results": "Batch Tool Results",
    "key_findings": "Key Findings",
    "tool_errors": "Tool Errors",
    "structured_signals": "Structured Signals",
    "decision_evidence": "Decision Evidence",
    "compression_lossiness": "Compression Lossiness",
    "artifact_refs": "Artifact References",
    "output_info": "Output Info",
}

LAST_TOOL_SECTION_ORDER: tuple[str, ...] = tuple(LAST_TOOL_SECTION_HEADINGS)


def _combine_lane_sections(llm_body: str, deterministic_body: str) -> str:
    """Render LLM then deterministic lane bodies without changing legacy output."""
    llm_text = str(llm_body or "").strip()
    deterministic_text = str(deterministic_body or "").strip()
    if not deterministic_text:
        return llm_text
    if not llm_text:
        return f"Deterministic lane:\n{deterministic_text}"
    return (
        f"LLM lane:\n{llm_text}\n"
        f"Deterministic lane:\n{deterministic_text}"
    )


def _resolve_tool_name(facts: Any, synthesized: Mapping[str, Any]) -> str:
    """Resolve the executed tool name without emitting placeholder text.

    Precedence: ``facts.selected_tool`` → ``synthesized['tool']``.
    Empty resolution returns ``""`` (never ``"unknown tool"``) so callers
    can omit the ``## Tool Executed`` section when no tool ran.
    """
    selected = str(get_field(facts, "selected_tool", "") or "").strip()
    if selected:
        return selected
    candidate = str((synthesized.get("tool") if synthesized else None) or "").strip()
    if candidate:
        return candidate
    return ""


def _resolve_parameters(
    metadata: Mapping[str, Any],
    facts: Any,
    tool_name: str,
) -> Mapping[str, Any]:
    """Pick the parameter mapping that should render after ``Tool:``.

    Precedence rules (matching PTR's inline behavior):
        1. ``metadata['last_tool_result']['parameters']`` wins when present
           and non-empty.
        2. Otherwise fall back to ``facts.tool_parameters``:
           - flat ``{param: value}`` mapping → use directly,
           - tool-keyed ``{tool_name: {param: value}}`` mapping → index by
             the resolved ``tool_name`` when available.
    """
    last_result = as_mapping(metadata.get("last_tool_result", {}))
    last_result_params = as_mapping(last_result.get("parameters"))
    if last_result_params:
        return last_result_params

    raw_facts_params = get_field(facts, "tool_parameters", {})
    facts_params = as_mapping(raw_facts_params)
    if not facts_params:
        return {}

    if tool_name and tool_name in facts_params:
        candidate = facts_params.get(tool_name)
        if isinstance(candidate, Mapping):
            # Tool-keyed mapping: use the per-tool params.
            return candidate
        # The key matches the tool name but the value is not a mapping;
        # treat the outer mapping as flat to preserve any meaningful pairs.
        return facts_params

    # Heuristic for tool-keyed shape: every value is itself a mapping and
    # there is at least one entry. In that case there is no usable tool
    # name, so we cannot pick a sub-mapping; render nothing rather than
    # leaking another tool's parameters.
    if facts_params and all(isinstance(v, Mapping) for v in facts_params.values()):
        return {}

    # Flat parameter mapping: use directly.
    return facts_params


def extract_last_tool_sections(
    metadata: Mapping[str, Any],
    facts: Any,
    synthesized: Optional[Mapping[str, Any]] = None,
    *,
    prefer_runtime_evidence: bool = False,
) -> dict[str, str]:
    """Project last-tool runtime state into formatted prompt section bodies.

    Args:
        metadata: ``facts.metadata`` mapping (or any mapping-like view).
            Read defensively; missing keys yield empty strings.
        facts: State-like object exposing ``selected_tool`` and
            ``tool_parameters`` either as mapping keys or attributes.
        synthesized: Optional synthesized tool output (e.g. from
            ``tool_synthesizer``). Only used as a fallback for
            ``summary`` / ``key_findings`` and the tool name. Pass
            ``None`` when no synthesizer ran.
        prefer_runtime_evidence: Prefer same-process raw compact evidence
            registered for the active PTR turn. Leave disabled for durable
            phase snapshots and stored prompt projections.

    Returns:
        Dict with exactly the ten keys documented in the module
        docstring. Each value is a formatted section body string;
        missing data yields ``""``.
    """
    metadata_view = as_mapping(metadata)
    synthesized_view: Mapping[str, Any] = as_mapping(synthesized) if synthesized else {}

    tool_name = _resolve_tool_name(facts, synthesized_view)

    if tool_name:
        params = _resolve_parameters(metadata_view, facts, tool_name)
        formatted_params = format_parameters(params) if params else ""
        tool_executed = f"Tool: {tool_name}"
        if formatted_params:
            tool_executed = f"{tool_executed}\nParameters: {formatted_params}"
    else:
        tool_executed = ""

    # Phase 1.4 (re-audit fix): read through ``read_compact_evidence`` so
    # the batch-aware view wins when a multi-call batch's primary call
    # carries detail that the legacy ``last_tool_result_compact`` field
    # does not. The helper already projects the legacy field as the
    # ``single``-source view when no batch metadata exists, so this is
    # the sole reader — no direct ``metadata.get('last_tool_result_compact')``
    # fallback is needed.
    evidence = read_compact_evidence(
        metadata_view,
        prefer_runtime=prefer_runtime_evidence,
    )
    compact_result: Mapping[str, Any] = {}
    deterministic_compact_result: Mapping[str, Any] = {}
    if evidence is not None and evidence.rows:
        primary = evidence.rows[0].get("compact_tool_result")
        if isinstance(primary, Mapping):
            compact_result = as_mapping(primary)
        deterministic_primary = evidence.rows[0].get(
            "deterministic_compact_tool_result"
        )
        if isinstance(deterministic_primary, Mapping):
            deterministic_compact_result = as_mapping(deterministic_primary)

    batch_tool_results = ""
    if evidence is not None and evidence.source == "batch":
        lines = [
            f"batch_status: {evidence.status}",
            f"batch_success: {evidence.success}",
        ]
        for row in evidence.rows:
            row_view = as_mapping(row)
            tool_id = str(
                row_view.get("tool_id") or row_view.get("tool") or "unknown_tool"
            )
            status = str(row_view.get("status") or "unknown")
            intent = str(row_view.get("intent") or "").strip()
            failure = str(row_view.get("failure_category") or "").strip()
            compact = as_mapping(row_view.get("compact_tool_result"))
            summary = str(compact.get("summary") or "").strip()
            deterministic_compact = as_mapping(
                row_view.get("deterministic_compact_tool_result")
            )
            deterministic_summary = str(
                deterministic_compact.get("summary") or ""
            ).strip()

            parts = [f"- {tool_id}: {status}"]
            if failure:
                parts.append(f"failure={failure}")
            if intent:
                parts.append(f"intent={intent}")
            if summary:
                summary_label = "llm_summary" if deterministic_summary else "summary"
                parts.append(
                    f"{summary_label}={truncate(summary, MAX_SUMMARY_CHARS)}"
                )
            if deterministic_summary:
                parts.append(
                    "deterministic_summary="
                    f"{truncate(deterministic_summary, MAX_SUMMARY_CHARS)}"
                )
            lines.append("; ".join(parts))
        batch_tool_results = "\n".join(lines)

    raw_summary = (
        compact_result.get("summary")
        or synthesized_view.get("summary")
        or ""
    )
    tool_output_summary = _combine_lane_sections(
        truncate(str(raw_summary), MAX_SUMMARY_CHARS),
        truncate(
            str(deterministic_compact_result.get("summary") or ""),
            MAX_SUMMARY_CHARS,
        ),
    )

    raw_key_findings = (
        compact_result.get("key_findings")
        or synthesized_view.get("key_findings")
        or []
    )
    key_findings = _combine_lane_sections(
        format_sequence(as_sequence(raw_key_findings)),
        format_sequence(
            as_sequence(deterministic_compact_result.get("key_findings"))
        ),
    )

    tool_errors = _combine_lane_sections(
        format_sequence(as_sequence(compact_result.get("errors") or [])),
        format_sequence(
            as_sequence(deterministic_compact_result.get("errors") or [])
        ),
    )
    structured_signals = _combine_lane_sections(
        format_structured_signals(as_sequence(compact_result.get("structured_signals"))),
        format_structured_signals(
            as_sequence(deterministic_compact_result.get("structured_signals"))
        ),
    )
    decision_evidence = _combine_lane_sections(
        format_sequence(as_sequence(compact_result.get("decision_evidence"))),
        format_sequence(
            as_sequence(deterministic_compact_result.get("decision_evidence"))
        ),
    )
    artifact_refs = _combine_lane_sections(
        format_artifact_refs(as_sequence(compact_result.get("artifact_refs"))),
        format_artifact_refs(
            as_sequence(deterministic_compact_result.get("artifact_refs"))
        ),
    )
    lossiness_risk = str(compact_result.get("lossiness_risk") or "").strip()
    llm_compression_lossiness = (
        f"lossiness_risk: {lossiness_risk}" if lossiness_risk else ""
    )
    deterministic_lossiness_risk = str(
        deterministic_compact_result.get("lossiness_risk") or ""
    ).strip()
    deterministic_compression_lossiness = (
        f"lossiness_risk: {deterministic_lossiness_risk}"
        if deterministic_lossiness_risk
        else ""
    )
    compression_lossiness = _combine_lane_sections(
        llm_compression_lossiness,
        deterministic_compression_lossiness,
    )

    last_result = as_mapping(metadata_view.get("last_tool_result", {}))
    artifact_path = str(metadata_view.get("last_artifact_path", "") or "").strip()
    was_truncated = bool(last_result.get("was_truncated", False))
    raw_chars_truncated = last_result.get("chars_truncated", 0)
    chars_truncated = (
        raw_chars_truncated
        if isinstance(raw_chars_truncated, int) and not isinstance(raw_chars_truncated, bool)
        else 0
    )
    suggest_file_reading = bool(last_result.get("suggest_file_reading", False))

    output_info = ""
    if artifact_path and was_truncated and chars_truncated > 0:
        if suggest_file_reading:
            output_info = (
                f"Output condensed ({chars_truncated:,} chars omitted). "
                "If key evidence is still missing and the saved path is available, "
                "use a visible filesystem read/search tool with bounded scope. "
                "Do not default to full reads."
                f"\nSaved output path: `{artifact_path}`"
            )
        else:
            output_info = (
                f"Output slightly condensed ({chars_truncated:,} chars). "
                "Compact summary likely contains required evidence; avoid extra "
                "artifact reads unless a concrete gap remains."
                f"\nSaved output path: `{artifact_path}`"
            )

    return {
        "tool_executed": tool_executed,
        "tool_output_summary": tool_output_summary,
        "batch_tool_results": batch_tool_results,
        "key_findings": key_findings,
        "tool_errors": tool_errors,
        "structured_signals": structured_signals,
        "decision_evidence": decision_evidence,
        "artifact_refs": artifact_refs,
        "compression_lossiness": compression_lossiness,
        "output_info": output_info,
    }


def iter_renderable_last_tool_sections(
    projected_sections: Mapping[str, str],
    *,
    keys: Sequence[str] | None = None,
) -> Iterable[tuple[str, str]]:
    """Yield non-empty last-tool section headings and bodies in shared order."""
    section_keys = keys or LAST_TOOL_SECTION_ORDER
    for section_key in section_keys:
        heading = LAST_TOOL_SECTION_HEADINGS.get(section_key)
        if not heading:
            continue
        body = str(projected_sections.get(section_key) or "").strip()
        if body:
            yield heading, body


__all__ = [
    "LAST_TOOL_SECTION_HEADINGS",
    "LAST_TOOL_SECTION_ORDER",
    "extract_last_tool_sections",
    "iter_renderable_last_tool_sections",
]
