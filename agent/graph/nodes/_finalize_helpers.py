"""Retry-aggregation helpers for the unified finalizer node.

Pulled out of ``finalize.py`` to keep the node lean. These utilities are
simple-tool only — multi-attempt aggregation does not apply on the deep
reasoning path because each iteration runs through its own observation
synthesis.

Quality rule 1 (deduplication): the canonical retry-narrative renderer
lives in ``agent.graph.nodes.finalizer`` (the cheap suffixer module).
This module imports from there rather than redefining it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, MutableMapping, Optional

from backend.services.metrics.utils import safe_gauge, safe_inc

from .finalizer import _format_retry_narrative

logger = logging.getLogger(__name__)


def _deduplicate_strings(items: List[str]) -> List[str]:
    """Case-insensitive, order-preserving string de-duplication."""
    seen: set[str] = set()
    deduped: List[str] = []
    for item in items:
        normalized = str(item).lower().strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped


def _deduplicate_findings_across_attempts(
    retry_attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deduplicate findings across retry attempts.

    Returns aggregated dict with:
      - ``all_findings``: deduplicated list of key findings
      - ``all_vulnerabilities``: deduplicated list of vulnerabilities
      - ``all_actions``: deduplicated list of next actions
      - ``retry_narrative``: human-readable retry history
    """
    if not retry_attempts:
        return {
            "all_findings": [],
            "all_vulnerabilities": [],
            "all_actions": [],
            "retry_narrative": "",
        }
    if len(retry_attempts) == 1:
        synth = retry_attempts[0].get("synthesized_output", {}) or {}
        return {
            "all_findings": list(synth.get("key_findings", []) or []),
            "all_vulnerabilities": list(synth.get("vulnerabilities", []) or []),
            "all_actions": list(synth.get("next_actions", []) or []),
            "retry_narrative": "",
        }

    findings_raw: List[str] = []
    vulns_raw: List[str] = []
    actions_raw: List[str] = []
    for attempt in retry_attempts:
        synth = attempt.get("synthesized_output", {}) or {}
        findings_raw.extend(synth.get("key_findings", []) or [])
        vulns_raw.extend(synth.get("vulnerabilities", []) or [])
        actions_raw.extend(synth.get("next_actions", []) or [])

    return {
        "all_findings": _deduplicate_strings(findings_raw),
        "all_vulnerabilities": _deduplicate_strings(vulns_raw),
        "all_actions": _deduplicate_strings(actions_raw),
        "retry_narrative": _format_retry_narrative(retry_attempts),
    }


def resolve_simple_tool_retry_context(
    metadata: MutableMapping[str, Any],
    synthesized: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve ``retry_attempts`` + ``aggregated_findings`` for the prompt.

    Mirrors the legacy simple-tool finalizer pre-prompt aggregation,
    including metrics-on-best-effort and a graceful fallback narrative
    when full deduplication fails.
    """

    retry_attempts: List[Dict[str, Any]] = list(metadata.get("retry_attempts") or [])
    aggregated_findings: Optional[Dict[str, Any]] = None

    if len(retry_attempts) > 1:
        logger.info(
            "[FINALIZE] Aggregating findings from %s attempts", len(retry_attempts)
        )
        try:
            aggregated_findings = _deduplicate_findings_across_attempts(retry_attempts)
            metadata["aggregated_findings"] = aggregated_findings
            safe_inc("simple_tool_aggregation_success")
            deduplicated_count = (
                len(aggregated_findings.get("all_findings", []))
                + len(aggregated_findings.get("all_vulnerabilities", []))
                + len(aggregated_findings.get("all_actions", []))
            )
            safe_gauge("simple_tool_findings_deduplicated", deduplicated_count)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.error("[FINALIZE] Aggregation failed: %s", exc, exc_info=True)
            safe_inc("simple_tool_aggregation_fallback")
            try:
                aggregated_findings = {
                    "all_findings": list((synthesized or {}).get("key_findings", []) or []),
                    "all_vulnerabilities": list(
                        (synthesized or {}).get("vulnerabilities", []) or []
                    ),
                    "all_actions": list((synthesized or {}).get("next_actions", []) or []),
                    "retry_narrative": _format_retry_narrative(retry_attempts),
                }
            except Exception as narrative_exc:  # pragma: no cover - belt-and-suspenders
                logger.error(
                    "[FINALIZE] Failed to construct fallback narrative: %s",
                    narrative_exc,
                )
                aggregated_findings = None

    retry_attempts_for_prompt: Optional[List[Dict[str, Any]]] = None
    if len(retry_attempts) > 1 and aggregated_findings is not None:
        retry_attempts_for_prompt = retry_attempts

    return {
        "retry_attempts": retry_attempts_for_prompt,
        "aggregated_findings": aggregated_findings,
    }


__all__ = [
    "resolve_simple_tool_retry_context",
    "_deduplicate_findings_across_attempts",
]
