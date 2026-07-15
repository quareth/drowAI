"""LLM-powered universal tool output processor.

This module analyzes raw tool output into a compact extraction-oriented
summary shape for downstream graph compression. It owns prompt rendering and
deterministic fallback behavior, but not compact envelope normalization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List
import re

from agent.providers.llm.core.exceptions import LLMRefusalError
from core.llm import LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC, wait_for_with_timeout
from core.prompts.constants import (
    COMPACT_DECISION_EVIDENCE_MAX_CHARS,
    COMPACT_ERROR_CONTEXT_MESSAGE_MAX_CHARS,
    COMPACT_ERROR_LEAD_MAX_CHARS,
    COMPACT_FAILURE_FINDING_MAX_CHARS,
    COMPACT_FAILURE_STDOUT_LINE_MAX_CHARS,
    COMPACT_FINDING_MAX_CHARS,
    COMPACT_RULE_FINDING_MAX_CHARS,
    COMPACT_SUMMARY_MAX_CHARS,
)
from core.prompts.registry import PromptRegistry
from core.llm.structured_schemas import TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT
from agent.semantic.enrichment import (
    extract_runtime_semantic_inputs_with_fallback,
    render_semantic_evidence_for_prompt,
    render_semantic_observations_for_prompt,
)
from agent.graph.compression.schema import (
    TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE,
    normalize_lossiness_risk,
    normalize_string_list,
    normalize_structured_signals,
)

_SEMANTIC_EVIDENCE_RULES_BLOCK = "\n".join(
    [
        "SEMANTIC EVIDENCE HANDLING",
        "- `semantic_evidence` is deterministic execution-local context produced by the tool layer.",
        "- Treat both `semantic_evidence` and the raw output as observed facts from independent sources.",
        "- If they conflict, do not silently prefer either: preserve both, record the conflict verbatim in `decision_evidence`, downgrade `lossiness_risk` to at least `medium`, and avoid absolute claims in `summary` and `key_findings`.",
        "- Conflicts are signal, not noise; they may indicate a parser bug or runtime anomaly.",
        "- `semantic_evidence` is grouped by `type`. Do not invent new types in `structured_signals`.",
        "- Do not assume `semantic_evidence` is complete. Absence of an evidence entry does not imply absence of the underlying fact.",
        "",
    ]
)

@dataclass
class ProcessedOutput:
    """Structured result returned by tool output processing."""

    summary: str
    key_findings: List[str]
    vulnerabilities: List[str]
    next_actions: List[str]
    structured_signals: List[Dict[str, Any]]
    decision_evidence: List[str]
    lossiness_risk: str
    token_count: int
    importance_score: float
    analysis_source: str = "llm"
    analysis_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)

try:
    from agent.logger import AgentLogger
    from agent.providers.llm.core.base import LLMClient
    from agent.tools.tool_registry import get_tool_metadata
    from agent.utils.output_processing import sample_head_middle_tail
except Exception:  # pragma: no cover - fallback for tests
    from logger import AgentLogger
    from providers.llm.core.base import LLMClient
    from tools.tool_registry import get_tool_metadata
    from utils.output_processing import sample_head_middle_tail


class UniversalToolProcessor:
    """Generic processor for transforming raw tool outputs."""

    _MAX_ANALYSIS_CHARS = 10_000
    # Outputs at or under both caps skip LLM compression and pass through
    # deterministically. Defaults restore the original 1200/40 behavior;
    # set the env vars (e.g. 3000/100) to widen the raw-pass window.
    _LLM_BYPASS_MAX_CHARS = int(os.getenv("TOOL_PROCESSOR_LLM_BYPASS_MAX_CHARS", "1200"))
    _LLM_BYPASS_MAX_LINES = int(os.getenv("TOOL_PROCESSOR_LLM_BYPASS_MAX_LINES", "40"))

    def __init__(self, llm_client: Optional[LLMClient] = None, logger: Optional[AgentLogger] = None) -> None:
        self.llm_client = llm_client
        self.logger = logger

    _prompt_registry: Optional[PromptRegistry] = None

    @classmethod
    def _get_prompt_registry(cls) -> PromptRegistry:
        if cls._prompt_registry is None:
            cls._prompt_registry = PromptRegistry()
        return cls._prompt_registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def process_output(
        self, tool_name: str, raw_output: str, metadata: Optional[Dict[str, Any]] = None
    ) -> ProcessedOutput:
        """Process raw tool output using an LLM with rule-based fallback."""
        metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
        semantic_inputs = extract_runtime_semantic_inputs_with_fallback(metadata_dict)

        text, was_sampled = sample_head_middle_tail(
            raw_output,
            total_limit=self._MAX_ANALYSIS_CHARS,
            return_was_sampled=True,
        )
        fmt = self._detect_format(text)
        category = self._categorize(tool_name, metadata_dict)
        output_mode = self._detect_output_mode(tool_name)
        status = str(metadata_dict.get("status") or "").strip().lower()
        is_failed = status in {"failed", "error"}

        # If there's no tool output, short-circuit with a safe structured default
        if not text.strip():
            return ProcessedOutput(
                summary="No tool output provided to analyze.",
                key_findings=[],
                vulnerabilities=[],
                next_actions=[],
                structured_signals=[],
                decision_evidence=[],
                lossiness_risk="low",
                token_count=0,
                importance_score=0.0,
                analysis_source="deterministic",
                analysis_reason="empty_output",
            )

        use_llm = self._should_use_llm_processing(
            text=text,
            fmt=fmt,
            was_sampled=was_sampled,
            output_mode=output_mode,
            is_failed=is_failed,
        )

        if self.llm_client and use_llm:
            try:
                # Use chat_with_usage for token tracking (Phase 7)
                llm_response = await wait_for_with_timeout(
                    self.llm_client.chat_with_usage(
                        "TOOL OUTPUT PROCESSING",
                        self._build_prompt(
                            tool_name,
                            category,
                            fmt,
                            text,
                            metadata,
                            semantic_inputs=semantic_inputs,
                            was_sampled=was_sampled,
                        ),
                        temperature=0,
                        max_tokens=800,
                        structured_output=TOOL_OUTPUT_COMPRESSOR_STRUCTURED_OUTPUT,
                    ),
                    timeout_sec=LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC,
                    component="TOOL_OUTPUT_COMPRESSOR",
                    operation="tool_output_processing_llm_call",
                    logger=self.logger or logging.getLogger(__name__),
                    outcome="deterministic_fallback",
                    details=f"tool_name={tool_name}",
                )
                content = llm_response.content
                structured_payload = getattr(llm_response, "structured_output", None)
                # Convert usage to dict format for storage
                captured_usage = self._convert_usage_to_dict(
                    llm_response.usage,
                    TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE,
                )
            except asyncio.TimeoutError:
                if self.logger:
                    self.logger.warning(f"LLM request timed out for {tool_name}, using fallback")
                content = None
                captured_usage = None
                structured_payload = None
            except LLMRefusalError:
                raise
            except Exception as exc:  # pragma: no cover - API failure
                if self.logger:
                    self.logger.warning(f"LLM request failed: {exc}")
                content = None
                captured_usage = None
                structured_payload = None
            
            if content or isinstance(structured_payload, dict):
                if isinstance(structured_payload, dict):
                    data = structured_payload
                else:
                    data = self._parse_json_content(content or "")
                if isinstance(data, dict):
                    return ProcessedOutput(
                        summary=data.get("summary", ""),
                        key_findings=normalize_string_list(data.get("key_findings")),
                        vulnerabilities=[],
                        next_actions=[],
                        structured_signals=normalize_structured_signals(
                            data.get("structured_signals")
                        ),
                        decision_evidence=normalize_string_list(
                            data.get("decision_evidence"),
                            limit=5,
                        ),
                        lossiness_risk=normalize_lossiness_risk(
                            data.get("lossiness_risk")
                        ),
                        token_count=self._approx_tokens(content),
                        importance_score=self._score_importance(
                            data.get("key_findings", []),
                            [],
                        ),
                        analysis_source="llm",
                        usage=captured_usage,
                    )
                elif content:
                    if self.logger:
                        preview = content[:400]
                        self.logger.warning(
                            f"Failed to parse LLM response: invalid JSON; preview: {preview}"
                        )

        deterministic_reason = (
            "llm_threshold_bypass"
            if self.llm_client and not use_llm
            else "llm_unavailable_or_failed"
        )
        return self._build_deterministic_output(
            text=text,
            metadata=metadata_dict,
            is_failed=is_failed,
            reason=deterministic_reason,
            was_sampled=was_sampled,
        )

    # ------------------------------------------------------------------
    # Prompt building and helpers
    # ------------------------------------------------------------------
    def _build_prompt(
        self,
        tool_name: str,
        category: str,
        fmt: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        semantic_inputs: Optional[Dict[str, Any]] = None,
        *,
        was_sampled: bool = False,
    ) -> str:
        """Return a versioned compressor prompt rendered from central templates."""

        del category  # Reserved for future template routing by tool category.

        # Extract metadata fields
        status = str(metadata.get("status") or "").strip().lower() if metadata else ""
        is_failed = status in ("failed", "error")
        tool_intent = metadata.get("tool_intent") if metadata else None
        tool_params = metadata.get("tool_params") if metadata else None
        # Format the exact command that was run
        tool_call_str = tool_name
        if tool_params and isinstance(tool_params, dict):
            params_display = ", ".join(f"{k}={repr(v)}" for k, v in tool_params.items() if v is not None)
            if params_display:
                tool_call_str = f"{tool_name}({params_display})"

        template_id = (
            "tool_output_processing_failure" if is_failed else "tool_output_processing_success"
        )
        template = self._get_prompt_registry().get_template(template_id)
        semantic_payload = semantic_inputs if isinstance(semantic_inputs, dict) else {}
        rendered_observations = render_semantic_observations_for_prompt(
            semantic_payload.get("semantic_observations")
        )
        rendered_evidence = render_semantic_evidence_for_prompt(
            semantic_payload.get("semantic_evidence")
        )
        has_semantic_context = bool(rendered_observations or rendered_evidence)
        analysis_context = self._render_analysis_context_for_prompt(
            semantic_rules_block=(
                _SEMANTIC_EVIDENCE_RULES_BLOCK if has_semantic_context else ""
            ),
            semantic_observations=rendered_observations,
            semantic_evidence=rendered_evidence,
        )
        prompt_context = {
            "tool_call": tool_call_str,
            "output_format": fmt,
            "tool_intent": str(tool_intent).strip() if tool_intent else "none",
            "output_mode": self._detect_output_mode(tool_name),
            "sampling_mode": "head_middle_tail_sample" if was_sampled else "full_or_contiguous",
            "analysis_context": analysis_context,
            "tool_output": text,
        }
        safe_context = {
            key: "" if value is None else str(value)
            for key, value in prompt_context.items()
        }
        return template.format_map(safe_context)

    @staticmethod
    def _render_analysis_context_for_prompt(
        *,
        semantic_rules_block: str,
        semantic_observations: str,
        semantic_evidence: str,
    ) -> str:
        """Render optional derived-analysis sections with raw-output separation."""

        sections: list[str] = []
        semantic_lines: list[str] = []
        if semantic_rules_block.strip():
            semantic_lines.append(semantic_rules_block.strip())
        if semantic_observations.strip():
            if semantic_lines:
                semantic_lines.append("")
            semantic_lines.extend(
                [
                    "SEMANTIC OBSERVATIONS:",
                    semantic_observations.strip(),
                ]
            )
        if semantic_evidence.strip():
            if semantic_lines:
                semantic_lines.append("")
            semantic_lines.extend(
                [
                    "SEMANTIC EVIDENCE:",
                    semantic_evidence.strip(),
                ]
            )
        if semantic_lines:
            sections.append("\n".join(semantic_lines))
        if not sections:
            return ""
        return "\n\n".join(sections) + "\n\n"

    def _should_use_llm_processing(
        self,
        *,
        text: str,
        fmt: str,
        was_sampled: bool,
        output_mode: str,
        is_failed: bool,
    ) -> bool:
        """Decide whether to invoke LLM processing for this output payload."""
        if was_sampled:
            return True

        line_count = len([line for line in text.splitlines() if line.strip()])
        return not (
            len(text) <= self._LLM_BYPASS_MAX_CHARS
            and line_count <= self._LLM_BYPASS_MAX_LINES
        )

    @staticmethod
    def _clean_lines(text: str) -> List[str]:
        """Normalize to non-empty, stripped lines."""
        return [line.strip() for line in text.splitlines() if line.strip()]

    @staticmethod
    def _truncate(text: str, limit: int = COMPACT_SUMMARY_MAX_CHARS) -> str:
        """Truncate text to a hard character limit.

        Callers MUST pass an explicit limit sourced from
        ``core.prompts.constants`` so truncation stays configurable from
        one place. The default exists only as a safety net for the
        summary-shaped path.
        """
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[: max(limit - 3, 0)].rstrip() + "..."

    @staticmethod
    def _dedupe_preserve_order(items: List[str]) -> List[str]:
        """De-duplicate strings while preserving insertion order."""
        seen: set[str] = set()
        deduped: List[str] = []
        for item in items:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _build_deterministic_output(
        self,
        *,
        text: str,
        metadata: Dict[str, Any],
        is_failed: bool,
        reason: str,
        was_sampled: bool,
    ) -> ProcessedOutput:
        """Build structured output without LLM calls."""
        stdout_text = str(metadata.get("stdout") or "")
        stderr_text = str(metadata.get("stderr") or "")
        if is_failed:
            return self._build_deterministic_failure_output(
                text=text,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                reason=reason,
                was_sampled=was_sampled,
            )
        return self._build_deterministic_success_output(
            text=text,
            reason=reason,
            was_sampled=was_sampled,
        )

    def _build_deterministic_success_output(
        self,
        *,
        text: str,
        reason: str,
        was_sampled: bool,
    ) -> ProcessedOutput:
        """Deterministic success summarization for bounded command outputs."""
        lines = self._clean_lines(text)
        summary = self._truncate(
            lines[0] if lines else text, limit=COMPACT_SUMMARY_MAX_CHARS
        )

        findings = self._rule_based_findings(text)
        if not findings:
            findings = [
                self._truncate(line, limit=COMPACT_FINDING_MAX_CHARS)
                for line in lines
            ]
            findings = self._dedupe_preserve_order(findings)

        return ProcessedOutput(
            summary=summary or "Command completed successfully.",
            key_findings=findings,
            vulnerabilities=[],
            next_actions=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="medium" if was_sampled else "low",
            token_count=self._approx_tokens(text),
            importance_score=self._score_importance(findings, []),
            analysis_source="deterministic",
            analysis_reason=reason,
        )

    def _build_deterministic_failure_output(
        self,
        *,
        text: str,
        stdout_text: str,
        stderr_text: str,
        reason: str,
        was_sampled: bool,
    ) -> ProcessedOutput:
        """Deterministic failure summarization that preserves stderr and stdout signal."""
        stderr_lines = self._clean_lines(stderr_text)
        stdout_lines = self._clean_lines(stdout_text)
        text_lines = self._clean_lines(text)

        primary_error = stderr_lines[0] if stderr_lines else (text_lines[0] if text_lines else "Command failed.")
        summary = (
            f"Command failed: {self._truncate(primary_error, limit=COMPACT_ERROR_LEAD_MAX_CHARS)}"
        )
        if stdout_lines:
            summary = f"{summary} Stdout also produced output."

        findings: List[str] = [
            self._truncate(line, limit=COMPACT_FAILURE_FINDING_MAX_CHARS)
            for line in stderr_lines[:3]
        ]
        if stdout_lines:
            findings.append(
                f"stdout: {self._truncate(stdout_lines[0], limit=COMPACT_FAILURE_STDOUT_LINE_MAX_CHARS)}"
            )
        if not findings and text_lines:
            findings.append(
                self._truncate(text_lines[0], limit=COMPACT_FAILURE_FINDING_MAX_CHARS)
            )
        findings = self._dedupe_preserve_order(findings)

        structured_signals: List[Dict[str, Any]] = []
        if primary_error:
            structured_signals.append(
                {
                    "type": "error_context",
                    "message": self._truncate(
                        primary_error, limit=COMPACT_ERROR_CONTEXT_MESSAGE_MAX_CHARS
                    ),
                }
            )

        decision_evidence: List[str] = []
        if stderr_lines:
            decision_evidence.append(
                self._truncate(stderr_lines[0], limit=COMPACT_DECISION_EVIDENCE_MAX_CHARS)
            )
        if stdout_lines:
            decision_evidence.append(
                "stdout: "
                + self._truncate(
                    stdout_lines[0], limit=COMPACT_FAILURE_STDOUT_LINE_MAX_CHARS
                )
            )

        return ProcessedOutput(
            summary=summary,
            key_findings=findings,
            vulnerabilities=[],
            next_actions=[],
            structured_signals=normalize_structured_signals(structured_signals),
            decision_evidence=normalize_string_list(decision_evidence, limit=2),
            lossiness_risk="high" if was_sampled else "medium",
            token_count=self._approx_tokens(text),
            importance_score=self._score_importance(findings, []),
            analysis_source="deterministic",
            analysis_reason=reason,
        )

    def _detect_output_mode(self, tool_name: str) -> str:
        """Classify output interpretation mode for prompt rendering."""
        lowered = tool_name.lower()
        tokens = [token for token in re.split(r"[^a-z0-9]+", lowered) if token]
        if "stat_path" in lowered or "stat" in tokens:
            return "metadata_only"
        if "read_file" in lowered or "read" in tokens:
            return "file_contents"
        return "command_output"

    def _detect_format(self, text: str) -> str:
        """Detect the format of tool output for better analysis prompts.
        
        Args:
            text: Raw tool output text.
            
        Returns:
            Format string: "json", "xml", "csv", or "text" (default).
        """
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        if stripped.startswith("<?xml") or ("<" in stripped[:50] and ">" in stripped[:50]):
            return "xml"
        
        # More strict CSV detection - look for consistent comma-separated structure
        lines = stripped.splitlines()
        if len(lines) >= 2:
            first = lines[0]
            second = lines[1] if len(lines) > 1 else ""
            # CSV should have multiple commas and similar structure across lines
            first_parts = first.split(",")
            second_parts = second.split(",")
            # Only detect as CSV if multiple columns AND similar column count
            if len(first_parts) >= 3 and len(second_parts) >= 2:
                if abs(len(first_parts) - len(second_parts)) <= 1:
                    return "csv"
        
        return "text"

    def _categorize(self, tool_name: str, meta: Optional[Dict[str, Any]]) -> str:
        if meta and meta.get("category"):
            return str(meta["category"])
        parts = tool_name.split(".")
        if len(parts) > 1:
            return parts[0]
        try:
            data = get_tool_metadata(tool_name)
            return data.get("category", parts[0])
        except Exception:
            return parts[0]

    def _rule_based_findings(self, text: str) -> List[str]:
        findings: List[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            lower = line.lower()
            if any(k in lower for k in ("vuln", "open", "error", "failed")):
                candidate = line.strip()[:COMPACT_RULE_FINDING_MAX_CHARS]
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    findings.append(candidate)
        return findings

    def _approx_tokens(self, text: str) -> int:
        """Count tokens accurately using tiktoken or fallback to approximation."""
        try:
            from .token_utils import count_tokens
            return count_tokens(text)
        except ImportError:
            # Fallback to old approximation if token_utils not available
            return max(len(text) // 4, 1)

    def _score_importance(self, key_findings: List[Any], vulnerabilities: List[Any]) -> float:
        score = 5.0 + len(vulnerabilities) * 2 + len(key_findings) * 0.5
        return float(min(score, 10.0))

    # ------------------------------------------------------------------
    # Robust JSON parsing helpers
    # ------------------------------------------------------------------
    def _strip_code_fences(self, content: str) -> str:
        s = content.strip()
        if s.startswith("```"):
            # remove first fence line
            s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
            # remove trailing fence
            if s.endswith("```"):
                s = s[: -3]
        return s.strip()

    def _extract_first_json_object(self, content: str) -> Optional[str]:
        text = content
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    def _parse_json_content(self, content: str) -> Optional[Dict[str, Any]]:
        # Try strict parse first
        try:
            return json.loads(content)
        except Exception:
            pass
        # Strip code fences and retry
        stripped = self._strip_code_fences(content)
        if stripped != content:
            try:
                return json.loads(stripped)
            except Exception:
                pass
        # Extract the first balanced JSON object and retry
        obj = self._extract_first_json_object(stripped)
        if obj:
            try:
                return json.loads(obj)
            except Exception:
                return None
        return None

    def _convert_usage_to_dict(self, usage: Any, source: str = "unknown") -> Optional[Dict[str, Any]]:
        """Convert UsageData to dict for storage (Phase 7).
        
        Delegates to UsageData.to_dict() when available for consistency.
        Falls back to manual extraction for backward compatibility.
        
        Args:
            usage: UsageData instance or None
            source: Identifier for the call site
            
        Returns:
            Dict representation with source tag, or None if usage is None/invalid
        """
        if usage is None:
            return None
        
        # Prefer UsageData.to_dict() if available (canonical implementation)
        if hasattr(usage, 'to_dict') and callable(usage.to_dict):
            try:
                return usage.to_dict(source)
            except Exception:
                pass  # Fall through to manual extraction
        
        # Handle UsageData objects without to_dict (backward compatibility)
        if hasattr(usage, 'prompt_tokens'):
            return {
                "prompt_tokens": getattr(usage, 'prompt_tokens', 0) or 0,
                "completion_tokens": getattr(usage, 'completion_tokens', 0) or 0,
                "total_tokens": getattr(usage, 'total_tokens', 0) or 0,
                "model": getattr(usage, 'model', 'unknown'),
                "provider": getattr(usage, 'provider', 'openai'),
                "cached_tokens": getattr(usage, 'cached_tokens', 0) or 0,
                "reasoning_tokens": getattr(usage, 'reasoning_tokens', 0) or 0,
                "source": source,
            }
        
        return None
