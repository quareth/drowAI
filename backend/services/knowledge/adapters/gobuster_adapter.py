"""Deterministic adapter for gobuster web enumeration output.

This adapter normalizes gobuster metadata/evidence into canonical observations:
- web.path_discovered"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from typing import Any, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .web_common import (
    build_web_path_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    resolve_evidence_refs,
    resolve_target_url,
)

_GOBUSTER_LINE_RE = re.compile(r"^(?P<path>/\S+)\s+\(Status:\s*(?P<status>\d+)\)")


class GobusterKnowledgeAdapter:
    """Normalize gobuster execution payloads into canonical web observations."""

    tool_names = ("web_applications.web_crawlers.gobuster",)
    capability_families = ("web_enumeration", "web_crawling")

    def supports(self, context: AdapterContext) -> bool:
        source_tool = context.source_tool_name()
        if source_tool in self.tool_names:
            return True
        return context.capability_family() in self.capability_families

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        source_tool = context.source_tool_name() or self.tool_names[0]
        target_url = resolve_target_url(context)
        findings = self._extract_from_semantic_observations(
            semantic_observations=context.semantic_observations,
            target_url=target_url,
        )
        if not findings:
            findings = self._collect_findings(context)
        if not findings:
            return []

        evidence_refs = resolve_evidence_refs(context)
        observations: list[ObservationCreate] = []
        for item in findings:
            raw_url = str(item.get("url") or "").strip()
            raw_path = str(item.get("path") or "").strip()
            subject_key = build_web_path_subject_key(url=raw_url)
            if not subject_key and raw_path:
                subject_key = build_web_path_subject_key(target_url=target_url, discovered_path=raw_path)
            if not subject_key:
                continue
            canonical_url = subject_key.removeprefix("web.path:")
            path = raw_path or (urlsplit(canonical_url).path or "/")
            payload: dict[str, Any] = {
                "source": source_tool,
                "path": path,
                "target_url": str(item.get("target_url") or "").strip() or target_url or canonical_url,
            }
            status = item.get("status")
            if isinstance(status, int):
                payload["status_code"] = status
            size = item.get("size")
            if isinstance(size, int):
                payload["response_size"] = size
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type="web.path_discovered",
                    subject_type="web.path",
                    subject_key=subject_key,
                    payload=payload,
                )
            )
        return dedupe_observations(observations)

    @staticmethod
    def _extract_from_semantic_observations(
        *,
        semantic_observations: list[Mapping[str, Any]],
        target_url: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in semantic_observations:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("observation_type") or "").strip().lower() != "web.path_discovered":
                continue
            payload = item.get("payload")
            payload_dict = payload if isinstance(payload, Mapping) else {}
            url_value = str(payload_dict.get("url") or "").strip()
            if not url_value:
                subject_key = str(item.get("subject_key") or "").strip()
                if subject_key.startswith("web.path:"):
                    url_value = subject_key.removeprefix("web.path:")
            path_value = str(payload_dict.get("path") or "").strip()
            row: dict[str, Any] = {}
            if url_value:
                row["url"] = url_value
            if path_value:
                row["path"] = path_value
            row_target = str(payload_dict.get("target_url") or "").strip() or target_url
            if row_target:
                row["target_url"] = row_target
            raw_status = payload_dict.get("status_code")
            if not isinstance(raw_status, int):
                raw_status = payload_dict.get("status")
            if isinstance(raw_status, int):
                row["status"] = raw_status
            raw_size = payload_dict.get("response_size")
            if not isinstance(raw_size, int):
                raw_size = payload_dict.get("size")
            if isinstance(raw_size, int):
                row["size"] = raw_size
            if row.get("url") or (row_target and row.get("path")):
                rows.append(row)
        return rows

    def _collect_findings(self, context: AdapterContext) -> list[dict[str, Any]]:
        metadata_findings = self._extract_from_tool_metadata(context.tool_metadata)
        if metadata_findings:
            return metadata_findings
        return self._extract_from_artifact_text(context)

    @staticmethod
    def _extract_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        findings = tool_metadata.get("findings")
        if isinstance(findings, list):
            for item in findings:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                row: dict[str, Any] = {"path": path}
                status = item.get("status")
                if isinstance(status, int):
                    row["status"] = status
                size = item.get("size")
                if isinstance(size, int):
                    row["size"] = size
                rows.append(row)

        if rows:
            return rows

        found_paths = tool_metadata.get("found_paths")
        if isinstance(found_paths, list):
            for value in found_paths:
                path = str(value or "").strip()
                if path:
                    rows.append({"path": path})
        return rows

    def _extract_from_artifact_text(self, context: AdapterContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            for line in content.splitlines():
                parsed = self._parse_gobuster_line(line)
                if parsed is not None:
                    rows.append(parsed)
        return rows

    @staticmethod
    def _parse_gobuster_line(line: str) -> dict[str, Any] | None:
        cleaned = str(line or "").strip()
        if not cleaned.startswith("/"):
            return None
        match = _GOBUSTER_LINE_RE.match(cleaned)
        if not match:
            return None
        row: dict[str, Any] = {"path": match.group("path")}
        status_text = match.group("status")
        try:
            row["status"] = int(status_text)
        except (TypeError, ValueError):
            pass
        size_match = re.search(r"\[Size:\s*(\d+)\]", cleaned)
        if size_match:
            try:
                row["size"] = int(size_match.group(1))
            except (TypeError, ValueError):
                pass
        return row
