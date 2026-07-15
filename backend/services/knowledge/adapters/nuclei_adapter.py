"""Deterministic adapter for nuclei vulnerability scanner output.

This adapter normalizes nuclei metadata/evidence into canonical observations:
- finding.vulnerability_detected"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from typing import Any, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .semantic_common import extract_semantic_observations
from .web_common import (
    build_web_path_subject_key,
    build_finding_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    normalize_url,
    resolve_evidence_refs,
    sanitize_token,
)

_NUCLEI_LINE_RE = re.compile(
    r"^\[(?P<template>[^\]]+)\]\s+\[(?P<severity>[^\]]+)\]\s+(?P<target>\S+)(?:\s+\((?P<matcher>[^)]+)\))?",
    re.IGNORECASE,
)


class NucleiKnowledgeAdapter:
    """Normalize nuclei execution payloads into canonical finding observations."""

    tool_names = ("web_applications.web_vulnerability_scanners.nuclei",)
    capability_families = ("web_scanning", "vulnerability_scanning")

    def supports(self, context: AdapterContext) -> bool:
        source_tool = context.source_tool_name()
        if source_tool in self.tool_names:
            return True
        return context.capability_family() in self.capability_families

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        source_tool = context.source_tool_name() or self.tool_names[0]
        semantic = extract_semantic_observations(
            context,
            allowed_subject_types_by_observation={
                "finding.vulnerability_detected": {"finding.instance"}
            },
        )
        if semantic:
            # Inject evidence_refs into semantic observations that lack them
            evidence_refs = resolve_evidence_refs(context)
            if evidence_refs:
                enriched: list[ObservationCreate] = []
                for obs in semantic:
                    payload = dict(obs.payload or {})
                    if not payload.get("evidence_refs"):
                        payload["evidence_refs"] = evidence_refs
                        obs = ObservationCreate(
                            user_id=obs.user_id,
                            engagement_id=obs.engagement_id,
                            task_id=obs.task_id,
                            source_execution_id=obs.source_execution_id,
                            ingestion_run_id=obs.ingestion_run_id,
                            observation_type=obs.observation_type,
                            subject_type=obs.subject_type,
                            subject_key=obs.subject_key,
                            assertion_level=obs.assertion_level,
                            payload=payload,
                            observation_metadata=dict(obs.observation_metadata or {}),
                            observed_at=obs.observed_at,
                            dedupe_key=obs.dedupe_key,
                        )
                    enriched.append(obs)
                return enriched
            return semantic

        evidence_refs = resolve_evidence_refs(context)
        findings = self._collect_findings(context)
        observations: list[ObservationCreate] = []

        for finding in findings:
            detector_id = sanitize_token(
                finding.get("template_id")
                or finding.get("template")
                or finding.get("matcher")
                or "nuclei"
            )
            if not detector_id:
                continue
            target_url = normalize_url(finding.get("target_url"))
            if not target_url:
                continue
            matcher_id = sanitize_token(finding.get("matcher"))
            subject_key = build_finding_subject_key(
                detector_id=detector_id,
                target_url=target_url,
                variant_id=matcher_id or None,
            )
            payload: dict[str, Any] = {
                "source": "nuclei",
                "detector_id": detector_id,
                "target_url": target_url,
            }
            severity = sanitize_token(finding.get("severity"))
            if severity:
                payload["severity"] = severity
            confidence = sanitize_token(finding.get("confidence"))
            if confidence:
                payload["confidence"] = confidence
            if matcher_id:
                payload["matcher_id"] = matcher_id
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs

            # Pass through rich fields from normalized tool_metadata results
            for key in (
                "title",
                "description_summary",
                "classification",
                "tags",
                "references",
                "matched_at",
                "extracted_results",
            ):
                value = finding.get(key)
                if value is not None and value != "" and value != []:
                    payload[key] = value

            observations.append(
                make_observation(
                    context=context,
                    observation_type="finding.vulnerability_detected",
                    subject_type="finding.instance",
                    subject_key=subject_key,
                    payload=payload,
                )
            )

            path_observation = self._build_conservative_path_observation(
                context=context,
                source_tool=source_tool,
                finding=finding,
                evidence_refs=evidence_refs,
            )
            if path_observation is not None:
                observations.append(path_observation)

        return dedupe_observations(observations)

    @staticmethod
    def _is_explicit_path_discovery_row(finding: Mapping[str, Any]) -> bool:
        for key in ("path_discovery", "path_existence"):
            value = finding.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
        return False

    def _build_conservative_path_observation(
        self,
        *,
        context: AdapterContext,
        source_tool: str,
        finding: Mapping[str, Any],
        evidence_refs: list[dict[str, Any]],
    ) -> ObservationCreate | None:
        if not self._is_explicit_path_discovery_row(finding):
            return None

        target_url = normalize_url(finding.get("target_url"))
        if not target_url:
            return None
        parsed_target = urlsplit(target_url)
        if not parsed_target.scheme or not parsed_target.netloc:
            return None
        if parsed_target.path in {"", "/"}:
            return None

        subject_key = build_web_path_subject_key(url=target_url)
        if not subject_key:
            return None

        payload: dict[str, Any] = {
            "source": source_tool,
            "path": parsed_target.path,
            "target_url": target_url,
        }
        status_code = finding.get("status_code")
        if isinstance(status_code, int) and status_code > 0:
            payload["status_code"] = status_code
        response_size = finding.get("response_size")
        if isinstance(response_size, int) and response_size >= 0:
            payload["response_size"] = response_size
        if evidence_refs:
            payload["evidence_refs"] = evidence_refs
        return make_observation(
            context=context,
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key=subject_key,
            payload=payload,
        )

    def _collect_findings(self, context: AdapterContext) -> list[dict[str, Any]]:
        from_metadata = self._extract_from_tool_metadata(context.tool_metadata)
        if from_metadata:
            return from_metadata
        return self._extract_from_artifact_text(context)

    @staticmethod
    def _extract_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        results = tool_metadata.get("results")
        if not isinstance(results, list):
            return rows
        for item in results:
            if not isinstance(item, Mapping):
                continue
            target_url = (
                item.get("target_url")
                or item.get("matched_at")
                or item.get("matched-at")
                or item.get("url")
                or item.get("host")
                or item.get("target")
            )
            template_id = item.get("template_id") or item.get("template-id") or item.get("template")
            matcher = item.get("matcher") or item.get("matcher_name") or item.get("matcher-name")
            severity = item.get("severity") or item.get("info", {}).get("severity")
            confidence = item.get("confidence") or item.get("info", {}).get("confidence")
            row = {
                "target_url": target_url,
                "template_id": template_id,
                "matcher": matcher,
                "severity": severity,
                "confidence": confidence,
            }
            for key in ("path_discovery", "path_existence", "status_code", "response_size"):
                value = item.get(key)
                if value is not None:
                    row[key] = value
            for key in (
                "title",
                "description_summary",
                "classification",
                "tags",
                "references",
                "matched_at",
                "extracted_results",
            ):
                value = item.get(key)
                if value is not None and value != "" and value != []:
                    row[key] = value
            if row["target_url"] and row["template_id"]:
                rows.append(row)
        return rows

    def _extract_from_artifact_text(self, context: AdapterContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            for line in content.splitlines():
                parsed = self._parse_nuclei_line(line)
                if parsed is not None:
                    rows.append(parsed)
        return rows

    @staticmethod
    def _parse_nuclei_line(line: str) -> dict[str, Any] | None:
        cleaned = str(line or "").strip()
        match = _NUCLEI_LINE_RE.match(cleaned)
        if not match:
            return None
        return {
            "template_id": match.group("template"),
            "severity": match.group("severity"),
            "target_url": match.group("target"),
            "matcher": match.group("matcher"),
        }
