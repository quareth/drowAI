"""Prompt building and evidence redaction for candidate extraction.

Scope:
- Redact bounded evidence payloads.
- Render system/user prompts with existing template contracts.

Boundary:
- No LLM call execution or payload/result mapping.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from agent.context.chunking.redactor import ArtifactRedactor
from core.prompts.registry import PromptRegistry

from .contracts import CandidateExtractionRequest


class CandidatePromptBuilder:
    """Build prompts from bounded evidence and extraction request context."""

    _SYSTEM_TEMPLATE_ID = "knowledge_candidate_extraction_system"
    _USER_TEMPLATE_ID = "knowledge_candidate_extraction_user"

    def __init__(
        self,
        *,
        prompt_registry: PromptRegistry,
        redactor: ArtifactRedactor,
    ) -> None:
        self.prompt_registry = prompt_registry
        self.redactor = redactor

    def redact_evidence_bundle(self, bundle: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        redacted: list[dict[str, Any]] = []
        for item in bundle:
            content = str(item.get("content") or "")
            redacted.append(
                {
                    "evidence_archive_id": str(item.get("evidence_archive_id") or ""),
                    "artifact_kind": str(item.get("artifact_kind") or "unknown"),
                    "mode_used": str(item.get("mode_used") or "head"),
                    "content": self.redactor.redact_equal_len(content),
                }
            )
        return redacted

    def redact_compact_hint(self, compact_hint: Mapping[str, Any] | None) -> str:
        if not isinstance(compact_hint, Mapping):
            return "none"
        raw = json.dumps(dict(compact_hint), ensure_ascii=True, sort_keys=True)
        return self.redactor.redact_equal_len(raw)

    def compact_hint_masking_applied(self, compact_hint: Mapping[str, Any] | None) -> bool:
        if not isinstance(compact_hint, Mapping):
            return False
        raw = json.dumps(dict(compact_hint), ensure_ascii=True, sort_keys=True)
        return self.redactor.redact_equal_len(raw) != raw

    def build_prompts(
        self,
        *,
        request: CandidateExtractionRequest,
        bounded_evidence: Sequence[Mapping[str, Any]],
    ) -> dict[str, str]:
        system_prompt = self.prompt_registry.get_template(self._SYSTEM_TEMPLATE_ID)
        user_template = self.prompt_registry.get_template(self._USER_TEMPLATE_ID)
        compact_hint_text = self.redact_compact_hint(request.compact_output_hint)
        evidence_payload = json.dumps(
            {
                "bundle_format": "candidate_evidence_v1",
                "items": list(bounded_evidence),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        user_prompt = user_template.format_map(
            {
                "tool_name": str(request.tool_name),
                "capability_family": str(request.capability_family or "unknown"),
                "extraction_mode": str(request.extraction_mode),
                "compact_summary_hint": compact_hint_text,
                "bounded_evidence_bundle": evidence_payload,
            }
        )
        return {"system_prompt": system_prompt, "user_prompt": user_prompt}


__all__ = ["CandidatePromptBuilder"]
