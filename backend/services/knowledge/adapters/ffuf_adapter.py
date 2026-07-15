"""Deterministic adapter for ffuf web-path discovery outputs.

This adapter normalizes ffuf crawler/fuzzer executions into canonical
`web.path_discovered` observations while enforcing backend subject-key
normalization and bounded per-origin emission.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..contracts import ObservationCreate
from .base import AdapterContext
from .web_common import (
    build_web_origin_key,
    build_web_path_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    resolve_evidence_refs,
    resolve_target_url,
)


class FfufKnowledgeAdapter:
    """Normalize ffuf execution payloads into canonical web observations."""

    tool_names = (
        "web_applications.web_application_fuzzers.ffuf",
        "web_applications.web_crawlers.ffuf",
    )
    capability_families = ()
    _MAX_PATHS_PER_ORIGIN = 200
    _SOFT_404_STATUS_CODE = 404
    _SOFT_404_RESPONSE_SIZE_MAX = 255

    def supports(self, context: AdapterContext) -> bool:
        return context.source_tool_name() in self.tool_names

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        source_tool = context.source_tool_name()
        is_crawler = source_tool == "web_applications.web_crawlers.ffuf"
        rows: list[dict[str, Any]] = []
        if is_crawler:
            rows = self._extract_rows_from_semantic_observations(context.semantic_observations)
        if not rows:
            rows = self._extract_rows_from_tool_metadata(context.tool_metadata)
        if not rows:
            rows = self._extract_rows_from_artifact_json(context)
        if not rows:
            return []

        rows = self._apply_pre_observation_drop_rules(rows)
        if not rows:
            return []

        rows = self._apply_per_origin_cap(rows)
        if not rows:
            return []

        evidence_refs = resolve_evidence_refs(context)
        target_hint = resolve_target_url(context)
        observations: list[ObservationCreate] = []
        for row in rows:
            raw_url = str(row.get("url") or "").strip()
            if not raw_url:
                continue
            subject_key = build_web_path_subject_key(url=raw_url)
            if not subject_key:
                continue
            canonical_url = subject_key.removeprefix("web.path:")
            path = str(row.get("path") or "").strip() or (urlsplit(canonical_url).path or "/")
            payload: dict[str, Any] = {
                "source": source_tool,
                "path": path,
                "target_url": str(row.get("target_url") or "").strip() or target_hint or canonical_url,
            }

            status_code = row.get("status_code")
            if isinstance(status_code, int) and status_code > 0:
                payload["status_code"] = status_code
            response_size = row.get("response_size")
            if isinstance(response_size, int) and response_size >= 0:
                payload["response_size"] = response_size
            if self._is_calibrated_row(row, context.semantic_evidence):
                payload["calibrated"] = True
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

    def _apply_pre_observation_drop_rules(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            status_code = row.get("status_code")
            response_size = row.get("response_size")
            if (
                isinstance(status_code, int)
                and status_code == self._SOFT_404_STATUS_CODE
                and isinstance(response_size, int)
                and response_size <= self._SOFT_404_RESPONSE_SIZE_MAX
            ):
                continue
            kept.append(dict(row))
        return kept

    @staticmethod
    def _extract_rows_from_semantic_observations(
        semantic_observations: Sequence[Mapping[str, Any]],
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
            if not url_value:
                continue

            row: dict[str, Any] = {"url": url_value}
            path = str(payload_dict.get("path") or "").strip()
            if path:
                row["path"] = path
            target_url = str(payload_dict.get("target_url") or "").strip()
            if target_url:
                row["target_url"] = target_url

            raw_status = payload_dict.get("status_code")
            if not isinstance(raw_status, int):
                raw_status = payload_dict.get("status")
            if isinstance(raw_status, int):
                row["status_code"] = raw_status

            raw_size = payload_dict.get("response_size")
            if not isinstance(raw_size, int):
                raw_size = payload_dict.get("length")
            if isinstance(raw_size, int):
                row["response_size"] = raw_size
            rows.append(row)
        return rows

    @staticmethod
    def _extract_rows_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        config = tool_metadata.get("config")
        config_dict = config if isinstance(config, Mapping) else {}
        target_hint = str(config_dict.get("url") or config_dict.get("target") or "").strip()
        results = tool_metadata.get("results")
        if not isinstance(results, list):
            return rows
        for item in results:
            if not isinstance(item, Mapping):
                continue
            url_value = str(item.get("url") or item.get("target_url") or "").strip()
            if not url_value:
                continue
            row: dict[str, Any] = {"url": url_value}
            path = str(item.get("path") or "").strip()
            if path:
                row["path"] = path
            if target_hint:
                row["target_url"] = target_hint
            raw_status = item.get("status_code")
            if not isinstance(raw_status, int):
                raw_status = item.get("status")
            if isinstance(raw_status, int):
                row["status_code"] = raw_status
            raw_size = item.get("response_size")
            if not isinstance(raw_size, int):
                raw_size = item.get("length")
            if not isinstance(raw_size, int):
                raw_size = item.get("size")
            if isinstance(raw_size, int):
                row["response_size"] = raw_size
            rows.append(row)
        return rows

    def _extract_rows_from_artifact_json(self, context: AdapterContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            text = str(content or "").strip()
            if not text:
                continue
            parsed_payloads: list[Any] = []
            decoder = json.JSONDecoder()
            try:
                payload, _index = decoder.raw_decode(text)
                parsed_payloads.append(payload)
            except json.JSONDecodeError:
                for line in text.splitlines():
                    raw_line = line.strip()
                    if not raw_line or not raw_line.startswith(("{", "[")):
                        continue
                    try:
                        parsed_payloads.append(json.loads(raw_line))
                    except json.JSONDecodeError:
                        continue

            for payload in parsed_payloads:
                if isinstance(payload, list):
                    candidate_rows = payload
                elif isinstance(payload, Mapping):
                    embedded = payload.get("results")
                    candidate_rows = embedded if isinstance(embedded, list) else []
                else:
                    candidate_rows = []
                if not candidate_rows:
                    continue
                rows.extend(self._extract_rows_from_tool_metadata({"results": candidate_rows}))
        return rows

    @staticmethod
    def _is_calibrated_row(
        row: Mapping[str, Any],
        semantic_evidence: Sequence[Mapping[str, Any]],
    ) -> bool:
        autocalibration_enabled = False
        calibrated_status_filters: list[int] = []
        calibrated_size_filters: list[int] = []

        for evidence in semantic_evidence:
            if not isinstance(evidence, Mapping):
                continue
            evidence_type = str(evidence.get("type") or "").strip().lower()
            name = str(evidence.get("name") or "").strip().lower()
            value = evidence.get("value")
            detail = evidence.get("detail")
            detail_dict = detail if isinstance(detail, Mapping) else {}

            if evidence_type == "baseline" and name == "autocalibration":
                if isinstance(value, bool):
                    autocalibration_enabled = value
                elif isinstance(value, str):
                    autocalibration_enabled = value.strip().lower() in {"true", "1", "yes", "on"}
                continue

            if evidence_type == "matcher_or_filter" and name == "calibrated_filter_group":
                group_text = str(value or "")
                for token in group_text.split(","):
                    key, _, token_value = token.partition("=")
                    normalized_key = key.strip().lower()
                    normalized_value = token_value.strip()
                    if normalized_key in {"status", "status_code"} and normalized_value.isdigit():
                        calibrated_status_filters.append(int(normalized_value))
                    if normalized_key in {"size", "length", "response_size"} and normalized_value.isdigit():
                        calibrated_size_filters.append(int(normalized_value))
                continue

            if evidence_type == "baseline" and name == "filter_size":
                note = str(detail_dict.get("note") or "").strip().lower()
                if note == "autocalibration_filter" and str(value).strip().isdigit():
                    calibrated_size_filters.append(int(str(value).strip()))

        if not autocalibration_enabled:
            return False

        status_code = row.get("status_code")
        response_size = row.get("response_size")
        status_match = isinstance(status_code, int) and status_code in set(calibrated_status_filters)
        size_match = isinstance(response_size, int) and response_size in set(calibrated_size_filters)
        return status_match or size_match

    def _apply_per_origin_cap(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            url_value = str(row.get("url") or "").strip()
            if not url_value:
                continue
            origin_key = build_web_origin_key(url_value)
            if not origin_key:
                continue
            grouped.setdefault(origin_key, []).append(dict(row))

        def _status_rank(item: Mapping[str, Any]) -> int:
            status_code = item.get("status_code")
            if not isinstance(status_code, int):
                return 999
            if status_code == 200:
                return 0
            if status_code == 301:
                return 1
            if status_code == 302:
                return 2
            if 200 <= status_code < 300:
                return 3
            if 300 <= status_code < 400:
                return 4
            if status_code == 401:
                return 5
            if status_code == 403:
                return 6
            return 7

        capped_rows: list[dict[str, Any]] = []
        for origin_key in sorted(grouped):
            ranked_rows = sorted(
                grouped[origin_key],
                key=lambda item: (
                    _status_rank(item),
                    str(item.get("url") or ""),
                    str(item.get("path") or ""),
                ),
            )
            capped_rows.extend(ranked_rows[: self._MAX_PATHS_PER_ORIGIN])

        return capped_rows
