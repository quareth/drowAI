"""Orchestrate memory extraction gate, parsing, and persistence flow.

This service coordinates structural checks, LLM gate/extraction calls, and
delegation to MemoryStore for persistence. It does not create DB sessions and
does not manage transaction commits.
"""

from __future__ import annotations

import os
import re
import logging
from typing import TYPE_CHECKING

from runtime_shared.durable_secret_masking import mask_durable_secrets

from core.llm import (
    LLM_TIMEOUT_MEMORY_EXTRACTION_SEC,
    LLM_TIMEOUT_MEMORY_GATE_SEC,
    wait_for_with_timeout,
)
from core.llm.structured_schemas import (
    MEMORY_EXTRACTION_STRUCTURED_OUTPUT,
    MEMORY_GATE_STRUCTURED_OUTPUT,
)

from .memory_extraction_prompts import (
    build_extraction_messages,
    build_gate_classifier_messages,
)
from .memory_extraction_schemas import ExtractionResult, GateClassifierOutput
from .memory_models import MemoryCreateRequest, MemorySearchResult, MemoryTier
from .memory_store import MemoryStore

if TYPE_CHECKING:
    from agent.providers.llm.core.base import LLMClient

logger = logging.getLogger(__name__)

MEMORY_EXTRACTION_MIN_MESSAGE_LENGTH = int(
    os.getenv("MEMORY_EXTRACTION_MIN_MESSAGE_LENGTH", "10")
)
MEMORY_EXTRACTION_MAX_FACTS_PER_TURN = int(
    os.getenv("MEMORY_EXTRACTION_MAX_FACTS_PER_TURN", "5")
)
_TOOL_OUTPUT_LINE_RE = re.compile(
    r"^(?:"
    r"[$#]\s+"
    r"|nmap scan report"
    r"|starting nmap"
    r"|host is up"
    r"|not shown:"
    r"|service info:"
    r"|port\s+state\s+service"
    r"|cve-\d{4}-\d+"
    r"|\d+/(?:tcp|udp)\s+\S+"
    r"|[A-Z_][A-Z0-9_]*="
    r")",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")


class MemoryExtractionService:
    """Orchestrate memory extraction: structural check -> gate -> extract -> store."""

    def __init__(
        self,
        memory_store: MemoryStore,
        gate_client: "LLMClient",
        extraction_client: "LLMClient",
    ) -> None:
        self.memory_store = memory_store
        self.gate_client = gate_client
        self.extraction_client = extraction_client

    @staticmethod
    def _assistant_response_is_pure_tool_output(assistant_response: str) -> bool:
        """Return True when response appears to be raw tool output with no prose."""
        stripped = assistant_response.strip()
        if not stripped:
            return False

        # Fenced block with no surrounding prose is treated as tool-only output.
        prose_without_fences = _CODE_FENCE_RE.sub("", stripped).strip()
        if not prose_without_fences and "```" in stripped:
            return True

        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if len(lines) < 2:
            return False
        return all(_TOOL_OUTPUT_LINE_RE.match(line) for line in lines)

    def _structural_check(self, user_message: str, assistant_response: str) -> bool:
        """Return True when the turn is structurally worth evaluating."""
        if not user_message or not user_message.strip():
            return False
        if len(user_message.strip()) < MEMORY_EXTRACTION_MIN_MESSAGE_LENGTH:
            return False
        if not assistant_response or not assistant_response.strip():
            return False
        if self._assistant_response_is_pure_tool_output(assistant_response):
            return False
        return True

    async def should_extract(self, user_message: str, assistant_response: str) -> bool:
        """Run structural + LLM gate to determine whether extraction should run."""
        if not self._structural_check(user_message, assistant_response):
            return False

        messages = build_gate_classifier_messages(user_message, assistant_response)
        response = await wait_for_with_timeout(
            self.gate_client.chat_with_usage(
                messages[0]["content"],
                messages[1]["content"],
                structured_output=MEMORY_GATE_STRUCTURED_OUTPUT,
            ),
            timeout_sec=LLM_TIMEOUT_MEMORY_GATE_SEC,
            component="MEMORY_GATE",
            operation="memory_gate_llm_call",
            logger=logger,
            outcome="memory_gate_timeout",
        )
        parsed = GateClassifierOutput.model_validate(response.structured_output or {})
        return parsed.extractable

    async def extract(
        self,
        user_message: str,
        assistant_response: str,
        *,
        user_id: int,
        tenant_id: int | None,
        engagement_id: int | None,
        task_id: int | None,
        conversation_id: str | None,
        turn_id: str | None,
    ) -> list[MemorySearchResult]:
        """Run extraction and persist resulting facts via MemoryStore."""
        messages = build_extraction_messages(user_message, assistant_response)
        response = await wait_for_with_timeout(
            self.extraction_client.chat_with_usage(
                messages[0]["content"],
                messages[1]["content"],
                structured_output=MEMORY_EXTRACTION_STRUCTURED_OUTPUT,
            ),
            timeout_sec=LLM_TIMEOUT_MEMORY_EXTRACTION_SEC,
            component="MEMORY_EXTRACTION",
            operation="memory_extraction_llm_call",
            logger=logger,
            task_id=task_id,
            outcome="memory_extraction_timeout",
        )
        parsed = ExtractionResult.model_validate(response.structured_output or {})

        results: list[MemorySearchResult] = []
        for fact in parsed.facts[:MEMORY_EXTRACTION_MAX_FACTS_PER_TURN]:
            tier = MemoryTier(fact.tier)
            durable_content = str(
                mask_durable_secrets(
                    fact.content,
                    source="memory_extraction.fact_content",
                )
                or ""
            )
            request = MemoryCreateRequest(
                content=durable_content,
                memory_tier=tier,
                user_id=user_id,
                tenant_id=tenant_id if tier == MemoryTier.TASK_ENGAGEMENT else None,
                engagement_id=engagement_id if tier == MemoryTier.TASK_ENGAGEMENT else None,
                task_id=task_id if tier == MemoryTier.TASK_ENGAGEMENT else None,
                source_type="chat_extraction",
                conversation_id=conversation_id,
                source_turn_id=turn_id,
            )
            stored = await self.memory_store.store(request)
            if stored is not None:
                results.append(stored)

        return results

    async def extract_if_needed(
        self,
        user_message: str,
        assistant_response: str,
        *,
        user_id: int,
        tenant_id: int | None,
        engagement_id: int | None,
        task_id: int | None,
        conversation_id: str | None,
        turn_id: str | None,
    ) -> list[MemorySearchResult]:
        """Gate and extract memories in one convenience call."""
        if not await self.should_extract(user_message, assistant_response):
            return []
        return await self.extract(
            user_message,
            assistant_response,
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            task_id=task_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
