"""Deterministic adapter for sqlmap vulnerability confirmation output.

This adapter normalizes sqlmap metadata/evidence into canonical observations:
- finding.vulnerability_confirmed (only when confirmation is explicit)"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .semantic_common import extract_semantic_observations
from .web_common import (
    build_finding_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    normalize_url,
    resolve_evidence_refs,
    resolve_target_url,
    sanitize_token,
)

_PARAMETER_LINE_RE = re.compile(r"^Parameter:\s*(?P<param>[^\s]+)\s*\(", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^\s*Type:\s*(?P<type>.+)$", re.IGNORECASE)


class SqlmapKnowledgeAdapter:
    """Normalize sqlmap execution payloads into confirmed vulnerability observations."""

    tool_names = ("web_applications.web_vulnerability_scanners.sqlmap",)
    capability_families = ("web_scanning", "vulnerability_scanning", "sql_injection")

    def supports(self, context: AdapterContext) -> bool:
        source_tool = context.source_tool_name()
        if source_tool in self.tool_names:
            return True
        return context.capability_family() in self.capability_families

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = extract_semantic_observations(
            context,
            allowed_subject_types_by_observation={
                "finding.vulnerability_confirmed": {"finding.instance"}
            },
        )
        if semantic:
            return semantic

        evidence_refs = resolve_evidence_refs(context)
        target_url = resolve_target_url(context)
        confirmations = self._collect_confirmed_injections(context)
        observations: list[ObservationCreate] = []

        for confirmation in confirmations:
            parameter = sanitize_token(confirmation.get("parameter"))
            if not parameter:
                continue
            detector = sanitize_token(confirmation.get("detector_id") or "sqlmap")
            injection_type = sanitize_token(confirmation.get("injection_type") or "sql-injection")
            resolved_target = normalize_url(confirmation.get("target_url") or target_url)
            if not resolved_target:
                continue
            subject_key = build_finding_subject_key(
                detector_id=detector,
                target_url=resolved_target,
                parameter=parameter,
                variant_id=injection_type,
            )
            payload: dict[str, Any] = {
                "source": "sqlmap",
                "detector_id": detector,
                "target_url": resolved_target,
                "parameter": parameter,
                "injection_type": injection_type,
                "confidence": "confirmed",
            }
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type="finding.vulnerability_confirmed",
                    subject_type="finding.instance",
                    subject_key=subject_key,
                    payload=payload,
                )
            )
        return dedupe_observations(observations)

    def _collect_confirmed_injections(self, context: AdapterContext) -> list[dict[str, Any]]:
        from_metadata = self._extract_confirmed_from_tool_metadata(context.tool_metadata, context=context)
        if from_metadata:
            return from_metadata
        return self._extract_confirmed_from_artifacts(context)

    def _extract_confirmed_from_tool_metadata(
        self,
        tool_metadata: Mapping[str, Any],
        *,
        context: AdapterContext,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        vulnerabilities = tool_metadata.get("vulnerabilities")
        if isinstance(vulnerabilities, list):
            for item in vulnerabilities:
                if not isinstance(item, Mapping):
                    continue
                parameter = item.get("parameter")
                injection_type = item.get("type")
                title = item.get("title")
                if parameter and (injection_type or title):
                    rows.append(
                        {
                            "parameter": parameter,
                            "injection_type": injection_type or title,
                            "detector_id": "sqlmap",
                            "target_url": resolve_target_url(context),
                        }
                    )
        if rows:
            return rows

        stdout = tool_metadata.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return self._extract_confirmed_from_text(stdout, context=context)
        return rows

    def _extract_confirmed_from_artifacts(self, context: AdapterContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            rows.extend(self._extract_confirmed_from_text(content, context=context))
        return rows

    @staticmethod
    def _extract_confirmed_from_text(text: str, *, context: AdapterContext) -> list[dict[str, Any]]:
        content = str(text or "")
        if "sqlmap identified the following injection point" not in content.lower():
            return []

        rows: list[dict[str, Any]] = []
        current_parameter: str | None = None
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            param_match = _PARAMETER_LINE_RE.match(line)
            if param_match:
                current_parameter = param_match.group("param")
                continue
            type_match = _TYPE_LINE_RE.match(line)
            if type_match and current_parameter:
                rows.append(
                    {
                        "parameter": current_parameter,
                        "injection_type": type_match.group("type"),
                        "detector_id": "sqlmap",
                        "target_url": resolve_target_url(context),
                    }
                )
        return rows
